import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.material_service import MaterialService
from test_investigation import ROOT, ENV, stages, completion


class MaterialServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "sessions.sqlite3"
        self.service = MaterialService(self.database, completion)
        self.env = patch.dict(os.environ, ENV); self.env.start(); self.addCleanup(self.env.stop)

    def test_materials_only_start_and_message_only_resume(self):
        first = self.service.invoke({"materials": stages()[0]["configs"]})
        second = self.service.invoke({"session_id": first["session_id"], "message": "为什么要检查网关？"})
        self.assertEqual((first["status"], second["status"]), ("success", "success"))
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertTrue(second["result"]["workflow"]["resumed"])
        self.assertEqual(len(second["result"]["conversation"]), 2)

    def test_update_merges_materials_and_survives_service_reopen(self):
        first = self.service.invoke({"materials": stages()[0]["configs"]})
        service = MaterialService(self.database, completion)
        newer = stages()[1]["configs"]["SW1"]
        service.invoke({"session_id": first["session_id"], "materials": {"SW1": newer}, "message": "继续分析"})
        latest = service.store.history(first["session_id"])["attempts"][-1]["input"]
        self.assertEqual(latest["configs"]["SW1"], newer)
        self.assertEqual(set(latest["configs"]), set(stages()[0]["configs"]))

    def test_invalid_or_unknown_session_and_missing_materials(self):
        for request in ({}, {"materials": []}, {"session_id": "bad"}, {"session_id": "f" * 32}, []):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.service.invoke(request)

    def test_action_only_request_uses_existing_materials(self):
        first = self.service.invoke({"materials": stages()[0]["configs"]})
        reply = self.service.invoke({"session_id": first["session_id"], "action": "repair"})
        self.assertEqual(reply["status"], "success")
        self.assertEqual(reply["result"]["conversation"][-1]["intent"], "repair")


@unittest.skipUnless(importlib.util.find_spec("agentarts"), "Install requirements-agentarts.txt")
class AgentArtsSdkTests(unittest.TestCase):
    setUp = MaterialServiceTests.setUp

    def test_real_sdk_routes_and_session_header(self):
        from starlette.testclient import TestClient
        from labagent.agentarts_runtime import create_app
        from agentarts.sdk.runtime.model import SESSION_HEADER
        with TestClient(create_app(self.database, completion)) as client:
            self.assertEqual(client.get("/ping").status_code, 200)
            first = client.post("/invocations", json={"materials": stages()[0]["configs"]}, headers={SESSION_HEADER: "platform-session-1"})
            self.assertEqual(first.status_code, 200)
            session = first.json()["session_id"]
            self.assertEqual(first.headers[SESSION_HEADER], "platform-session-1")
            self.assertEqual(first.json()["platform_session_id"], "platform-session-1")
            second = client.post("/invocations", json={"message": "说明网关参数"}, headers={SESSION_HEADER: "platform-session-1"})
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json()["session_id"], session)
            self.assertEqual(len(second.json()["result"]["conversation"]), 2)
            self.assertEqual(client.post("/invocations", json={"materials": []}).status_code, 400)

    def test_platform_session_survives_adapter_reopen_and_keeps_sessions_separate(self):
        from starlette.testclient import TestClient
        from labagent.agentarts_runtime import create_app
        from agentarts.sdk.runtime.model import SESSION_HEADER
        with TestClient(create_app(self.database, completion)) as client:
            first = client.post("/invocations", json={"materials": stages()[0]["configs"]}, headers={SESSION_HEADER: "cloud-a"}).json()
        with TestClient(create_app(self.database, completion)) as client:
            response = client.post("/invocations", json={"message": "继续解释"}, headers={SESSION_HEADER: "cloud-a"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers[SESSION_HEADER], "cloud-a")
            self.assertEqual(response.json()["session_id"], first["session_id"])
            self.assertEqual(len(response.json()["result"]["conversation"]), 2)
            self.assertEqual(client.post("/invocations", json={"message": "继续解释"}, headers={SESSION_HEADER: "cloud-b"}).status_code, 400)
            other = client.post("/invocations", json={"materials": stages()[0]["configs"]}, headers={SESSION_HEADER: "cloud-b"}).json()
            self.assertNotEqual(other["session_id"], first["session_id"])
            self.assertEqual(len(other["result"]["conversation"]), 1)


if __name__ == "__main__":
    unittest.main()
