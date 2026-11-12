from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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
