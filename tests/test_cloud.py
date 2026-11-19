import base64
import hashlib
import hmac
import time
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from labagent.server import create_server
from test_engine import ROOT, case


class CloudApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        cls.origin = "https://labagent-test.onrender.com"
        cls.server = create_server(0, Path(cls.temp.name) / "sessions.sqlite3", public_url=cls.origin, team_password="test-team-password")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.authorization = "Basic " + base64.b64encode(b"labagent:test-team-password").decode()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(5)
        cls.temp.cleanup()

    def request(self, path, payload=None, *, auth=True, headers=None):
        values = {"Host": "labagent-test.onrender.com", **(headers or {})}
        if auth:
            values.setdefault("Authorization", self.authorization)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            values.update({"Content-Type": "application/json"})
            values.setdefault("Origin", self.origin)
        with urlopen(Request(self.base + path, data=data, headers=values), timeout=5) as response:
            body = response.read().decode()
            return json.loads(body) if response.headers["Content-Type"].startswith("application/json") else body

    def test_health_probe_and_team_entry(self):
        self.assertTrue(self.request("/api/health", auth=False)["online"])
        with self.assertRaises(HTTPError) as denied:
            self.request("/", auth=False)
        self.assertEqual(denied.exception.code, 401)
        self.assertIn("LabAgent", denied.exception.headers["WWW-Authenticate"])
        self.assertIn("诊断与交流", self.request("/"))
        with self.assertRaises(HTTPError) as denied:
            self.request("/api/diagnose", case(), auth=False)
        self.assertEqual(denied.exception.code, 401)

    def test_cloud_diagnosis_resume_and_report_download(self):
        first = self.request("/api/diagnose", case("01_area_mismatch"))
        payload = case("03_verified_demo"); payload["session_id"] = first["session_id"]
        second = self.request("/api/diagnose", payload)
        self.assertEqual(second["status"], "verified_from_observations")
        history = self.request("/api/sessions/" + first["session_id"])
        self.assertEqual(len(history["attempts"]), 2)
        report = self.request("/api/export", {"session_id": first["session_id"]})
        self.assertIn("网络实验诊断过程报告", report["markdown"])
        self.assertTrue(report["filename"].endswith(".md"))
        self.assertTrue(Path(report["report_path"]).is_file())

    def test_cross_site_origin_and_unknown_host_rejected(self):
        for headers in ({"Origin": "https://other.invalid"}, {"Host": "other.invalid"}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as denied:
                self.request("/api/diagnose", case(), headers=headers)
            self.assertEqual(denied.exception.code, 403)

    def test_invalid_password_and_malformed_authorization_rejected(self):
        for value in ("Basic !!!", "Bearer unrelated", "Basic " + base64.b64encode(b"labagent:wrong").decode()):
            with self.subTest(value=value), self.assertRaises(HTTPError) as denied:
                self.request("/api/sessions", headers={"Authorization": value})
            self.assertEqual(denied.exception.code, 401)

    def test_team_cookie_authenticates_media_without_basic_header_and_rejects_invalid_sessions(self):
        with urlopen(Request(self.base + "/", headers={"Authorization":self.authorization}), timeout=5) as response:
            cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Secure", cookie)
        cookie = cookie.split(";", 1)[0]
        with urlopen(Request(self.base + "/api/demos/operation_history/video", headers={"Cookie":cookie, "Range":"bytes=0-15"}), timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(response.read()), 16)
        expires = str(int(time.time()) - 1)
        signature = hmac.new(b"test-team-password", ("team-v1:" + expires).encode(), hashlib.sha256).hexdigest()
        for invalid in (cookie + "bad", "labagent_team=" + expires + "." + signature):
            with self.subTest(cookie=invalid), self.assertRaises(HTTPError) as denied:
                self.request("/api/rooms", {"name":"blocked", "actor":"visitor"}, auth=False, headers={"Cookie":invalid})
            self.assertEqual(denied.exception.code, 401)
            self.assertIsNone(denied.exception.headers.get("WWW-Authenticate"))
            self.assertEqual(json.loads(denied.exception.read())["code"], "team_sign_in_required")

    def test_background_requests_do_not_open_basic_auth_dialogs(self):
        with self.assertRaises(HTTPError) as denied:
            self.request("/api/rooms/" + "a" * 32, auth=False)
        self.assertEqual(denied.exception.code, 401)
        self.assertIsNone(denied.exception.headers.get("WWW-Authenticate"))
        self.assertEqual(json.loads(denied.exception.read())["code"], "team_sign_in_required")

    def test_online_configuration_requires_https_and_team_password(self):
        for url, password in (("http://example.com", "test"), ("https://example.com/path", "test"), ("https://example.com", "")):
            with self.subTest(url=url, password=password), self.assertRaises(ValueError):
                create_server(0, public_url=url, team_password=password)


if __name__ == "__main__":
    unittest.main()
