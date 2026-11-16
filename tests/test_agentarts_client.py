from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import tempfile
import unittest

from labagent.agentarts_client import AgentArtsClient, AgentArtsError, SESSION_HEADER
from labagent.monitoring import record_invocation, summarize


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
                if self.path in ("/missing", "/failed"):
                    self.send_response(400 if self.path == "/missing" else 500)
                    self.end_headers()
                    self.wfile.write(json.dumps({"code": "session_not_found", "message": "private-provider-error"}).encode())
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

    def test_session_error_exposes_only_status_and_known_code(self):
        for path, status in (("/missing", 400), ("/failed", 500), ("/denied", 401)):
            with self.subTest(path=path), self.assertRaises(AgentArtsError) as failed:
                AgentArtsClient(self.url + path, "private-key").invoke({})
            self.assertEqual(failed.exception.http_status, status)
            self.assertEqual(failed.exception.code, "session_not_found" if status in (400, 500) else None)
            self.assertNotIn("private-", str(failed.exception))

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

    def test_monitor_records_success_and_failure_without_request_content(self):
        folder = Path(__file__).resolve().parents[1] / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as directory:
            path = Path(directory) / "calls.jsonl"
            AgentArtsClient(self.url, "private-key", monitor_path=path).invoke({"materials":{"private.cfg":"private-material"}})
            with self.assertRaisesRegex(ValueError, "HTTP 401"):
                AgentArtsClient(self.url + "/denied", "private-key", monitor_path=path).invoke({})
            text = path.read_text(encoding="utf-8")
            for private in ("private-key", "private-material", "private.cfg", "private-provider-error"):
                self.assertNotIn(private, text)
            rows = [json.loads(line) for line in text.splitlines()]
            self.assertEqual([row["http_status"] for row in rows], [200, 401])
            summary = summarize(rows)
            self.assertEqual(summary["requests"], 2)
            self.assertEqual(summary["transport_successes"], 1)
            self.assertEqual(summary["failures"], {"http_error":1})

    def test_monitor_totals_model_usage_and_tool_results(self):
        folder = Path(__file__).resolve().parents[1] / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as directory:
            path = Path(directory) / "calls.jsonl"
            result = {"status":"success", "reply":"private-answer", "result":{"llm":{
                "status":"ok", "calls":[{"elapsed_ms":12,"usage":{"total_tokens":30}},
                                             {"elapsed_ms":8,"usage":{"total_tokens":20}}],
                "tool_trace":[{"tool":"read_source","status":"ok","result":"private-config"}]}}}
            record_invocation(path, elapsed_ms=40, http_status=200, result=result)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("private-",text)
            summary = summarize([json.loads(text)])
            self.assertEqual(summary["model_calls"],2)
            self.assertEqual(summary["total_tokens"],50)
            self.assertEqual(summary["tools"],{"read_source":1})
            self.assertEqual(summary["average_ms"],40)
            self.assertEqual(summary["p95_ms"],40)
