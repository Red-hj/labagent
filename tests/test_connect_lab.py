"""Exercise connector preflight with a real local MCP process and no website."""
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from labagent.mcp_client import MCPError
from scripts.connect_lab import Website, main


ROOT = Path(__file__).resolve().parents[1]


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "mcp.json"
        self.settings = {"servers": {"test": {
            "command": sys.executable,
            "args": ["-B", str(ROOT / "tests/helpers/mcp_lab_fixture.py")],
            "tools": {"r1": {"name": "device_exec", "effect": "observe", "device": "R1",
                "arguments": {"target": "R1"},
                "choices": {"command": ["display ip interface brief"]}}}}}}
        self.write_config()

    def write_config(self):
        self.config.write_text(json.dumps(self.settings), encoding="utf-8")

    def run_main(self, *flags):
        output, errors = StringIO(), StringIO()
        with patch.object(sys, "argv", ["connect_lab.py", str(self.config), *flags]), redirect_stdout(output), redirect_stderr(errors):
            status = main()
        return status, output.getvalue(), errors.getvalue()

    def test_preflight_discovers_real_tools_without_pairing_or_device_commands(self):
        with patch("scripts.connect_lab.Website") as website, patch("scripts.connect_lab.getpass.getpass") as prompt, patch("scripts.connect_lab.LabMCPTools.execute") as execute:
            status, output, errors = self.run_main("--check")
        self.assertEqual(status, 0, errors)
        self.assertIn("lab_test_r1", output)
        self.assertIn("display ip interface brief", output)
        self.assertIn("本机MCP自检通过", output)
        website.assert_not_called()
        prompt.assert_not_called()
        execute.assert_not_called()

    def test_preflight_fails_when_no_mcp_server_connects(self):
        self.settings["servers"]["test"]["command"] = str(self.config.parent / "missing.exe")
        self.write_config()
        status, output, errors = self.run_main("--check")
        self.assertEqual(status, 1)
        self.assertNotIn("自检通过", output)
        self.assertIn("test：服务未连接", errors)
        self.assertIn("没有可用检查", errors)

    def test_missing_config_explains_setup_before_starting_mcp_or_pairing(self):
        self.config.unlink()
        with patch("scripts.connect_lab.LabMCPTools") as lab, patch("scripts.connect_lab.Website") as website:
            status, output, errors = self.run_main("--check")
        self.assertEqual(status, 1)
        self.assertIn(str(self.config), errors)
        self.assertIn("data/examples/mcp-ensp.example.json", errors)
        self.assertIn("实际配置文件", errors)
        self.assertEqual(output, "")
        lab.assert_not_called()
        website.assert_not_called()

    def test_partial_connection_reports_warning_and_lists_available_tools(self):
        self.settings["servers"]["offline"] = {"command": str(self.config.parent / "missing.exe")}
        self.write_config()
        status, output, errors = self.run_main("--check")
        self.assertEqual(status, 0)
        self.assertIn("lab_test_r1", output)
        self.assertIn("offline：服务未连接", errors)

    def test_input_closed_reports_actionable_error_without_claiming(self):
        with patch("scripts.connect_lab.getpass.getpass", side_effect=EOFError), patch.object(Website, "post") as post:
            status, _, errors = self.run_main()
        self.assertEqual(status, 1)
        self.assertIn("未读到连接码", errors)
        post.assert_not_called()

    def test_claim_without_token_does_not_poll_or_expose_response(self):
        for grant in ({}, {"token": 123}, {"token": "", "detail": "private-detail"}):
            with self.subTest(grant=grant), patch("scripts.connect_lab.getpass.getpass", return_value="A" * 16), patch.object(Website, "post", return_value=grant) as post:
                status, output, errors = self.run_main()
                self.assertEqual(status, 1)
                self.assertIn("有效连接令牌", errors)
                self.assertNotIn("private-detail", output + errors)
                post.assert_called_once()

    def test_website_rejects_invalid_and_oversized_responses_without_network_retry(self):
        website = Website("http://127.0.0.1:8765")
        for raw in (b"[]", b"null", b"<html>Unavailable</html>", b"\xff", b" " * 180_001):
            with self.subTest(raw=raw[:20]), patch.object(website.opener, "open", return_value=BytesIO(raw)) as opened:
                with self.assertRaises(MCPError):
                    website.post("/api/lab/agent/poll", {})
                opened.assert_called_once()

    def test_website_distinguishes_transport_failure_from_invalid_response(self):
        website = Website("http://127.0.0.1:8765")
        with patch.object(website.opener, "open", side_effect=OSError):
            with self.assertRaises(ConnectionError):
                website.post("/api/lab/agent/poll", {})
        with patch.object(website.opener, "open", return_value=BytesIO(b'{"job":null}')):
            self.assertEqual(website.post("/api/lab/agent/poll", {}), {"job": None})
