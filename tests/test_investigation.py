from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.engine import validate, fingerprint
from labagent.investigation import source_bundle, execute_tool, validate_submission, investigate
from labagent.workflow import available, run_open_workflow
from labagent.store import Store
from labagent.report import generate_report

ROOT = Path(__file__).resolve().parents[1]
ENV = {"LABAGENT_LLM_URL": "https://model.invalid/chat", "LABAGENT_LLM_MODEL": "test", "LABAGENT_LLM_KEY": "test-key"}


def stages():
    return json.loads((ROOT / "data/scenarios/open_network.json").read_text(encoding="utf-8"))["stages"]


def submission(quote=" gateway-list 10.20.0.254", line=8):
    return {"summary": "网关设置存在需要核对的疑点。", "hypotheses": [{"title": "网关归属尚需确认", "reasoning": "不能根据单份配置排除VRRP，应补充网关归属与可达性。",
            "evidence": [{"source_id": "config:1", "line": line, "quote": quote}]}],
            "evidence_requests": [{"device": "SW1", "information": "提供网关归属、VRRP和路由信息", "why": "验证地址是否属于实际网关"}]}


def completion(messages):
    if messages[-1]["role"] != "tool":
        return {"tool_calls": [{"id": "read", "function": {"name": "read_source", "arguments": json.dumps({"source_id": "config:1", "start_line": 1, "end_line": 10})}}]}
    observation = json.loads(messages[-1]["content"])
    line = next(item for item in observation["lines"] if "gateway-list" in item["text"])
    return {"tool_calls": [{"id": "submit", "function": {"name": "submit_hypotheses", "arguments": json.dumps(submission(line["text"], line["line"]), ensure_ascii=False)}}]}


class OpenInvestigationTests(unittest.TestCase):
    def setUp(self):
        self.payload = stages()[0]
        self.sources, _ = source_bundle(self.payload)

    def test_no_ospf_topology_or_fault_type_is_required(self):
        validate(self.payload)
        with patch.dict(os.environ, ENV):
            result = investigate(self.payload, completion=completion)
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertEqual(result["status"], "needs_information")
        self.assertEqual(result["findings"], [])
        hypothesis = result["investigation"]["hypotheses"][0]
        self.assertEqual(hypothesis["state"], "unverified")
        self.assertNotIn("code", hypothesis)
        self.assertEqual(hypothesis["evidence"][0]["quote"], " gateway-list 10.20.0.254")

    def test_cannot_cite_source_before_reading_it(self):
        with self.assertRaises(ValueError):
            validate_submission(submission(), self.sources, set())

    def test_rejects_fabricated_quotes_and_non_integer_line(self):
        for value in (submission("gateway-list 10.20.0.254"), submission("invented"), submission(line=True)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_submission(value, self.sources, {("config:1", 8)})

    def test_cannot_claim_confirmed_or_override_status(self):
        value = submission()
        value["hypotheses"][0]["state"] = "confirmed"
        with self.assertRaises(ValueError):
            validate_submission(value, self.sources, {("config:1", 8)})

    def test_empty_hypotheses_allowed_with_explicit_evidence_request(self):
        value = submission(); value["hypotheses"] = []
        result = validate_submission(value, self.sources, set())
        self.assertEqual(result["hypotheses"], [])
        self.assertTrue(result["evidence_requests"])
        value["evidence_requests"] = []
        with self.assertRaises(ValueError):
            validate_submission(value, self.sources, set())

    def test_source_reads_redact_only_sensitive_lines_and_cannot_cite_them(self):
        self.payload["configs"]["SW1"] = "public line\n password my-private-password\n gateway-list 10.20.0.1\n"
        sources, _ = source_bundle(self.payload)
        seen = set()
        result, _ = execute_tool("read_source", {"source_id": "config:1", "start_line": 1, "end_line": 3}, self.payload, sources, seen)
        self.assertNotIn("my-private-password", json.dumps(result))
        self.assertEqual(result["lines"][2]["text"], " gateway-list 10.20.0.1")
        with self.assertRaises(ValueError):
            validate_submission(submission("[敏感内容已省略]", 2), sources, seen)

    def test_cannot_read_file_paths_or_unlisted_sources(self):
        for source_id in ("../../.env", "config:99"):
            with self.subTest(source_id=source_id), self.assertRaises(ValueError):
                execute_tool("read_source", {"source_id": source_id, "start_line": 1, "end_line": 2}, self.payload, self.sources, set())

    def test_source_window_is_bounded(self):
        for first, last in ((0, 1), (1, 100), (True, 3)):
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                execute_tool("read_source", {"source_id": "config:1", "start_line": first, "end_line": last}, self.payload, self.sources, set())

    def test_stale_outputs_not_available_to_model(self):
        self.payload["command_outputs"] = [{"device": "SW1", "command": "show anything", "text": "old data", "config_fingerprint": "old"}]
        sources, warnings = source_bundle(self.payload)
        self.assertNotIn("output:1", sources)
        self.assertTrue(warnings)
        self.payload["command_outputs"][0]["config_fingerprint"] = fingerprint(self.payload)
        sources, _ = source_bundle(self.payload)
        self.assertIn("output:1", sources)

    def test_old_quote_invalid_after_source_changes(self):
        sources, _ = source_bundle(stages()[1])
        with self.assertRaises(ValueError):
            validate_submission(submission(), sources, {("config:1", 8)})

    def test_text_only_model_reply_is_not_accepted(self):
        with patch.dict(os.environ, ENV):
            result = investigate(self.payload, completion=lambda _: {"content": "已修复"})
        self.assertEqual(result["llm"]["status"], "no_evidence")
        self.assertFalse(result["investigation"])
        self.assertNotIn("text", result["llm"])

    def test_unapproved_tool_is_rejected_and_loop_is_bounded(self):
        def malicious(_):
            return {"tool_calls": [{"id": "bad", "function": {"name": "run_shell", "arguments": '{"command":"anything"}'}}]}
        with patch.dict(os.environ, ENV):
            result = investigate(self.payload, completion=malicious)
        self.assertEqual(result["llm"]["status"], "step_limit")
        self.assertEqual(len(result["llm"]["tool_trace"]), 6)
        self.assertTrue(all(item["status"] == "rejected" for item in result["llm"]["tool_trace"]))

    def test_without_model_remains_unverified(self):
        self.payload["use_llm"] = False
        result = investigate(self.payload)
        self.assertEqual(result["llm"]["status"], "not_configured")
        self.assertEqual(result["status"], "needs_information")

    def test_retrieved_references_are_available_to_ui_and_report(self):
        def choose(messages):
            if messages[-1]["role"] != "tool":
                return completion(messages)
            observation = json.loads(messages[-1]["content"])
            if observation.get("source_id"):
                return {"tool_calls": [{"id": "reference", "function": {"name": "retrieve_reference", "arguments": '{"query":"网关排查"}'}}]}
            return {"tool_calls": [{"id": "submit", "function": {"name": "submit_hypotheses", "arguments": json.dumps(submission())}}]}
        found = {"results": [{"id": "course", "chunk_id": "course:1", "title": "网关排查", "content": "比较终端与网关地址。"}], "warnings": []}
        with patch.dict(os.environ, ENV), patch("labagent.investigation.search", return_value=found):
            result = investigate(self.payload, completion=choose)
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertEqual(result["knowledge"][0]["chunk_id"], "course:1")

    def test_compare_materials_returns_real_changes(self):
        newer = stages()[1]
        sources, _ = source_bundle(newer)
        result, terminal = execute_tool("compare_materials", {}, newer, sources, set(), previous_materials=self.payload["configs"])
        self.assertFalse(terminal)
        self.assertTrue(result["changed"])
        diff = next(item["diff"] for item in result["changes"] if item["material"] == "SW1")
        self.assertIn("- gateway-list 10.20.0.254", diff)
        self.assertIn("+ gateway-list 10.20.0.1", diff)
        unchanged, _ = execute_tool("compare_materials", {}, newer, sources, set(), previous_materials=newer["configs"])
        self.assertFalse(unchanged["changed"])

    def test_ping_tool_checks_uploaded_counts_and_registers_citations(self):
        self.payload["configs"] = {"记录": "PC1执行ping 10.20.0.1：发送4个，收到4个。\nPing 10.30.0.10: 4 packets transmitted, 0 received\nPing 10.0.0.1: 0 packets transmitted, 0 received"}
        sources, _ = source_bundle(self.payload)
        seen = set()
        checked, _ = execute_tool("check_submitted_pings", {}, self.payload, sources, seen)
        self.assertEqual([item["passed"] for item in checked["pings"]], [True, False])
        self.assertEqual(seen, {("config:1", 1), ("config:1", 2)})
        self.assertEqual(checked["source"], "uploaded_text")

    def test_followup_can_answer_without_repeating_material_requests(self):
        value = submission(); value["evidence_requests"] = []; value["reply"] = "请先核对网关归属。"; value["steps"] = ["对照实验网关规划。"]
        result = validate_submission(value, self.sources, {("config:1", 8)}, conversational=True)
        self.assertEqual(result["reply"], value["reply"])
        self.assertEqual(result["steps"], value["steps"])

    @unittest.skipUnless(available(), "Install workflow dependencies")
    def test_followup_keeps_conversation_across_graph_reopens(self):
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp, patch.dict(os.environ, ENV):
            database = Path(temp) / "graph.sqlite3"
            first = run_open_workflow(self.payload, database, completion)
            followup = deepcopy(self.payload)
            followup["session_id"] = first["workflow"]["thread_id"]
            followup["interaction"] = {"type": "ask", "message": "为什么这个网关有疑点？"}
            def choose(messages):
                if messages[-1]["role"] != "tool":
                    context = json.loads(messages[1]["content"])
                    self.assertEqual(len(context["conversation"]), 1)
                    self.assertEqual(context["interaction"]["message"], followup["interaction"]["message"])
                    return completion(messages)
                answer = submission(); answer["reply"] = "配置网关与实际接口不同，需要确认地址归属。"; answer["evidence_requests"] = []
                return {"tool_calls": [{"id": "answer", "function": {"name": "submit_hypotheses", "arguments": json.dumps(answer)}}]}
            second = run_open_workflow(followup, database, choose)
            self.assertTrue(second["workflow"]["resumed"])
            self.assertEqual(second["llm"]["status"], "ok")
            self.assertEqual(len(second["conversation"]), 2)
            self.assertEqual(second["conversation"][-1]["user"], followup["interaction"]["message"])
            self.assertTrue(second["investigation"]["pending_requests"])

    def test_invalid_interaction_rejected_before_model_call(self):
        for interaction in ({"type": "unknown", "message": "test"}, {"type": "ask", "message": ""}):
            self.payload["interaction"] = interaction
            with self.subTest(interaction=interaction), self.assertRaises(ValueError):
                validate(self.payload)

    def test_completed_outcome_requires_actual_passing_ping_targets(self):
        value = submission(); value.update(hypotheses=[], evidence_requests=[], outcome="observations_passed", verification_targets=["10.30.0.10"])
        with self.assertRaises(ValueError):
            validate_submission(value, self.sources, set(), conversational=True, verifying=True)
        passing = {"pings": [{"target": "10.30.0.10", "passed": True}]}
        checked = validate_submission(value, self.sources, set(), conversational=True, verifying=True, checked_pings=passing)
        self.assertEqual(checked["outcome"], "observations_passed")
        self.payload["interaction"] = {"type": "ask", "message": "解释已经通过的观测"}
        _, terminal = execute_tool("submit_hypotheses", value, self.payload, self.sources, {("config:1", 8)}, checked_pings=passing)
        self.assertTrue(terminal)
        failing = {"pings": [{"target": "10.30.0.10", "passed": False}]}
        with self.assertRaises(ValueError):
            validate_submission(value, self.sources, set(), conversational=True, verifying=True, checked_pings=failing)

    @unittest.skipUnless(available(), "Install workflow dependencies")
    def test_verified_observations_finish_graph_and_concept_question_stays_complete(self):
        payload = stages()[1]
        payload["configs"]["PC1"] = "Ping 10.30.0.10: 4 packets transmitted, 4 received"
        payload["interaction"] = {"type": "verify", "message": "核对本次上传观测"}
        def verify(messages):
            if messages[-1]["role"] != "tool":
                return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": "{}"}} for name in ("compare_materials", "check_submitted_pings")]}
            args = {"summary": "本次目标观测通过", "hypotheses": [], "evidence_requests": [], "outcome": "observations_passed", "verification_targets": ["10.30.0.10"]}
            return {"tool_calls": [{"id": "finish", "function": {"name": "submit_hypotheses", "arguments": json.dumps(args)}}]}
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp, patch.dict(os.environ, ENV):
            database = Path(temp) / "graph.sqlite3"
            first = run_open_workflow(payload, database, verify)
            self.assertEqual(first["status"], "verified_from_observations")
            self.assertFalse(first["workflow"]["paused"])
            payload["session_id"] = first["workflow"]["thread_id"]
            payload["interaction"] = {"type": "ask", "message": "为什么要检查回程？"}
            second = run_open_workflow(payload, database, completion)
            self.assertEqual(second["status"], "verified_from_observations")
            self.assertFalse(second["workflow"]["paused"])
            self.assertEqual(len(second["conversation"]), 2)

    @unittest.skipUnless(available(), "Install workflow dependencies")
    def test_graph_pause_resume_refreshes_evidence_and_report(self):
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            database = Path(temp) / "graph.sqlite3"
            store = Store(Path(temp) / "sessions.sqlite3")
            with patch.dict(os.environ, ENV):
                first = store.save(self.payload, run_open_workflow(self.payload, database, completion))
                second = stages()[1]; second["session_id"] = first["session_id"]
                resumed = store.save(second, run_open_workflow(second, database, completion))
            self.assertTrue(first["workflow"]["paused"])
            self.assertFalse(first["workflow"]["resumed"])
            self.assertTrue(resumed["workflow"]["resumed"])
            self.assertTrue(resumed["workflow"]["paused"])
            old = first["investigation"]["hypotheses"][0]["evidence"][0]
            new = resumed["investigation"]["hypotheses"][0]["evidence"][0]
            self.assertNotEqual(old["quote"], new["quote"])
            self.assertEqual(new["quote"], " gateway-list 10.20.0.1")
            report = generate_report(store.history(first["session_id"]))
            self.assertIn("排查方向与原文依据", report)
            self.assertIn("gateway-list 10.20.0.254", report)
            self.assertIn("gateway-list 10.20.0.1", report)
            self.assertNotIn("本次在受支持的检查范围内未检出故障", report)


if __name__ == "__main__":
    unittest.main()
