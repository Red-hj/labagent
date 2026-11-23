from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.agentarts_client import AgentArtsError
from labagent.cloud_workflow import CloudWorkflow
from labagent.material_service import MaterialService, SessionNotFound
from labagent.report import generate_report
from labagent.store import Store
from test_investigation import ROOT, ENV, stages, completion
from test_resolution import GOAL, FEEDBACK, reported, call
from test_experiment_goals import payload as goal_payload, scripted_completion


def compare_then_complete(messages):
    if messages[-1]["role"] != "tool":
        return call("compare_materials", {})
    if "changes" in json.loads(messages[-1]["content"]):
        return completion([{"role": "user"}])
    return completion(messages)


class SessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / "web.sqlite3")
        self.services = {}
        self.calls = []
        self.now = 1000
        self.completion = completion
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.workflow = CloudWorkflow(self.store, self.client_factory, clock=lambda: self.now)

    def client_factory(self, platform_id):
        owner = self

        class Client:
            def invoke(self, request):
                owner.calls.append((platform_id, deepcopy(request)))
                if platform_id not in owner.services:
                    owner.services[platform_id] = MaterialService(owner.folder / (platform_id + ".sqlite3"), owner.completion)
                service = owner.services[platform_id]
                service.completion = owner.completion
                try:
                    return service.invoke(request)
                except SessionNotFound:
                    raise AgentArtsError("运行时返回 HTTP 400", http_status=400, code="session_not_found") from None
        return Client()

    def run_round(self, payload):
        return self.store.save(payload, self.workflow.run(payload))

    def initial(self):
        payload = stages()[0]
        payload["requirements"] = {"goal": GOAL}
        return self.run_round(payload)

    def test_idle_recovery_preserves_context_new_evidence_and_web_report(self):
        first = self.initial()
        self.now += 301
        self.completion = compare_then_complete
        payload = {**stages()[1], "session_id": first["session_id"], "requirements": {"goal": GOAL},
                   "interaction": {"type": "update", "message": "已改为正确网关，继续检查。"}}
        second = self.run_round(payload)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertNotEqual(second["agentarts"]["platform_session_id"], first["agentarts"]["platform_session_id"])
        self.assertNotEqual(second["agentarts"]["session_id"], first["agentarts"]["session_id"])
        self.assertTrue(second["agentarts"]["session_recovered"])
        self.assertTrue(second["workflow"]["context_restored"])
        self.assertEqual(len(second["conversation"]), 2)
        self.assertEqual(second["conversation"][0], first["conversation"][0])
        self.assertEqual(second["investigation"]["experiment_goal"], GOAL)
        self.assertEqual(second["investigation"]["experiment_state"]["goal"], GOAL)
        self.assertEqual(second["investigation"]["experiment_state"]["turn_count"], 2)
        diff = second["interaction_checks"]["compare_materials"]
        self.assertTrue(diff["has_previous"])
        self.assertTrue(diff["changed"])
        self.assertIn("- gateway-list 10.20.0.254", str(diff))
        self.assertIn("+ gateway-list 10.20.0.1", str(diff))
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("session_id", self.calls[-1][1])
        cloud = self.services[second["agentarts"]["platform_session_id"]]
        saved = cloud.store.history(second["agentarts"]["session_id"])["attempts"][-1]["input"]
        self.assertEqual(saved["configs"], payload["configs"])
        self.assertEqual(saved["question"], payload["question"])
        self.now += 5
        third = self.run_round({**payload, "interaction": {"type": "ask", "message": "为什么要重新获取租约？"}})
        self.assertEqual(third["agentarts"]["platform_session_id"], second["agentarts"]["platform_session_id"])
        self.assertEqual(third["agentarts"]["started_at"], second["agentarts"]["started_at"])
        self.assertEqual(len(third["conversation"]), 3)
        history = self.store.history(first["session_id"])
        self.assertEqual(len(history["attempts"]), 3)
        report = generate_report(history)
        self.assertIn(GOAL, report)
        self.assertIn("已改为正确网关", report)

    def test_explicit_missing_session_recovers_once_without_repeating_diagnosis(self):
        first = self.initial()
        platform_id = first["agentarts"]["platform_session_id"]
        # A fresh container has neither attempts nor LangGraph checkpoints.
        self.services[platform_id] = MaterialService(self.folder / "empty.sqlite3", completion)
        self.now += 5
        second = self.run_round({**stages()[1], "session_id": first["session_id"]})
        self.assertTrue(second["agentarts"]["session_recovered"])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[1][0], platform_id)
        self.assertNotEqual(self.calls[2][0], platform_id)
        self.assertEqual(len(second["conversation"]), 2)
        self.assertEqual(len(self.store.history(first["session_id"])["attempts"]), 2)

    def test_busy_session_rotates_at_maximum_age(self):
        first = self.initial()
        # Active recently, but the runtime's total lifetime is exhausted.
        self.now += 3601
        with self.store.connect() as db:
            row = db.execute("SELECT id, result FROM attempts").fetchone()
            result = json.loads(row[1])
            result["agentarts"]["last_activity_at"] = self.now - 5
            db.execute("UPDATE attempts SET result=? WHERE id=?", (json.dumps(result), row[0]))
        result = self.run_round({**stages()[0], "session_id": first["session_id"]})
        self.assertTrue(result["agentarts"]["session_recovered"])
        self.assertEqual(len(self.calls), 2)

    def test_legacy_web_history_recovers_without_sending_expired_platform_id(self):
        first = self.initial()
        with self.store.connect() as db:
            row = db.execute("SELECT id, result FROM attempts").fetchone()
            result = json.loads(row[1])
            for key in ("started_at", "last_activity_at"):
                result["agentarts"].pop(key)
            db.execute("UPDATE attempts SET result=? WHERE id=?", (json.dumps(result), row[0]))
        result = self.run_round({**stages()[0], "session_id": first["session_id"]})
        self.assertTrue(result["agentarts"]["session_recovered"])
        self.assertNotEqual(self.calls[-1][0], self.calls[0][0])

    def test_generic_errors_never_retry_or_append_web_attempts(self):
        first = self.initial()
        for error in (AgentArtsError("HTTP 500", http_status=500),
                      AgentArtsError("HTTP 401", http_status=401),
                      AgentArtsError("HTTP 400", http_status=400), ValueError("连接超时")):
            with self.subTest(error=str(error)):
                invoke = unittest.mock.Mock(side_effect=error)
                self.workflow.client_factory = lambda platform_id: type("Client", (), {"invoke": invoke})()
                with self.assertRaises(ValueError):
                    self.workflow.run({**stages()[0], "session_id": first["session_id"]})
                self.assertEqual(invoke.call_count, 1)
                self.assertEqual(len(self.store.history(first["session_id"])["attempts"]), 1)

    def test_old_runtime_image_cannot_silently_discard_restored_context(self):
        first = self.initial()
        self.now += 301
        legacy = {"session_id": "e" * 32, "status": "success", "result": first}
        invoke = unittest.mock.Mock(return_value=legacy)
        self.workflow.client_factory = lambda platform_id: type("Client", (), {"invoke": invoke})()
        with self.assertRaisesRegex(ValueError, "更新AgentArts镜像"):
            self.workflow.run({**stages()[0], "session_id": first["session_id"]})
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(len(self.store.history(first["session_id"])["attempts"]), 1)

    def test_lost_checkpoint_uses_existing_attempt_without_creating_new_cloud_session(self):
        first = self.initial()
        service = self.services[first["agentarts"]["platform_session_id"]]
        service.workflow_database = self.folder / "new-checkpoints.sqlite3"
        self.completion = compare_then_complete
        second = self.run_round({**stages()[1], "session_id": first["session_id"],
                                 "interaction": {"type": "update", "message": "已改网关。"}})
        self.assertFalse(second["agentarts"]["session_recovered"])
        self.assertEqual(second["agentarts"]["session_id"], first["agentarts"]["session_id"])
        self.assertTrue(second["workflow"]["context_restored"])
        self.assertTrue(second["interaction_checks"]["compare_materials"]["has_previous"])
        self.assertEqual(len(second["conversation"]), 2)

    def test_recovered_retest_uses_actual_output_and_report_status(self):
        first = self.initial()
        self.now += 301

        def verify(messages):
            if messages[-1]["role"] != "tool":
                return call("check_submitted_pings", {})
            return call("submit_hypotheses", {"summary": "新原始输出核验通过", "hypotheses": [],
                        "evidence_requests": [], "outcome": "observations_passed", "verification_targets": ["10.30.0.10"]})

        self.completion = verify
        payload = {**stages()[1], "session_id": first["session_id"],
                   "interaction": {"type": "verify", "message": "PC1最新输出：\nPING 10.30.0.10 (10.30.0.10) 56 data bytes\n4 packets transmitted, 4 received, 0% packet loss"}}
        result = self.run_round(payload)
        self.assertEqual(result["status"], "verified_from_observations")
        self.assertTrue(result["interaction_checks"]["check_submitted_pings"]["pings"][0]["passed"])
        report = generate_report(self.store.history(first["session_id"]))
        self.assertIn("新原始输出核验通过", report)

    def test_protocol_requirements_are_rechecked_after_each_runtime_recovery(self):
        self.completion = scripted_completion(0)
        first = self.run_round(goal_payload(0))
        criteria = first["investigation"]["goal_assessment"]["criteria"]
        self.now += 301
        self.completion = scripted_completion(1)
        middle = {**goal_payload(1), "session_id": first["session_id"],
                  "interaction": {"type": "verify", "message": "Ping已通，核对实验目标。"}}
        second = self.run_round(middle)
        assessment = second["investigation"]["goal_assessment"]
        self.assertTrue(second["agentarts"]["session_recovered"])
        self.assertEqual(assessment["criteria"], criteria)
        self.assertEqual([row["status"] for row in assessment["checks"]], ["passed", "failed", "failed"])
        self.assertNotEqual(second["status"], "verified_from_observations")
        self.now += 301
        self.completion = scripted_completion(2)
        last = {**goal_payload(2), "session_id": first["session_id"],
                "interaction": {"type": "verify", "message": "已修正OSPF。"}}
        final = self.run_round(last)
        self.assertEqual(final["investigation"]["goal_assessment"]["criteria"], criteria)
        self.assertEqual(final["investigation"]["goal_assessment"]["status"], "passed")
        self.assertEqual(final["status"], "verified_from_observations")
        self.assertEqual(len(self.store.history(first["session_id"])["attempts"]), 3)

    def test_user_reported_completion_survives_recovery_and_conceptual_question(self):
        first = self.initial()
        self.completion = reported
        resolved = self.run_round({**stages()[0], "session_id": first["session_id"],
                                   "interaction": {"type": "ask", "message": FEEDBACK}})
        self.assertEqual(resolved["status"], "resolved_by_user_report")
        self.now += 301
        def explain(messages):
            if messages[-1]["role"] != "tool":
                return call("read_source", {"source_id": "config:1", "start_line": 1, "end_line": 10})
            return call("submit_hypotheses", {
                "summary": "租约保留旧网关，重新获取后更新。", "hypotheses": [], "evidence_requests": []})
        self.completion = explain
        continued = self.run_round({**stages()[0], "session_id": first["session_id"],
                                    "interaction": {"type": "ask", "message": "为什么需要重新获取租约？"}})
        self.assertEqual(continued["status"], "resolved_by_user_report")
        self.assertEqual(continued["investigation"]["verification"], resolved["investigation"]["verification"])
        self.assertEqual(len(continued["conversation"]), 3)

    def test_invalid_resume_context_rejected_without_saving(self):
        service = MaterialService(self.folder / "invalid.sqlite3", completion)
        for snapshot in (None, [], {}, {"input": stages()[0], "result": {"investigation": []}},
                         {"input": stages()[0], "result": {"investigation": {}, "conversation": ["bad"]}}):
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                service.invoke({"resume_from": snapshot})
        self.assertEqual(service.store.sessions(), [])

    def test_malformed_nested_context_rejected_before_graph_or_model_runs(self):
        service = MaterialService(self.folder / "malformed.sqlite3", completion)
        for analysis in ({"hypotheses": ["bad"]}, {"pending_requests": [None]},
                         {"outcome": {}}, {"outcome": "observations_passed"},
                         {"goal_assessment": {"criteria": [None]}}):
            with self.subTest(analysis=analysis), self.assertRaises(ValueError):
                service.invoke({"resume_from": {"input": stages()[0], "result": {"investigation": analysis}}})
        self.assertEqual(service.store.sessions(), [])


@unittest.skipUnless(importlib.util.find_spec("agentarts"), "需要AgentArts SDK独立环境")
class RecoverySdkTests(unittest.TestCase):
    def test_recovery_through_official_sdk_in_fresh_container(self):
        from starlette.testclient import TestClient
        from labagent.agentarts_runtime import create_app
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp, patch.dict(os.environ, ENV):
            original = create_app(Path(temp) / "original.sqlite3", completion)
            with TestClient(original) as client:
                response = client.post("/invocations", json={"materials": stages()[0]["configs"]},
                                       headers={"x-hw-agentarts-session-id": "old-platform"})
                self.assertEqual(response.status_code, 200)
                first = response.json()
            snapshot = original.material_service.store.history(first["session_id"])["attempts"][-1]
            fresh = create_app(Path(temp) / "fresh.sqlite3", compare_then_complete)
            with TestClient(fresh) as client:
                missing = client.post("/invocations", json={"session_id": first["session_id"], "message": "继续"})
                self.assertEqual(missing.status_code, 400)
                self.assertEqual(missing.json()["code"], "session_not_found")
                response = client.post("/invocations", json={"resume_from": snapshot,
                    "materials": stages()[1]["configs"], "message": "网关已修复", "action": "update"},
                    headers={"x-hw-agentarts-session-id": "fresh-platform"})
                self.assertEqual(response.status_code, 200, response.text)
                second = response.json()
                self.assertTrue(second["session_recovered"])
                self.assertEqual(response.headers["x-hw-agentarts-session-id"], "fresh-platform")
                self.assertNotEqual(second["session_id"], first["session_id"])
                self.assertEqual(len(second["result"]["conversation"]), 2)
                self.assertTrue(second["result"]["interaction_checks"]["compare_materials"]["changed"])
                again = client.post("/invocations", json={"message": "继续解释"},
                                    headers={"x-hw-agentarts-session-id": "fresh-platform"})
                self.assertEqual(again.status_code, 200, again.text)
                self.assertEqual(again.json()["session_id"], second["session_id"])


if __name__ == "__main__":
    unittest.main()
