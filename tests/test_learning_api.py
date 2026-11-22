from email.parser import BytesParser
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.server import create_server, Handler
from labagent.workflow import run_open_workflow
from labagent.learning import COOKIE
from test_personal import RequestSocket
from test_mcp_learning import ENV, step, call, fixture_spec

ROOT = Path(__file__).resolve().parents[1]


class LearningApiTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "runtime/tests"; base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        environment = patch.dict(os.environ, {**ENV, "LABAGENT_GUIDED_WORKFLOW": "1", "LABAGENT_MCP_CONFIG": "", "LABAGENT_CLOUD_PERSISTENCE": ""})
        environment.start(); self.addCleanup(environment.stop)
        self.server = create_server(0, self.folder / "sessions.sqlite3", diagnosis_backend="local")
        self.addCleanup(self.server.server_close)
        self.user, token = self.server.personal.authenticate({"username": "learner", "password": "test-student-password"}, register=True)
        self.account_cookie = "labagent_personal=" + token
        # Use the actual graph and handlers, with a deterministic model response.
        self.server.run_diagnosis = lambda payload: run_open_workflow(payload, self.folder / "workflow.sqlite3",
            lambda messages: call("submit_learning_step", step()))

    def request(self, path, payload=None, cookie=""):
        body = json.dumps(payload).encode() if payload is not None else b""
        values = {"Host": f"127.0.0.1:{self.server.server_port}", "Content-Type": "application/json", "Content-Length": str(len(body))}
        if cookie: values["Cookie"] = cookie
        raw = ("POST" if payload is not None else "GET") + " " + path + " HTTP/1.0\r\n"
        raw += "".join(key + ": " + value + "\r\n" for key, value in values.items())
        sock = RequestSocket(raw.encode() + b"\r\n" + body)
        with patch.object(Handler, "log_message"):
            self.server.RequestHandlerClass(sock, ("127.0.0.1", 12345), self.server)
        head, content = sock.output.getvalue().split(b"\r\n\r\n", 1)
        status, fields = head.split(b"\r\n", 1)
        return int(status.split()[1]), BytesParser().parsebytes(fields + b"\r\n\r\n"), json.loads(content)

    def test_classroom_mode_enforces_guidance_even_if_browser_omits_learning(self):
        status, _, result = self.request("/api/diagnose", {"diagnostic_mode": "open_network", "configs": {},
            "question": "我做到一半，不会看接口状态", "use_llm": True})
        self.assertEqual(status, 200)
        self.assertEqual(result["investigation"]["learning"]["level"], 1)

    def test_browser_cannot_self_grant_solutions(self):
        status, _, _ = self.request("/api/diagnose", {"diagnostic_mode": "open_network", "configs": {},
            "question": "直接给答案", "use_llm": True, "learning": {"level": 3, "authorized": True}}, self.account_cookie)
        self.assertEqual(status, 403)

    def test_solution_cookie_is_http_only_and_bound_to_logged_in_account(self):
        status, headers, _ = self.request("/api/learning/unlock", {"code": "test-teacher-code"}, self.account_cookie)
        self.assertEqual(status, 200)
        grant = headers["Set-Cookie"]
        self.assertIn("HttpOnly", grant); self.assertIn("SameSite=Strict", grant)
        cookies = self.account_cookie + "; " + grant.split(";", 1)[0]
        self.assertTrue(self.request("/api/learning", cookie=cookies)[2]["solution_unlocked"])
        self.assertFalse(self.request("/api/learning", cookie=grant.split(";", 1)[0])[2]["solution_unlocked"])
        self.assertEqual(self.request("/api/learning/unlock", {"code": "wrong"}, self.account_cookie)[0], 400)

    def test_mcp_collection_endpoint_returns_real_protocol_output_without_startup_settings(self):
        config = self.folder / "mcp.json"
        config.write_text(json.dumps({"servers": {"test": fixture_spec()}}))
        with patch.dict(os.environ, {"LABAGENT_MCP_CONFIG": str(config)}):
            status, _, data = self.request("/api/lab/collect", {"tool": "lab_test_r1", "arguments": {"command": "display ip interface brief"}})
        self.assertEqual(status, 200)
        self.assertIn("10.12.0.1/24", next(iter(data["materials"].values())))
        self.assertNotIn("command\"", json.dumps(data))

    def test_learning_script_is_served_by_asset_allowlist(self):
        # Read the actual route response without assuming static files are generic.
        for path, marker in (("/learning.js", b"window.LabLearning"), ("/devices.js", b"window.LabDevices"),
                             ("/devices.css", b".connection-dialog")):
            with self.subTest(path=path):
                raw = f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1:{self.server.server_port}\r\n\r\n".encode()
                sock = RequestSocket(raw)
                with patch.object(Handler, "log_message"):
                    self.server.RequestHandlerClass(sock, ("127.0.0.1", 12345), self.server)
                self.assertTrue(sock.output.getvalue().startswith(b"HTTP/1.0 200"))
                self.assertIn(marker, sock.output.getvalue())


if __name__ == "__main__": unittest.main()
