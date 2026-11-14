from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest

from labagent.agentarts_client import AgentArtsClient, SESSION_HEADER


class AgentArtsClientTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers, payload))
                if self.path == "/redirect":
                    self.send_response(307)
                    self.send_header("Location", "/other-runtime")
                    self.end_headers()
                    return
                if self.path == "/denied":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"private-provider-error")
                    return
                body = json.dumps({"status": "success", "session_id": "a" * 32, "reply": "已接收材料"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_gateway_headers_and_materials_survive_followup(self):
        client = AgentArtsClient(self.url + "/runtimes/labagent/invocations?endpoint=dev", "test-key", "cloud-session")
        first = client.invoke({"materials": {"R1.cfg": "接口配置"}})
        client.invoke({"message": "如何修改？"})
        self.assertEqual(first["reply"], "已接收材料")
        self.assertEqual(len(self.requests), 2)
        for path, headers, _ in self.requests:
            self.assertEqual(path, "/runtimes/labagent/invocations?endpoint=dev")
            self.assertEqual(headers[SESSION_HEADER], "cloud-session")
            self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(self.requests[0][2]["materials"]["R1.cfg"], "接口配置")
        self.assertEqual(self.requests[1][2], {"message": "如何修改？"})

    def test_redirect_does_not_forward_credentials(self):
        with self.assertRaisesRegex(ValueError, "重定向"):
            AgentArtsClient(self.url + "/redirect", "test-key").invoke({})
        self.assertEqual(len(self.requests), 1)

    def test_authentication_error_has_actionable_status(self):
        with self.assertRaisesRegex(ValueError, "HTTP 401") as error:
            AgentArtsClient(self.url + "/denied", "test-key").invoke({})
        self.assertNotIn("private-provider-error", str(error.exception))
        self.assertNotIn("test-key", str(error.exception))

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "runtime/agentarts-signing-sdk/apig_sdk/signer.py").exists(),
                         "需要先安装华为官方IAM签名SDK")
    def test_iam_signature_covers_session_and_version_route(self):
        client = AgentArtsClient(self.url + "/runtimes/labagent/invocations?endpoint=Latest",
                                 platform_session_id="iam-session", access_key="example-ak",
                                 secret_key="example-sk", security_token="temporary-token")
        client.invoke({"materials": {"R1.cfg": "接口配置"}})
        client.invoke({"message": "继续检查"})
        sdk = client.iam_module
        for path, headers, payload in self.requests:
            self.assertEqual(headers["X-Sdk-Content-Sha256"], "UNSIGNED-PAYLOAD")
            self.assertEqual(headers["X-Security-Token"], "temporary-token")
            self.assertTrue(headers["Authorization"].startswith("V11-HMAC-SHA256 Credential=example-ak/"))
            request = sdk.HttpRequest("POST", self.url + path, dict(headers),
                                      json.dumps(payload, ensure_ascii=False))
            self.assertFalse(client.iam_signer.Verify(request, headers["Authorization"]))
            request.query = {}
            self.assertTrue(client.iam_signer.Verify(request, headers["Authorization"]))
            request.uri += "/another-runtime"
            self.assertFalse(client.iam_signer.Verify(request, headers["Authorization"]))
        self.assertEqual(self.requests[0][1][SESSION_HEADER], "iam-session")
        self.assertEqual(self.requests[1][1][SESSION_HEADER], "iam-session")
        self.assertEqual(self.requests[0][2]["materials"]["R1.cfg"], "接口配置")

    def test_incomplete_or_mixed_credentials_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "AK和SK"):
            AgentArtsClient(self.url, access_key="example-ak")
        with self.assertRaisesRegex(ValueError, "一种认证"):
            AgentArtsClient(self.url, "api-key", access_key="ak", secret_key="sk")
