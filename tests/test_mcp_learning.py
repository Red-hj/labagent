from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from labagent.mcp_client import StdioMCPClient, LabMCPTools, MCPError
from labagent.learning import grant_solution, solution_allowed, policy, validate_step
from labagent.investigation import investigate
from labagent.workflow import run_open_workflow
from labagent.store import Store

ROOT = Path(__file__).resolve().parents[1]
ENV = {"LABAGENT_LLM_URL": "https://model.invalid/chat", "LABAGENT_LLM_MODEL": "test", "LABAGENT_LLM_KEY": "test-key",
       "LABAGENT_SOLUTION_CODE": "test-teacher-code", "LABAGENT_LEARNING_KEY": "test-signing-key"}


def step(**changes):
    return {"stage": "try", "current_step": "先找到接口状态", "explanation": "先观察接口状态，不需要一次做完整个实验。",
        "action": "在R1输入 display ip interface brief。", "expected_observation": "找到连接R2的接口，观察它是否为up。",
        "question": "你看到哪一行？把这一行发回来。", "evidence": [], **changes}


def call(name, args):
    return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def fixture_spec():
    return {"command": sys.executable, "args": ["-B", str(ROOT / "tests/helpers/mcp_lab_fixture.py")],
        "tools": {"r1": {"name": "device_exec", "device": "R1", "effect": "observe", "arguments": {"target": "R1"},
                         "choices": {"command": ["display ip interface brief"]}}}}


class MCPLearningTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.folder = Path(self.temp.name)
        self.config = self.folder / "mcp.json"
        self.config.write_text(json.dumps({"servers": {"test": fixture_spec()}}), encoding="utf-8")
        self.environment = patch.dict(os.environ, {**ENV, "LABAGENT_MCP_CONFIG": ""})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_real_stdio_handshake_pagination_notifications_and_reverse_ping(self):
        with StdioMCPClient(fixture_spec()) as client:
            self.assertEqual([tool["name"] for tool in client.list_tools()], ["device_exec", "other_check"])
            text = client.call_tool("device_exec", {"target": "R1", "command": "display ip interface brief"})
            self.assertIn("10.12.0.1/24 up up", text)
        self.assertIsNotNone(client.process.poll())

    def test_tool_failure_is_not_a_successful_observation(self):
        with StdioMCPClient(fixture_spec()) as client:
            with self.assertRaisesRegex(MCPError, "执行失败"):
                client.call_tool("device_exec", {"command": "fail"})

    def test_timeout_closes_connection_without_replaying(self):
        with StdioMCPClient(fixture_spec(), timeout=.5) as client:
            with self.assertRaisesRegex(MCPError, "未自动重复"):
                client.call_tool("device_exec", {"command": "slow"})
            self.assertIsNotNone(client.process.poll())

    def test_model_cannot_override_bound_device_or_select_unlisted_commands(self):
        with LabMCPTools(self.config) as tools:
            self.assertEqual(len(tools.tools), 1)
            for arguments in ({"target": "R2", "command": "display ip interface brief"}, {"command": "system-view"}):
                with self.assertRaises(MCPError):
                    tools.execute("lab_test_r1", arguments)

    def test_disconnected_mcp_keeps_uploaded_material_diagnosis_available(self):
        config = json.loads(self.config.read_text())
        config["servers"]["test"]["command"] = str(self.folder / "not-installed.exe")
        self.config.write_text(json.dumps(config))
        with LabMCPTools(self.config) as tools:
            self.assertEqual(tools.tools, [])
            self.assertTrue(tools.warnings)

    def test_mcp_observation_becomes_a_citable_material_in_same_round(self):
        def complete(messages):
            if messages[-1]["role"] != "tool":
                return call("lab_test_r1", {"command": "display ip interface brief"})
            observed = json.loads(messages[-1]["content"])
            row = observed["lines"][-1]
            return call("submit_learning_step", step(evidence=[{"source_id": observed["source_id"], "line": row["line"], "quote": row["text"]}]))
        payload = {"configs": {}, "question": "我做到接口配置了，接下来怎么看？", "use_llm": True, "learning": {"level": 1}}
        with patch.dict(os.environ, {"LABAGENT_MCP_CONFIG": str(self.config)}):
            result = investigate(payload, completion=complete)
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertEqual(result["llm"]["tool_workflow"]["nodes"], ["select_check", "execute_check"] * 2 + ["review_learning_step"])
        evidence = result["investigation"]["learning"]["evidence"][0]
        self.assertEqual(payload["configs"][evidence["device"]].splitlines()[evidence["line"] - 1], evidence["quote"])
        self.assertTrue(result["collected_materials"])
        self.assertEqual(result["status"], "needs_information")

    def test_identical_mcp_recollection_outranks_a_later_old_upload(self):
        command = "ip route show table main"
        material = "MCP-R1-lab_test_r1-ip_route_show_table_main.log"
        spec = fixture_spec()
        spec["tools"]["r1"]["choices"]["command"] = [command]
        self.config.write_text(json.dumps({"servers": {"test": spec}}), encoding="utf-8")
        raw = (ROOT / "data/team_cases/lab_02/map_materials/repaired/R1-run.log").read_text(encoding="utf-8")
        table = raw.split(f"<R1>{command}\n", 1)[1].split("\n<R1>", 1)[0]
        earlier = f"设备：R1\n命令：{command}\n{table}"
        manual = f"<R1>{command}\n" + table.replace("proto ospf", "proto static")
        previous = {material: earlier, "R1-old-upload.log": manual}
        payload = {"configs": dict(previous), "question": "请重新读取 R1 到 192.168.20.10 的路由。", "use_llm": True,
                   "learning": {"level": 1}, "interaction": {"type": "verify", "message": "核对现在的路由来源。"}}

        def complete(messages):
            tools = [item["tool_call_id"] for item in messages if item["role"] == "tool"]
            if not tools:
                return call("lab_test_r1", {"command": command})
            if len(tools) == 1:
                return call("check_route_selection", {"device": "R1", "target": "192.168.20.10"})
            return call("submit_learning_step", step())

        with patch.dict(os.environ, {"LABAGENT_MCP_CONFIG": str(self.config)}):
            result = investigate(payload, completion=complete, previous_materials=previous)
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertEqual(result["collected_materials"], {material: earlier})
        latest, = result["interaction_checks"]["check_route_selection"]
        self.assertEqual(latest["routes"][0]["protocol"], "OSPF")
        ref, = latest["evidence"]
        self.assertEqual(ref["device"], material)
        self.assertEqual(ref["quote"], earlier.splitlines()[ref["line"] - 1])

    def test_student_can_ask_halfway_without_materials_or_a_fault(self):
        result = investigate({"configs": {}, "question": "接口已经连好了，我不会看状态", "use_llm": True, "learning": {"level": 1}},
            completion=lambda messages: call("submit_learning_step", step()))
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertEqual(result["investigation"]["learning"]["current_step"], "先找到接口状态")
        self.assertEqual(result["investigation"]["hypotheses"], [])

    def test_level_one_rejects_direct_repair_and_plain_diagnosis_submission(self):
        payload = {"configs": {}, "question": "直接给我答案", "use_llm": True, "learning": {"level": 1}}
        for name, args in (("submit_learning_step", step(action="ip address 10.12.0.1 255.255.255.0")),
                           ("submit_learning_step", None),
                           ("submit_hypotheses", {"summary": "完整答案"})):
            with self.subTest(name=name):
                result = investigate(deepcopy(payload), completion=lambda messages: call(name, args))
                self.assertNotEqual(result["llm"]["status"], "ok")
                self.assertNotIn("完整答案", result["investigation"].get("reply", ""))

    def test_cannot_complete_on_model_claim_without_goal_checks(self):
        with self.assertRaisesRegex(ValueError, "全部通过"):
            validate_step(step(stage="completed"), {"level": 1}, lambda ref: ref, {})

    def test_signed_solution_grant_is_subject_bound_expires_and_rejects_forgery(self):
        token = grant_solution("test-teacher-code", "student-a", now=100)
        self.assertTrue(solution_allowed(token, subject="student-a", now=101))
        self.assertFalse(solution_allowed(token, subject="student-b", now=101))
        self.assertFalse(solution_allowed(token, now=7400))
        self.assertFalse(solution_allowed(token + "0", now=101))
        with self.assertRaises(ValueError):
            policy({"learning": {"level": 3, "authorized": True}})
        with self.assertRaises(ValueError):
            policy({"learning": {"level": 3}})

    def test_guidance_resumes_same_session_and_does_not_restore_solution_privilege(self):
        store = Store(self.folder / "sessions.sqlite3")
        token = grant_solution("test-teacher-code", "student-a")
        payload = {"diagnostic_mode": "open_network", "configs": {}, "question": "接口这一句怎么写", "use_llm": True,
                   "learning": {"level": 3, "access_token": token}}
        first = store.save(payload, run_open_workflow(payload, self.folder / "workflow.sqlite3",
            lambda messages: call("submit_learning_step", step(action="ip address 10.12.0.1 255.255.255.0"))))
        history = store.history(first["session_id"])
        self.assertNotIn(token, json.dumps(history))
        self.assertNotIn("access_token", payload["learning"])
        next_payload = {**payload, "session_id": first["session_id"], "learning": {"level": 1},
                        "interaction": {"type": "ask", "message": "我还是不会看up，在哪里看？"}}
        second = store.save(next_payload, run_open_workflow(next_payload, self.folder / "workflow.sqlite3",
            lambda messages: call("submit_learning_step", step(current_step="找到Physical列", action="看输出的Physical列；先不用再改配置。"))))
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(len(store.history(first["session_id"])["attempts"]), 2)
        self.assertEqual(second["investigation"]["learning"]["level"], 1)
        with self.assertRaises(ValueError):
            run_open_workflow({**next_payload, "learning": {"level": 3}}, self.folder / "workflow.sqlite3")


if __name__ == "__main__":
    unittest.main()
