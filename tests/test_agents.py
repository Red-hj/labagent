import os
import unittest
from unittest.mock import patch

from labagent.agents import run_diagnostic_agent
from labagent.engine import diagnose
from test_engine import case


ENV = {"LABAGENT_LLM_URL": "https://model.invalid/chat", "LABAGENT_LLM_MODEL": "test", "LABAGENT_LLM_KEY": "test-key"}


class AgentTests(unittest.TestCase):
    def test_tool_call_and_observation_roundtrip(self):
        payload = case("01_area_mismatch"); result = diagnose(payload)
        rounds = []
        def complete(messages):
            rounds.append(messages.copy())
            if len(rounds) == 1:
                return {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "inspect_link", "arguments": '{"link_id":"r1-r2"}'}}]}
            self.assertEqual(messages[-1]["role"], "tool")
            self.assertIn("AREA_MISMATCH", messages[-1]["content"])
            return {"role": "assistant", "content": "请比较互联两端的区域。"}
        with patch.dict(os.environ, ENV):
            response = run_diagnostic_agent(payload, result, complete)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["tool_trace"][0]["status"], "ok")
        self.assertEqual(response["rounds"], 2)

    def test_undeclared_tool_is_rejected_without_execution(self):
        def complete(messages):
            if messages[-1]["role"] == "tool":
                return {"content": "需要继续检查。"}
            return {"tool_calls": [{"id": "t1", "function": {"name": "run_shell", "arguments": '{"command":"anything"}'}}]}
        payload = case()
        with patch.dict(os.environ, ENV):
            response = run_diagnostic_agent(payload, diagnose(payload), complete)
        self.assertEqual(response["tool_trace"][0]["status"], "rejected")

    def test_infinite_model_loop_is_bounded(self):
        def complete(messages):
            return {"tool_calls": [{"id": "t", "function": {"name": "inspect_verification", "arguments": "{}"}}]}
        payload = case()
        with patch.dict(os.environ, ENV):
            response = run_diagnostic_agent(payload, diagnose(payload), complete)
        self.assertEqual(response["status"], "step_limit")
        self.assertEqual(len(response["tool_trace"]), 4)

    def test_authentication_text_is_not_sent_to_model_tools(self):
        payload = case("11_unsupported")
        def complete(messages):
            if messages[-1]["role"] == "tool":
                self.assertNotIn("DEMO_ONLY", messages[-1]["content"])
                return {"content": "认证设置需要人工确认。"}
            return {"tool_calls": [{"id": "t1", "function": {"name": "inspect_link", "arguments": '{"link_id":"r1-r2"}'}}]}
        with patch.dict(os.environ, ENV):
            result = run_diagnostic_agent(payload, diagnose(payload), complete)
        self.assertEqual(result["status"], "ok")

    def test_text_without_successful_tool_is_not_accepted(self):
        payload = case()
        with patch.dict(os.environ, ENV):
            result = run_diagnostic_agent(payload, diagnose(payload), lambda messages: {"content": "已经修复"})
        self.assertEqual(result["status"], "no_evidence")
        self.assertNotIn("text", result)
        self.assertFalse(result["evidence_tools_used"])

    def test_rejected_tools_do_not_count_as_evidence(self):
        def completion(messages):
            if messages[-1]["role"] == "tool":
                return {"content": "已经通过"}
            return {"tool_calls": [{"id": "bad", "function": {"name": "inspect_link", "arguments": '{"link_id":[]}'}}]}
        payload = case()
        with patch.dict(os.environ, ENV):
            result = run_diagnostic_agent(payload, diagnose(payload), completion)
        self.assertEqual(result["status"], "no_evidence")
        self.assertFalse(result["evidence_tools_used"])

    def test_first_hint_does_not_send_raw_answer_to_the_model(self):
        payload = case("01_area_mismatch")
        payload["hint_level"] = 1
        def completion(messages):
            if messages[-1]["role"] == "tool":
                evidence = messages[-1]["content"]
                self.assertNotIn("network 10.12", evidence)
                self.assertNotIn("area 1", evidence)
                self.assertNotIn('"actual"', evidence)
                self.assertIn("directions", evidence)
                return {"content": "请先比较互联两端的区域。"}
            return {"tool_calls": [{"id": "hint", "function": {"name": "inspect_link", "arguments": '{"link_id":"r1-r2"}'}}]}
        with patch.dict(os.environ, ENV):
            result = run_diagnostic_agent(payload, diagnose(payload), completion)
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
