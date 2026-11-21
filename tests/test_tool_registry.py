"""A registered experiment tool reaches the model, execution loop and progress UI."""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

from labagent import investigation
from labagent.progress import progress_to
from labagent.tool_registry import ToolRegistry
from test_investigation import ENV, completion, stages


class ToolRegistryTests(unittest.TestCase):
    def test_new_tool_is_advertised_executed_and_named_without_loop_changes(self):
        registry = ToolRegistry()
        for tool in investigation.tool_registry.tools.values():
            registry.register(tool.definition, tool.label)(tool.handler)
        definition = {"type": "function", "function": {"name": "inspect_test_observation",
            "description": "Test tool", "parameters": {"type": "object", "properties": {}}}}
        executions = []

        @registry.register(definition, "正在核对测试观测")
        def inspect(args, payload, sources, seen, **kwargs):
            executions.append(args)
            return {"observed": True}, False

        count, catalog = 0, []
        def model(messages, *, tools, **kwargs):
            nonlocal count
            count += 1
            catalog[:] = tools
            if count == 1:
                return {"tool_calls": [{"id": "new-tool", "function": {
                    "name": "inspect_test_observation", "arguments": "{}"}}]}
            return completion(messages if count > 2 else [{"role": "user"}])

        events = []
        with (patch.dict(os.environ, {**ENV, "LABAGENT_MCP_CONFIG": ""}),
              patch.object(investigation, "tool_registry", registry),
              patch.object(investigation, "chat_completion", side_effect=model),
              progress_to(events.append)):
            result = investigation.investigate(deepcopy(stages()[0]))
        self.assertIn(definition, catalog)
        self.assertEqual(executions, [{}])
        self.assertEqual(result["llm"]["status"], "ok")
        trace = result["llm"]["tool_trace"]
        self.assertEqual([row["tool"] for row in trace],
                         ["inspect_test_observation", "read_source", "submit_hypotheses"])
        tool_events = [event for event in events if event.get("item", {}).get("id") == trace[0]["item_id"]]
        self.assertEqual(tool_events[0]["message"], "正在核对测试观测")
        self.assertEqual(tool_events[-1]["item"]["status"], "completed")
        self.assertEqual(result["investigation"]["hypotheses"][0]["evidence"][0]["quote"],
                         " gateway-list 10.20.0.254")

    def test_duplicate_registration_does_not_replace_the_existing_handler(self):
        registry = ToolRegistry()
        definition = {"function": {"name": "read"}}
        registry.register(definition, "read")(lambda: "original")
        with self.assertRaises(ValueError):
            registry.register(definition, "replacement")(lambda: "replacement")
        self.assertEqual(registry.execute("read"), "original")
        with self.assertRaisesRegex(ValueError, "不支持的工具"):
            registry.execute("missing")
