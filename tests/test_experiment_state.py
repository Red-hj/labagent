"""Continuity beyond the conversation window without reusing stale source IDs."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.experiment_state import experiment_context
from labagent.investigation import investigate, source_bundle, validate_submission
from labagent.workflow import available, run_open_workflow


ENV = {"LABAGENT_LLM_URL": "https://model.invalid/chat", "LABAGENT_LLM_MODEL": "test", "LABAGENT_LLM_KEY": "test-key"}
ROOT = Path(__file__).resolve().parents[1]


class ExperimentStateTests(unittest.TestCase):
    def setUp(self):
        self.payload = {"diagnostic_mode": "open_network", "configs": {"R1.log": "Interface GE0/0/0 physical up protocol up"},
                        "question": "让PC1按实验要求访问PC2", "use_llm": True}

    def choose(self, *, exclude=False, inspect=None):
        def completion(messages):
            if messages[-1]["role"] != "tool":
                if inspect:
                    inspect(json.loads(messages[1]["content"]))
                name, args = "read_source", {"source_id": "config:1", "start_line": 1, "end_line": 1}
            else:
                name = "submit_hypotheses"
                args = {"summary": "接口已启用，下一步核对转发。", "experiment_goal": "让PC1按实验要求访问PC2",
                        "hypotheses": [], "evidence_requests": [{"device": "R1", "information": "提供当前路由表", "why": "核对实际选路"}]}
                if exclude:
                    args["hypotheses"] = [{"title": "物理接口关闭", "reasoning": "提交输出显示接口物理和协议均up。",
                                           "state": "ruled_out", "evidence": [{"source_id": "config:1", "line": 1,
                                           "quote": self.payload["configs"]["R1.log"]}]}]
            return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
        return completion

    def first(self):
        with patch.dict(os.environ, ENV):
            return investigate(self.payload, completion=self.choose(exclude=True))

    @unittest.skipUnless(available(), "Install workflow dependencies")
    def test_exclusions_and_goal_survive_ten_turns_and_checkpoint_reopens(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp, patch.dict(os.environ, ENV):
            database = Path(temp) / "state.sqlite3"
            first = run_open_workflow(self.payload, database, self.choose(exclude=True))
            followup = deepcopy(self.payload)
            followup["session_id"] = first["workflow"]["thread_id"]
            for turn in range(2, 12):
                followup["interaction"] = {"type": "ask", "message": f"继续第{turn}轮"}
                def check(context):
                    state = context["experiment_state"]
                    self.assertEqual(state["turn_count"], turn - 1)
                    self.assertEqual(state["goal"], self.payload["question"])
                    self.assertEqual(state["ruled_out"][0]["observed_turn"], 1)
                    self.assertEqual(state["ruled_out"][0]["evidence_state"], "materials_unchanged")
                    self.assertNotIn("source_id", json.dumps(state))
                result = run_open_workflow(followup, database, self.choose(inspect=check))
                self.assertEqual(result["llm"]["status"], "ok")
            self.assertEqual(len(result["conversation"]), 8)
            self.assertEqual(result["investigation"]["experiment_state"]["turn_count"], 11)

    def test_material_reordering_is_safe_and_updates_expire_old_exclusion(self):
        previous = self.first()["investigation"]
        payload = deepcopy(self.payload)
        payload["configs"] = {"R2.log": "new unrelated output", **payload["configs"]}
        context = experiment_context(previous, payload, self.payload["configs"])
        self.assertEqual(context["ruled_out"][0]["evidence_state"], "materials_unchanged")
        for configs in ({"R1.log": "Interface GE0/0/0 physical down protocol down"}, {"renamed.log": self.payload["configs"]["R1.log"]}, {}):
            payload["configs"] = configs
            expired = experiment_context(previous, payload, self.payload["configs"])
            self.assertEqual(expired["ruled_out"][0]["evidence_state"], "needs_recheck")
            restored = experiment_context({"experiment_state": expired}, self.payload, configs)
            self.assertEqual(restored["ruled_out"][0]["evidence_state"], "needs_recheck")
        self.assertEqual(previous["experiment_state"]["ruled_out"][0]["evidence_state"], "materials_unchanged")

    def test_failed_turn_preserves_goal_pending_work_and_exclusions(self):
        previous = self.first()["investigation"]
        payload = deepcopy(self.payload)
        payload["configs"]["R1.log"] += "\nNew output"
        with patch('labagent.investigation.run_investigator', return_value={"status": "unavailable", "tool_trace": []}):
            failed = investigate(payload, previous, previous_materials=self.payload["configs"])
        state = failed["investigation"]["experiment_state"]
        self.assertEqual(state["turn_count"], 1)
        self.assertEqual(state["pending_requests"], previous["pending_requests"])
        self.assertEqual(state["ruled_out"][0]["evidence_state"], "needs_recheck")
        with patch.dict(os.environ, ENV):
            recovered = investigate(payload, failed["investigation"], completion=self.choose(), previous_materials=payload["configs"])
        self.assertEqual(recovered["investigation"]["experiment_goal"], self.payload["question"])
        self.assertEqual(recovered["investigation"]["experiment_state"]["turn_count"], 2)

    def test_historical_message_exclusion_never_becomes_current_material(self):
        payload = deepcopy(self.payload)
        payload["interaction"] = {"type": "ask", "message": "我刚才看到接口是up"}
        sources, _ = source_bundle(payload)
        args = {"summary": "根据本轮反馈继续核对。", "hypotheses": [{"title": "接口关闭", "reasoning": "用户口述接口up。", "state": "ruled_out",
                "evidence": [{"source_id": "message:current", "line": 1, "quote": payload["interaction"]["message"]}]}], "evidence_requests": []}
        checked = validate_submission(args, sources, {("message:current", 1)}, conversational=True)
        with patch('labagent.investigation.run_investigator', return_value={"status": "ok", "investigation": checked, "tool_trace": []}):
            result = investigate(payload)
        state = result["investigation"]["experiment_state"]
        self.assertEqual(state["ruled_out"][0]["evidence_state"], "historical_report")
        self.assertEqual(result["investigation"]["hypotheses"], [])

    def test_continuity_does_not_restore_solution_permission(self):
        previous = self.first()["investigation"]
        previous["experiment_state"]["learning"] = {"level": 3, "mode": "worked_solution"}
        payload = deepcopy(self.payload)
        payload["learning"] = {"level": 3}
        with patch.dict(os.environ, {"LABAGENT_LEARNING_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "授权"):
                investigate(payload, previous)


if __name__ == "__main__":
    unittest.main()
