"""Exercise real HTTP wire handlers and MCP JSON/SSE parsing without TCP."""
from http.server import BaseHTTPRequestHandler
from email.parser import BytesParser
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import HTTPHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

from labagent.mcp_client import HTTPMCPClient, MCPError
from test_personal import RequestSocket


class MCPPeer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, *, event_stream=False):
        body = json.dumps({"jsonrpc": "2.0", **value}).encode()
        if event_stream:
            body = b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info","data":"synthetic"}}\n\n' + b"event: message\ndata: " + body + b"\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if event_stream else "application/json")
        self.send_header("Mcp-Session-Id", "test-session")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append({"method": data.get("method"), "session": self.headers.get("Mcp-Session-Id"),
                                  "protocol": self.headers.get("MCP-Protocol-Version")})
        if self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "/other"); self.end_headers(); return
        if data["method"] == "notifications/initialized":
            self.send_response(202); self.end_headers(); return
        if data["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "synthetic", "version": "1"}}
        elif data["method"] == "tools/list":
            result = {"tools": [{"name": "get_lab_topology", "inputSchema": {"type": "object"}}]}
        elif data["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": "R1 -- R2 (synthetic topology)"}]}
        else:
            self.reply({"id": data["id"], "error": {"code": -32601, "message": "Unknown method"}}); return
        self.reply({"id": data["id"], "result": result}, event_stream=data["method"] == "tools/call")

    def do_DELETE(self):
        self.server.deleted = self.headers.get("Mcp-Session-Id")
        self.send_response(204); self.end_headers()


class MCPHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = SimpleNamespace(calls=[], deleted=None)
        self.url = "http://127.0.0.1:8765"
        server = self.server
        class WireHTTP(HTTPHandler):
            def http_open(self, request):
                body = request.data or b""
                headers = dict(request.header_items())
                headers["Content-Length"] = str(len(body))
                raw = request.get_method() + " " + request.selector + " HTTP/1.0\r\n"
                raw += "".join(key + ": " + value + "\r\n" for key, value in headers.items())
                sock = RequestSocket(raw.encode() + b"\r\n" + body)
                MCPPeer(sock, ("127.0.0.1", 12345), server)
                head, content = sock.output.getvalue().split(b"\r\n\r\n", 1)
                status, fields = head.split(b"\r\n", 1)
                code = int(status.split()[1])
                response = addinfourl(BytesIO(content), BytesParser().parsebytes(fields + b"\r\n\r\n"), request.full_url, code)
                response.msg = "OK"
                return response
        transport = patch("labagent.mcp_client.build_opener", side_effect=lambda *handlers: build_opener(ProxyHandler({}), WireHTTP(), *handlers))
        transport.start(); self.addCleanup(transport.stop)

    def tearDown(self):
        pass

    def test_json_initialization_sse_result_session_headers_and_close(self):
        with HTTPMCPClient({"url": self.url + "/mcp"}) as client:
            self.assertEqual(client.list_tools()[0]["name"], "get_lab_topology")
            self.assertIn("R1 -- R2", client.call_tool("get_lab_topology", {}))
        self.assertTrue(all(row["session"] == "test-session" for row in self.server.calls[1:]))
        self.assertTrue(all(row["protocol"] == "2025-06-18" for row in self.server.calls))
        self.assertEqual(self.server.deleted, "test-session")

    def test_redirect_is_not_followed_and_remote_plain_http_is_rejected(self):
        with self.assertRaisesRegex(MCPError, "跳转"):
            with HTTPMCPClient({"url": self.url + "/redirect"}):
                pass
        self.assertEqual(len(self.server.calls), 1)
        with self.assertRaisesRegex(MCPError, "HTTPS"):
            HTTPMCPClient({"url": "http://example.com/mcp"}).__enter__()


if __name__ == "__main__": unittest.main()
