import json
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from labagent.material_service import MaterialService
from labagent.server import create_server
from labagent.store import Store
from test_investigation import ROOT, ENV, stages, completion


class CloudWebTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.service = MaterialService(self.folder / "cloud.sqlite3", completion)
        self.calls = []
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def client_factory(self, platform_id):
        owner = self

        class Client:
            def invoke(self, payload):
                owner.calls.append((platform_id, payload))
                return owner.service.invoke(payload)
        return Client()

    def start_server(self, backend="agentarts", factory=None):
        server = create_server(0, self.folder / "web.sqlite3", diagnosis_backend=backend,
                               cloud_client_factory=factory or self.client_factory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_port}"

    def request(self, base, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        with urlopen(Request(base + path, data=data, headers={"Content-Type": "application/json"}), timeout=15) as response:
            return json.load(response)

    def test_followup_after_web_restart_keeps_cloud_session_materials_and_report(self):
        base = self.start_server()
        first = self.request(base, "/api/diagnose", stages()[0])
        session = first["session_id"]
        second_base = self.start_server()
        updated = {**stages()[1], "session_id": session,
                   "interaction": {"type": "update", "message": "已更新配置，继续检查。"}}
        second = self.request(second_base, "/api/diagnose", updated)
        self.assertEqual(second["session_id"], session)
        self.assertEqual(self.calls[0][0], self.calls[1][0])
        self.assertEqual(self.calls[1][1]["session_id"], first["agentarts"]["session_id"])
        self.assertEqual(self.calls[1][1]["materials"], updated["configs"])
        cloud_history = self.service.store.history(first["agentarts"]["session_id"])
        self.assertEqual(len(cloud_history["attempts"]), 2)
        self.assertEqual(cloud_history["attempts"][-1]["input"]["configs"], updated["configs"])
        restored = self.request(second_base, "/api/sessions/" + session)
        self.assertEqual(len(restored["attempts"]), 2)
        self.assertEqual(restored["attempts"][-1]["result"]["agentarts"], second["agentarts"])
        report = self.request(second_base, "/api/sessions/" + session + "/report")["markdown"]
        self.assertIn("已更新配置", report)

    def test_new_investigations_use_separate_cloud_sessions(self):
        base = self.start_server()
        first = self.request(base, "/api/diagnose", stages()[0])
        second = self.request(base, "/api/diagnose", stages()[0])
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertNotEqual(self.calls[0][0], self.calls[1][0])
        with patch.dict(os.environ, {"LABAGENT_LLM_KEY": ""}):
            health = self.request(base, "/api/health")
        self.assertEqual(health["diagnosis_backend"], "agentarts")
        self.assertTrue(health["diagnosis_configured"])
        self.assertFalse(health["llm_configured"])

    def test_stream_reports_cloud_stages_and_saves_result(self):
        base = self.start_server()
        with urlopen(Request(base + "/api/diagnose/stream", data=json.dumps(stages()[0]).encode(),
                             headers={"Content-Type": "application/json"}), timeout=15) as response:
            events = [json.loads(line) for line in response if line.strip()]
        progress = [event["stage"] for event in events if event["type"] == "progress"]
        self.assertIn("cloud_connecting", progress)
        self.assertIn("cloud_received", progress)
        self.assertEqual(events[-1]["type"], "result")
        session = events[-1]["result"]["session_id"]
        self.assertEqual(len(self.request(base, "/api/sessions/" + session)["attempts"]), 1)

    def test_cloud_failure_has_no_saved_attempt_or_local_fallback(self):
        class FailedClient:
            def invoke(self, payload):
                raise ValueError("运行时返回 HTTP 401，请检查入站认证")
        base = self.start_server(factory=lambda session: FailedClient())
        with patch("labagent.server.run_workflow", side_effect=AssertionError("must use selected cloud backend")):
            with urlopen(Request(base + "/api/diagnose/stream", data=json.dumps(stages()[0]).encode(),
                                 headers={"Content-Type": "application/json"}), timeout=15) as response:
                events = [json.loads(line) for line in response if line.strip()]
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("401", events[-1]["message"])
        self.assertEqual(Store(self.folder / "web.sqlite3").sessions(), [])

    def test_switching_engine_keeps_existing_history(self):
        cloud_base = self.start_server()
        first = self.request(cloud_base, "/api/diagnose", stages()[0])
        local_base = self.start_server(backend="local")
        with self.assertRaises(HTTPError) as caught:
            self.request(local_base, "/api/diagnose", {**stages()[0], "session_id": first["session_id"]})
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("华为云", json.load(caught.exception)["error"])
        self.assertEqual(len(self.request(local_base, "/api/sessions/" + first["session_id"])["attempts"]), 1)


@unittest.skipUnless(importlib.util.find_spec("agentarts"), "需要AgentArts SDK独立环境")
class CloudWebSdkTests(unittest.TestCase):
    def setUp(self):
        CloudWebTests.setUp(self)
        from starlette.testclient import TestClient
        from labagent.agentarts_runtime import create_app
        app = create_app(self.folder / "sdk.sqlite3", completion)
        self.service = app.material_service
        self.sdk = TestClient(app)
        self.sdk.__enter__()
        self.addCleanup(self.sdk.__exit__, None, None, None)

    def client_factory(self, platform_id):
        owner = self

        class Client:
            def invoke(self, payload):
                owner.calls.append((platform_id, payload))
                response = owner.sdk.post("/invocations", json=payload,
                                           headers={"x-hw-agentarts-session-id": platform_id})
                owner.assertEqual(response.status_code, 200)
                owner.assertEqual(response.headers["x-hw-agentarts-session-id"], platform_id)
                return response.json()
        return Client()

    start_server = CloudWebTests.start_server
    request = CloudWebTests.request
    test_web_restart_and_followup_through_official_sdk = CloudWebTests.test_followup_after_web_restart_keeps_cloud_session_materials_and_report
