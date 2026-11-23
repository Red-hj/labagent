"""Current web materials replace cloud inputs; historical evidence remains in history."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.cloud_workflow import CloudWorkflow
from labagent.material_service import MaterialService
from labagent.store import Store

ROOT = Path(__file__).resolve().parents[1]
ENV = {"LABAGENT_LLM_URL": "https://model.invalid/chat", "LABAGENT_LLM_MODEL": "test",
       "LABAGENT_LLM_KEY": "test-key", "LABAGENT_MCP_CONFIG": ""}


def call(name, args):
    return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args)}}]}


class MaterialSnapshotTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        environment = patch.dict(os.environ, ENV)
        environment.start()
        self.addCleanup(environment.stop)
        self.prompts = []
        self.service = MaterialService(self.folder / "cloud.sqlite3", self.complete)

    def complete(self, messages):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        self.assertIn("submit_learning_step", prompt)
        for incompatible in ("submit_hypotheses", "answer_mode", "ready_for_retest", "operation_focus"):
            self.assertNotIn(incompatible, prompt)
        context = json.loads(messages[1]["content"])
        material = next((item for item in context["materials"] if item["source_id"].startswith("config:")), None)
        if messages[-1]["role"] != "tool":
            if context["interaction"].get("type") == "verify":
                return call("compare_materials", {})
        if material and (messages[-1]["role"] != "tool" or messages[-1].get("tool_call_id") == "compare_materials"):
            return call("read_source", {"source_id": material["source_id"], "start_line": 1, "end_line": material["line_count"]})
        evidence = []
        if material:
            observed = json.loads(messages[-1]["content"])
            row = observed["lines"][-1]
            evidence = [{"source_id": material["source_id"], "line": row["line"], "quote": row["text"]}]
        return call("submit_learning_step", {"stage": "observe", "current_step": "核对当前接口输出",
            "explanation": "结合本轮接口输出继续排查。", "action": "在 R2 查看 display ip interface brief。",
            "expected_observation": "找到对接接口的 Physical 和 Protocol 列。", "question": "R2 的对接接口状态是什么？", "evidence": evidence})

    def invoke(self, materials, **extra):
        return self.service.invoke({"materials": materials, "question": "实验目标是两端建立邻居，先核对当前接口。",
            "learning": {"level": 1}, **extra})

    def test_web_snapshot_removes_old_outputs_from_current_inputs_and_citations(self):
        web = Store(self.folder / "web.sqlite3")
        service = self.service
        class Client:
            def invoke(self, request):
                return service.invoke(request)
        workflow = CloudWorkflow(web, client_factory=lambda platform: Client())
        first_input = {"diagnostic_mode": "open_network", "question": "先核对当前接口", "configs": {"old.log": "R1 G0/0/0 down down"},
                       "use_llm": True, "learning": {"level": 1}}
        first = web.save(first_input, workflow.run(first_input))
        next_input = {**first_input, "session_id": first["session_id"], "configs": {"current.log": "R1 G0/0/0 up up"},
                      "interaction": {"type": "verify", "message": "核对接口变化并给下一步。"}}
        second = workflow.run(next_input)
        history = service.store.history(second["agentarts"]["session_id"])
        self.assertEqual(history["attempts"][-1]["input"]["configs"], next_input["configs"])
        self.assertEqual(history["attempts"][0]["input"]["configs"], first_input["configs"])
        self.assertEqual(second["investigation"]["learning"]["evidence"][0]["device"], "current.log")
        diff = second["interaction_checks"]["compare_materials"]
        self.assertTrue(diff["changed"])
        self.assertIn("down down", str(diff))
        self.assertIn("up up", str(diff))
        self.assertEqual([item["tool"] for item in second["llm"]["tool_trace"]],
                         ["compare_materials", "read_source", "submit_learning_step"])
        self.assertNotEqual(second["investigation"]["outcome"], "observations_passed")

    def test_direct_clients_keep_incremental_merge_by_default(self):
        first = self.invoke({"R1.log": "R1 up up"})
        second = self.invoke({"R2.log": "R2 down down"}, session_id=first["session_id"])
        saved = self.service.store.history(second["session_id"])["attempts"][-1]["input"]["configs"]
        self.assertEqual(saved, {"R1.log": "R1 up up", "R2.log": "R2 down down"})
        self.assertFalse(second["materials_replaced"])

    def test_empty_snapshot_clears_current_materials_but_keeps_history(self):
        first = self.invoke({"old.log": "R1 down down"})
        second = self.invoke({}, material_mode="replace", session_id=first["session_id"])
        history = self.service.store.history(second["session_id"])
        self.assertEqual(history["attempts"][-1]["input"]["configs"], {})
        self.assertEqual(history["attempts"][0]["input"]["configs"], {"old.log": "R1 down down"})
        self.assertTrue(second["materials_replaced"])

    def test_recovery_replaces_saved_inputs_instead_of_reintroducing_removed_materials(self):
        first = self.invoke({"old.log": "R1 down down"})
        previous = self.service.store.history(first["session_id"])["attempts"][-1]
        second = self.invoke({"current.log": "R1 up up"}, material_mode="replace", resume_from=deepcopy(previous))
        saved = self.service.store.history(second["session_id"])["attempts"][-1]["input"]["configs"]
        self.assertEqual(saved, {"current.log": "R1 up up"})
        self.assertTrue(second["session_recovered"])

    def test_invalid_mode_is_rejected_before_model_or_history_mutation(self):
        first = self.invoke({"R1.log": "R1 up up"})
        count = len(self.prompts)
        with self.assertRaisesRegex(ValueError, "material_mode"):
            self.invoke({}, session_id=first["session_id"], material_mode="unknown")
        self.assertEqual(len(self.prompts), count)
        self.assertEqual(len(self.service.store.history(first["session_id"])["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
