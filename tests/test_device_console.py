"""Console transcripts, actual stdio MCP and optional local TCP-to-bridge integration."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from io import BytesIO, StringIO
import json
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from labagent.device_console import (ConsoleError, DEFAULT_COMMANDS, MAX_OUTPUT,
    TelnetStream, read_command, terminal_text, validate_devices)
from labagent.lab_bridge import LabBridge
from labagent.mcp_client import LabMCPTools
from scripts.connect_lab import configure_console, execute_job, main
from scripts.device_mcp import serve

ROOT = Path(__file__).resolve().parents[1]
COMMAND = "display ip interface brief"
DEVICE = {"host": "127.0.0.1", "port": 2000, "commands": [COMMAND]}
OUTPUT = "GE0/0/0 10.12.0.1/24 up up"


class TranscriptSocket:
    def __init__(self, chunks=None, replies=None):
        self.chunks, self.replies = deque(chunks or [b"<R1>"]), replies or {}
        self.sent = []

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def settimeout(self, seconds): pass
    def recv(self, size):
        if not self.chunks: raise socket.timeout()
        chunk = self.chunks.popleft()
        if isinstance(chunk, Exception): raise chunk
        return chunk

    def sendall(self, value):
        self.sent.append(value)
        self.chunks.extend(self.replies.get(value, []))


class ProtocolTests(unittest.TestCase):
    def test_fragmented_telnet_negotiation_suboptions_and_echo_do_not_leak_into_output(self):
        sent, result = [], bytearray()
        stream = TelnetStream(sent.append)
        raw = (b"\xff\xfb\x01\xff\xfb\x01\xff\xfb\x03\xff\xfd\x03"
               b"\xff\xfd\x18\xff\xfb\x1f"
               b"before\xff\xfa\x18hidden\xff\xffdata\xff\xf0after\xff\xff"
               b"\xff\xfc\x01\xff\xfe\x03")
        for byte in raw:
            result.extend(stream.feed(bytes([byte])))
        self.assertEqual(result, b"beforeafter\xff")
        self.assertEqual(sent, [b"\xff\xfd\x01", b"\xff\xfd\x03", b"\xff\xfb\x03",
            b"\xff\xfc\x18", b"\xff\xfe\x1f", b"\xff\xfe\x01", b"\xff\xfc\x03"])

    def test_terminal_text_handles_multibyte_encodings_ansi_and_erased_text(self):
        self.assertEqual(terminal_text("接口\x1b[31mup\x1b[0m!\b\r\n".encode()), "接口up\n")
        self.assertEqual(terminal_text("接口正常".encode("gb18030")), "接口正常")

    def test_config_accepts_general_display_choices_and_rejects_command_composition(self):
        self.assertEqual(validate_devices({"devices": {"R1": DEVICE}})["R1"], DEVICE)
        for changed in ({"port": True}, {"host": ""}, {"commands": ["system-view"]},
                        {"commands": [COMMAND + "\nsave"]}, {"commands": ["display version | save"]}):
            with self.subTest(changed=changed), self.assertRaises(ConsoleError):
                validate_devices({"devices": {"R1": {**DEVICE, **changed}}})
        with self.assertRaises(ConsoleError):
            validate_devices({"devices": {"R1": DEVICE, "r1": DEVICE}})


class ConsoleTests(unittest.TestCase):
    def read(self, transcript, **kwargs):
        with patch("labagent.device_console.socket.create_connection", return_value=transcript):
            return read_command(DEVICE, COMMAND, **kwargs)

    def test_multiple_paginated_pages_and_fragmented_prompt_are_collected_once(self):
        transcript = TranscriptSocket(replies={
            COMMAND.encode() + b"\r\n": [COMMAND.encode() + b"\r\npage one\r\n---- Mo", b"re ----"],
            b" ": [b"\r\npage two\r\n---- More ----"]})
        original = transcript.sendall
        pages = 0
        def send(value):
            nonlocal pages
            if value == b" ":
                pages += 1
                if pages == 2:
                    transcript.replies[b" "] = [b"\r\n" + OUTPUT.encode() + b"\r\n<R", b"1>"]
            original(value)
        transcript.sendall = send
        text = self.read(transcript)
        self.assertIn("page one", text)
        self.assertIn("page two", text)
        self.assertIn(OUTPUT, text)
        self.assertNotIn("More", text)
        self.assertEqual(transcript.sent.count(COMMAND.encode() + b"\r\n"), 1)
        self.assertEqual(pages, 2)

    def test_split_pager_padding_does_not_send_two_spaces_for_the_same_page(self):
        transcript = TranscriptSocket(replies={
            COMMAND.encode() + b"\r\n": [COMMAND.encode() + b"\r\npage one\r\n---- More ----", b"  "],
            b" ": [b"\r\n" + OUTPUT.encode() + b"\r\n<R1>"]})
        self.assertIn(OUTPUT, self.read(transcript))
        self.assertEqual(transcript.sent.count(b" "), 1)

    def test_console_wakes_only_when_it_needs_enter_and_reads_after_prompt(self):
        transcript = TranscriptSocket([b"Press ENTER to get started", socket.timeout()], {
            b"\r\n": [b"\r\n<R1>"], COMMAND.encode() + b"\r\n": [OUTPUT.encode() + b"\r\n<R1>"]})
        self.assertEqual(self.read(transcript), OUTPUT)
        self.assertEqual(transcript.sent, [b"\r\n", COMMAND.encode() + b"\r\n"])

    def test_login_prompt_never_receives_a_device_command(self):
        transcript = TranscriptSocket([b"Username: "])
        with self.assertRaisesRegex(ConsoleError, "登录"):
            self.read(transcript)
        self.assertEqual(transcript.sent, [])

    def test_closed_timeout_oversized_and_rejected_output_never_become_observations(self):
        for chunks in ([b""], [], [b"x" * (MAX_OUTPUT + 1)],
                       [b"Error: Unrecognized command found at '^' position.\r\n<R1>"]):
            with self.subTest(chunks=str(chunks)[:40]):
                transcript = TranscriptSocket(replies={COMMAND.encode() + b"\r\n": chunks})
                with self.assertRaises(ConsoleError):
                    self.read(transcript, timeout=.02)
                self.assertEqual(transcript.sent.count(COMMAND.encode() + b"\r\n"), 1)

    def test_command_must_be_allowed_before_any_network_connection(self):
        with patch("labagent.device_console.socket.create_connection") as connect:
            with self.assertRaises(ConsoleError):
                read_command(DEVICE, "display current-configuration")
            connect.assert_not_called()


class SetupTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "console.json"

    def configure(self, port=2000):
        with patch("builtins.input", side_effect=["R1", "", str(port), ""]), redirect_stdout(StringIO()):
            configure_console(self.config)

    def test_wizard_generates_working_stdio_catalog_without_needing_an_external_server(self):
        self.configure()
        settings = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(settings["servers"]["console"]["command"], sys.executable)
        with LabMCPTools(self.config) as lab:
            self.assertEqual(lab.warnings, [])
            self.assertEqual(lab.tools[0]["function"]["name"], "lab_console_r1")
            self.assertEqual(lab.tools[0]["function"]["parameters"]["properties"]["command"]["enum"], DEFAULT_COMMANDS)
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            configure_console(self.config)
        self.assertEqual(self.config.read_bytes(), before)

    def test_probe_reads_real_stdio_fixture_without_contacting_website(self):
        self.config.write_text(json.dumps({"servers": {"test": {"command": sys.executable,
            "args": ["-B", str(ROOT / "tests/helpers/mcp_lab_fixture.py")],
            "tools": {"r1": {"name": "device_exec", "effect": "observe", "device": "R1",
                "arguments": {"target": "R1"}, "choices": {"command": [COMMAND]}}}}}}), encoding="utf-8")
        output = StringIO()
        with patch.object(sys, "argv", ["connect_lab.py", str(self.config), "--probe"]), \
                patch("scripts.connect_lab.Website") as website, redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertIn(OUTPUT, output.getvalue())
        website.assert_not_called()

    def test_invalid_setup_reports_actionable_error_without_partial_files(self):
        errors = StringIO()
        with patch.object(sys, "argv", ["connect_lab.py", str(self.config), "--setup"]), \
                patch("builtins.input", side_effect=["R1", "", "not-a-port"]), \
                redirect_stdout(StringIO()), redirect_stderr(errors):
            self.assertEqual(main(), 1)
        self.assertFalse(self.config.exists())
        self.assertIn("端口数字", errors.getvalue())

    def test_protocol_rejects_unconfigured_targets_and_extra_arguments_without_network(self):
        requests = [
            {"id": 1, "method": "initialize"},
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/call", "params": {"name": "device_exec",
                "arguments": {"target": "R1", "command": COMMAND, "host": "other"}}},
            {"id": 3, "method": "tools/call", "params": {"name": "device_exec",
                "arguments": {"target": "R2", "command": COMMAND}}}]
        incoming = BytesIO(b"\n".join(json.dumps({"jsonrpc": "2.0", **r}).encode() for r in requests))
        outgoing = BytesIO()
        with patch("scripts.device_mcp.read_command") as read:
            serve({"R1": DEVICE}, incoming, outgoing)
            read.assert_not_called()
        replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(replies[1]["error"]["code"], -32602)
        self.assertEqual(replies[2]["error"]["code"], -32602)

    def test_actual_tcp_console_through_stdio_pairing_job_and_result(self):
        captured = []
        class Peer(socketserver.BaseRequestHandler):
            def handle(peer):
                peer.request.settimeout(4)
                peer.request.sendall(b"\xff\xfb\x01<R1>")
                data = bytearray()
                while b"\n" not in data:
                    data.extend(peer.request.recv(4096))
                captured.append(bytes(data))
                peer.request.sendall(COMMAND.encode() + b"\r\n" + OUTPUT.encode() + b"\r\n<R1>")
        try:
            server = socketserver.TCPServer(("127.0.0.1", 0), Peer)
        except PermissionError as error:
            self.skipTest("Local TCP listening is blocked by this execution environment: " + str(error))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.configure(server.server_address[1])
            with LabMCPTools(self.config) as lab:
                bridge = LabBridge()
                paired = bridge.create("owner", "console test")
                token = bridge.claim(paired["code"], lab.tools)["token"]
                class Website:
                    def post(self, path, data):
                        bridge.finish(token, data["id"], data.get("result"), data.get("error"))
                with ThreadPoolExecutor(1) as worker:
                    result = worker.submit(bridge.collect, "owner", paired["id"], lab.tools[0]["function"]["name"], {"command": COMMAND}, timeout=4)
                    job = bridge.poll(token, wait=1)
                    self.assertTrue(execute_job(lab, Website(), job))
                    observed = result.result()
                self.assertEqual(observed["device"], "R1")
                self.assertEqual(observed["text"], OUTPUT)
                self.assertEqual(sum(message.count(COMMAND.encode()) for message in captured), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
