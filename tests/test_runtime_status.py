"""Read-only runtime checks over the official SDK and deployment CLI."""
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.agentarts_client import AgentArtsClient
from labagent.material_service import MaterialService
from scripts import call_agentarts
from test_mcp_learning import ENV

ROOT = Path(__file__).resolve().parents[1]
RELAY = "https://lab.example/api/lab/runtime/collect"


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=folder); self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        env = patch.dict(os.environ, {**ENV, "LABAGENT_LAB_RELAY_URL": RELAY})
        env.start(); self.addCleanup(env.stop)

    def test_status_reports_support_and_compares_relay_without_exposing_environment(self):
        service = MaterialService(self.folder / "sessions.sqlite3")
        with patch("labagent.workflow.available", return_value=True):
            status = service.runtime_status(RELAY)
        self.assertTrue(status["model_configured"])
        self.assertEqual(status["workflow_backend"], "langgraph")
        self.assertTrue(status["guided_learning"])
        self.assertEqual(status["lab_relay"], {"supported": True, "configured": True, "matches_expected": True})
        self.assertFalse(service.runtime_status("https://wrong.example/collect")["lab_relay"]["matches_expected"])
        with patch.dict(os.environ, {"LABAGENT_LAB_RELAY_URL": "", "LABAGENT_LLM_KEY": ""}):
            missing = service.runtime_status()
        self.assertFalse(missing["model_configured"])
        self.assertFalse(missing["lab_relay"]["configured"])
        self.assertIsNone(missing["lab_relay"]["matches_expected"])
        self.assertEqual(service.store.sessions(), [])
        for private in (ENV["LABAGENT_LLM_KEY"], ENV["LABAGENT_LLM_URL"], RELAY):
            self.assertNotIn(private, json.dumps(status))

    def test_client_disables_model_and_does_not_accept_an_old_diagnosis_as_status(self):
        client = AgentArtsClient("http://127.0.0.1:8080/invocations")
        status = MaterialService(self.folder / "sessions.sqlite3").runtime_status(RELAY)
        with patch.object(client, "invoke", return_value={"status": "ready", "runtime": status}) as invoke:
            self.assertEqual(client.runtime_status(expected_relay_url=RELAY), status)
        invoke.assert_called_once_with({"operation": "runtime_status", "use_llm": False, "expected_relay_url": RELAY})
        for old in ({"status": "not_configured", "result": {}}, {"status": "ready", "runtime": {}}):
            with patch.object(client, "invoke", return_value=old) as invoke:
                with self.assertRaisesRegex(ValueError, "尚未返回版本与能力状态"):
                    client.runtime_status()
                self.assertEqual(invoke.call_count, 1)

    def test_cli_returns_readiness_without_running_or_saving_a_diagnosis(self):
        ready = {"version": "test", "workflow_backend": "langgraph", "model_configured": True,
            "guided_learning": True, "lab_relay": {"supported": True, "configured": True, "matches_expected": True}}
        for matches in (True, False):
            status = deepcopy(ready); status["lab_relay"]["matches_expected"] = matches
            output = io.StringIO()
            with (patch.object(call_agentarts, "load_env"),
                  patch.object(call_agentarts.AgentArtsClient, "from_environment") as factory,
                  patch("sys.argv", ["call_agentarts.py", "--url", "http://localhost:8080/invocations", "--check-runtime", "--relay-url", RELAY]),
                  patch.object(Path, "mkdir", side_effect=AssertionError("Status queries must not save a diagnosis")),
                  redirect_stdout(output)):
                factory.return_value.runtime_status.return_value = status
                self.assertEqual(call_agentarts.main(), 0 if matches else 1)
                factory.return_value.runtime_status.assert_called_once_with(expected_relay_url=RELAY)
                factory.return_value.invoke.assert_not_called()
            self.assertEqual(json.loads(output.getvalue()), status)


@unittest.skipUnless(importlib.util.find_spec("agentarts"), "需要AgentArts SDK独立环境")
class RuntimeStatusSDKTests(unittest.TestCase):
    def test_sdk_status_does_not_call_model_or_create_platform_and_material_sessions(self):
        from starlette.testclient import TestClient
        from labagent.agentarts_runtime import create_app
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            with patch.dict(os.environ, {**ENV, "LABAGENT_LAB_RELAY_URL": RELAY}):
                app = create_app(Path(temp) / "sessions.sqlite3")
                with (TestClient(app) as client,
                      patch.object(app.material_service, "invoke", side_effect=AssertionError("must not diagnose"))):
                    response = client.post("/invocations", json={"operation": "runtime_status", "use_llm": False, "expected_relay_url": RELAY},
                        headers={"x-hw-agentarts-session-id": "runtime-status-check"})
                    self.assertEqual(response.status_code, 200, response.text)
                    status = response.json()
                    self.assertEqual(status["status"], "ready")
                    self.assertEqual(status["platform_session_id"], "runtime-status-check")
                    self.assertTrue(status["runtime"]["lab_relay"]["matches_expected"])
                    self.assertEqual(app.material_service.store.sessions(), [])
                    with app.material_service.store.connect() as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM agentarts_sessions").fetchone()[0], 0)
