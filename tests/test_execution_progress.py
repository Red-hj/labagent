"""Execution identities, reviewed tool lifecycles and actual streaming handlers."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.investigation import investigate
from labagent.progress import emit_progress, progress_to, ToolProgress
from labagent.server import create_server, Handler
from test_investigation import ENV, completion, stages
from test_mcp_learning import call, step
from test_personal import RequestSocket

ROOT = Path(__file__).resolve().parents[1]


class ExecutionProgressTests(unittest.TestCase):
    def test_nested_and_concurrent_requests_keep_distinct_event_sequences(self):
        def run(_):
            events = []
            with progress_to(events.append, session_id="session"):
                emit_progress("received", "received", event="turn.started")
                with progress_to(None):
                    emit_progress("inner", "not part of the outer request")
                tool = ToolProgress("read_source", "正在读取材料", 1)
                tool.complete()
            emit_progress("after", "must not leak")
            self.assertEqual([event["sequence"] for event in events], [1, 2, 3])
            self.assertEqual({event["session_id"] for event in events}, {"session"})
            return {event["turn_id"] for event in events}.pop()
        with ThreadPoolExecutor(2) as executor:
            identities = list(executor.map(run, range(2)))
        self.assertNotEqual(*identities)

    def test_rejected_tool_emits_failure_then_loop_can_complete(self):
        events, count = [], 0
        def model(messages):
            nonlocal count
            count += 1
            if count == 1:
                return {"tool_calls": [{"id": "same-id", "function": {"name": "read_source", "arguments": "invalid json"}}]}
            return completion(messages if count > 2 else [{"role": "user"}])
        with patch.dict(os.environ, ENV), progress_to(events.append):
            result = investigate(stages()[0], completion=model)
        self.assertEqual(result["llm"]["status"], "ok")
        trace = result["llm"]["tool_trace"]
        self.assertEqual(trace[0]["status"], "rejected")
        self.assertEqual(len({row["item_id"] for row in trace}), 3)
        for row in trace:
            lifecycle = [event for event in events if event.get("item", {}).get("id") == row["item_id"]]
            self.assertEqual(lifecycle[0]["event"], "item.started")
            self.assertEqual(lifecycle[-1]["event"], "item.completed")
            self.assertEqual(lifecycle[-1]["item"]["status"], "failed" if row["status"] == "rejected" else "completed")
        self.assertNotIn("arguments", json.dumps(events))
        self.assertNotIn("gateway-list", json.dumps(events))

    def test_learning_submission_is_not_successful_until_review_passes(self):
        events, count = [], 0
        def model(_):
            nonlocal count
            count += 1
            return call("submit_learning_step", step(evidence=[{"source_id": "missing", "line": 1, "quote": "invented"}]) if count == 1 else step())
        payload = {"diagnostic_mode": "open_network", "configs": {}, "question": "我不会看接口", "use_llm": True, "learning": {"level": 1}}
        with patch.dict(os.environ, ENV), progress_to(events.append):
            result = investigate(payload, completion=model)
        self.assertEqual(result["llm"]["status"], "ok")
        trace = result["llm"]["tool_trace"]
        self.assertEqual([row["status"] for row in trace], ["rejected", "ok"])
        states = [[event["item"]["status"] for event in events if event.get("item", {}).get("id") == row["item_id"]] for row in trace]
        self.assertEqual(states, [["running", "pending_review", "failed"], ["running", "pending_review", "completed"]])


class ExecutionHandlerTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {**ENV, "LABAGENT_MCP_CONFIG": "", "LABAGENT_GUIDED_WORKFLOW": "", "LABAGENT_CLOUD_PERSISTENCE": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.server = create_server(0, Path(self.temp.name) / "sessions.sqlite3", diagnosis_backend="local")
        self.addCleanup(self.server.server_close)
        model = patch("labagent.investigation.chat_completion", side_effect=lambda messages, **kwargs: completion(messages))
        model.start()
        self.addCleanup(model.stop)

    def request(self, path, payload):
        body = json.dumps(payload).encode()
        raw = f"POST {path} HTTP/1.0\r\nHost: 127.0.0.1:{self.server.server_port}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        sock = RequestSocket(raw)
        with patch.object(Handler, "log_message"):
            self.server.RequestHandlerClass(sock, ("127.0.0.1", 12345), self.server)
        head, body = sock.output.getvalue().split(b"\r\n\r\n", 1)
        self.assertIn(b"200", head.split(b"\r\n")[0])
        return [json.loads(line) for line in body.splitlines() if line] if path.endswith("/stream") else json.loads(body)

    def test_stream_preserves_turn_and_item_ids_in_saved_attempts(self):
        events = self.request("/api/diagnose/stream", stages()[0])
        result = events[-1]["result"]
        execution = result["execution"]
        self.assertEqual(events[0]["event"], "turn.started")
        self.assertEqual(events[-1]["event"], "turn.completed")
        self.assertEqual({event["turn_id"] for event in events}, {execution["turn_id"]})
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(events[-1]["session_id"], result["session_id"])
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(result["status"], "needs_information")
        self.assertEqual({item["id"] for item in execution["items"]}, {row["item_id"] for row in result["llm"]["tool_trace"]})
        store = self.server.RequestHandlerClass.keywords["store"]
        self.assertEqual(store.history(result["session_id"])["attempts"][-1]["result"]["execution"], execution)
        followup = {**stages()[0], "session_id": result["session_id"], "interaction": {"type": "ask", "message": "为什么？"}}
        second = self.request("/api/diagnose/stream", followup)
        self.assertNotEqual(second[0]["turn_id"], events[0]["turn_id"])
        self.assertEqual({event["session_id"] for event in second}, {result["session_id"]})

    def test_nonstreaming_requests_save_execution_metadata_too(self):
        result = self.request("/api/diagnose", stages()[0])
        self.assertEqual(result["execution"]["status"], "completed")
        self.assertTrue(all(item["status"] == "completed" for item in result["execution"]["items"]))

    def test_model_failure_is_distinct_from_waiting_for_experiment_evidence(self):
        with patch("labagent.investigation.chat_completion", return_value={"content": "unsupported text reply"}):
            events = self.request("/api/diagnose/stream", stages()[0])
        self.assertEqual(events[-1]["type"], "result", "failure results remain available to history and legacy clients")
        self.assertEqual(events[-1]["event"], "turn.failed")
        self.assertEqual(events[-1]["result"]["execution"]["status"], "failed")

    def test_unexpected_handler_failure_keeps_terminal_event_identity(self):
        self.server.run_diagnosis = lambda payload: (_ for _ in ()).throw(RuntimeError("private-error"))
        events = self.request("/api/diagnose/stream", stages()[0])
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["event"], "turn.failed")
        self.assertEqual(events[0]["turn_id"], events[-1]["turn_id"])
        self.assertNotIn("private-error", json.dumps(events))
