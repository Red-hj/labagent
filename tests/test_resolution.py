from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from labagent.investigation import investigate, source_bundle, execute_tool, validate_submission
from labagent.report import generate_report
from labagent.store import Store
from labagent.workflow import available, run_open_workflow
from test_investigation import ROOT, ENV, stages, submission, completion


GOAL = "PC1通过R1访问服务器10.30.0.10"
FEEDBACK = "我已把网关改为10.20.0.1，PC1到10.30.0.10的ping全部成功，访问恢复了。"


def call(name, arguments):
    return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


def reported(messages):
    if messages[-1]["role"] != "tool":
        return call("read_source", {"source_id": "message:current", "start_line": 1, "end_line": 1})
    return call("submit_hypotheses", {"summary": "用户反馈PC1访问10.30.0.10已恢复。", "reply": "访问已经恢复，可以保存本轮报告。",
        "outcome": "user_reported_resolved", "experiment_goal": GOAL, "verification_targets": ["10.30.0.10"],
        "resolution_evidence": [{"source_id": "message:current", "line": 1, "quote": FEEDBACK}],
        "hypotheses": [], "evidence_requests": [{"device": "R1", "information": "补NAT", "why": "旧请求"}]})


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.payload = stages()[0]
        self.payload.setdefault("requirements", {})["goal"] = GOAL

    def test_user_report_resolves_and_removes_stale_material_requests(self):
        payload = deepcopy(self.payload)
        payload["interaction"] = {"type": "ask", "message": FEEDBACK}
        with patch.dict("os.environ", ENV):
            result = investigate(payload, {"pending_requests": submission()["evidence_requests"]}, reported)
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertEqual(result["status"], "resolved_by_user_report")
        self.assertEqual(result["investigation"]["pending_requests"], [])
        self.assertEqual(result["runtime_missing"], [])
        self.assertEqual(result["interaction_checks"]["user_report"]["source"], "user_report")
        self.assertNotIn("check_submitted_pings", result["interaction_checks"])

    def test_chat_raw_ping_replaces_old_uploaded_failure_for_the_same_target(self):
        payload = deepcopy(self.payload)
        payload["configs"] = {"PC1": "Ping 10.30.0.10: 4 packets transmitted, 0 received"}
        payload["interaction"] = {"type": "verify", "message": "PC1最新输出：\nPING 10.30.0.10 (10.30.0.10) 56 data bytes\n4 packets transmitted, 4 received, 0% packet loss"}
        sources, _ = source_bundle(payload)
        seen = set()
        checked, _ = execute_tool("check_submitted_pings", {}, payload, sources, seen)
        self.assertEqual(len(checked["pings"]), 1)
        self.assertEqual(checked["pings"][0]["source_id"], "message:current")
        self.assertTrue(checked["pings"][0]["passed"])
        args = {"summary": "目标测试通过", "hypotheses": [], "evidence_requests": [], "outcome": "observations_passed", "verification_targets": ["10.30.0.10"]}
        result = validate_submission(args, sources, seen, conversational=True, verifying=True, checked_pings=checked, required_targets=["10.30.0.10"])
        self.assertEqual(result["outcome"], "observations_passed")

    def test_conversational_counts_remain_a_user_report(self):
        payload = deepcopy(self.payload)
        payload["configs"] = {"PC1": "原配置"}
        payload["interaction"] = {"type": "ask", "message": "我ping 10.30.0.10：发送4个，收到4个。"}
        sources, _ = source_bundle(payload)
        result, _ = execute_tool("check_submitted_pings", {}, payload, sources, set())
        self.assertEqual(result["pings"], [])
        self.assertEqual(len(result["reported_pings"]), 1)

    def test_raw_output_upgrades_reported_resolution_and_its_conclusion(self):
        payload = deepcopy(self.payload)
        payload["interaction"] = {"type": "ask", "message": "PING 10.30.0.10 (10.30.0.10) 56 data bytes\n4 packets transmitted, 4 received, 0% packet loss"}
        previous = {"outcome": "user_reported_resolved", "summary": "用户反馈恢复", "resolution_summary": "用户反馈恢复", "experiment_goal": GOAL,
                    "verification_targets": ["10.30.0.10"], "verification": {"source": "user_report", "targets": ["10.30.0.10"], "evidence": []}}
        def checked(messages):
            if messages[-1]["role"] != "tool":
                return call("check_submitted_pings", {})
            return call("submit_hypotheses", {"summary": "新原始输出核验通过", "hypotheses": [], "evidence_requests": [], "outcome": "observations_passed", "verification_targets": ["10.30.0.10"]})
        with patch.dict("os.environ", ENV):
            result = investigate(payload, previous, checked, previous_materials=payload["configs"])
        self.assertEqual(result["status"], "verified_from_observations")
        self.assertEqual(result["investigation"]["resolution_summary"], "新原始输出核验通过")

    def test_previous_goal_targets_cannot_be_replaced_with_only_gateway(self):
        value = {"summary": "通过", "hypotheses": [], "evidence_requests": [], "outcome": "observations_passed", "verification_targets": ["10.20.0.1"]}
        with self.assertRaisesRegex(ValueError, "原实验目标"):
            validate_submission(value, {}, set(), conversational=True, verifying=True, checked_pings={"pings": [{"target": "10.20.0.1", "passed": True}]}, required_targets=["10.30.0.10"])

    @unittest.skipUnless(available(), "Install workflow dependencies")
    def test_resolved_concept_followup_report_and_new_failure_across_reopens(self):
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp, patch.dict("os.environ", ENV):
            store = Store(Path(temp) / "sessions.sqlite3")
            database = Path(temp) / "workflow.sqlite3"
            first = store.save(self.payload, run_open_workflow(self.payload, database, completion))
            payload = deepcopy(self.payload)
            payload.update(session_id=first["session_id"], interaction={"type": "ask", "message": FEEDBACK})
            closed = store.save(payload, run_open_workflow(payload, database, reported))
            self.assertEqual(closed["status"], "resolved_by_user_report")
            self.assertFalse(closed["workflow"]["paused"])
            payload["interaction"] = {"type": "ask", "message": "为什么要重新获取租约？"}
            concept = store.save(payload, run_open_workflow(payload, database, completion))
            self.assertEqual(concept["status"], "resolved_by_user_report")
            self.assertEqual(concept["runtime_missing"], [])
            report = generate_report(store.history(first["session_id"]))
            self.assertIn("当前结论：用户反馈已恢复", report)
            self.assertIn(GOAL, report)
            self.assertIn(FEEDBACK, report)
            self.assertNotIn("## 下一步", report)
            self.assertIsNone(re.search(r"^##[^\n]*\n\s*(?=##|\Z)", report, re.M))
            failure = "PC1现在又ping不通10.30.0.10了。"
            payload["interaction"] = {"type": "ask", "message": failure}
            def reopen(messages):
                if messages[-1]["role"] != "tool":
                    return call("read_source", {"source_id": "message:current", "start_line": 1, "end_line": 1})
                return call("submit_hypotheses", {"summary": "目标再次不通，需要核对新输出。", "hypotheses": [{"title": "访问再次失败", "reasoning": "用户报告本轮目标不可达。", "evidence": [{"source_id": "message:current", "line": 1, "quote": failure}]}], "evidence_requests": [{"device": "PC1", "information": "本轮Ping输出", "why": "定位新现象"}]})
            reopened = store.save(payload, run_open_workflow(payload, database, reopen))
            self.assertEqual(reopened["status"], "needs_information")
            self.assertTrue(reopened["workflow"]["paused"])
            self.assertEqual(reopened["investigation"]["experiment_goal"], GOAL)
