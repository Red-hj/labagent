"""Workstation pairing, at-most-once delivery and real MCP observation flow."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.lab_bridge import LabBridge
from labagent.mcp_client import LabMCPTools, MCPError
from labagent.server import create_server, Handler
from scripts.connect_lab import execute_job, Website
from test_personal import RequestSocket
from test_mcp_learning import fixture_spec, ENV, call, step
from labagent.workflow import run_open_workflow

ROOT = Path(__file__).resolve().parents[1]
TOOL = {"type": "function", "function": {"name": "lab_test_r1", "description": "读取R1接口状态",
    "parameters": {"type": "object", "properties": {"command": {"type": "string", "enum": ["display ip interface brief"]}},
                   "required": ["command"], "additionalProperties": False}}}
ARGUMENTS = {"command": "display ip interface brief"}
OBSERVED = {"device": "R1", "command": "display ip interface brief", "text": "GE0/0/0 10.12.0.1/24 up up"}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.bridge = LabBridge(clock=lambda: self.now)
        self.pair = self.bridge.create("alice", "课堂电脑")
        self.token = self.bridge.claim(self.pair["code"], [TOOL])["token"]

    def test_pair_is_one_use_and_scoped_to_account(self):
        self.assertIsNone(self.bridge.status("bob"))
        self.assertNotIn("token", json.dumps(self.bridge.status("alice")))
        with self.assertRaises(MCPError): self.bridge.claim(self.pair["code"], [TOOL])
        with self.assertRaises(MCPError): self.bridge.collect("bob", self.pair["id"], "lab_test_r1", ARGUMENTS)
        with self.assertRaises(MCPError): self.bridge.disconnect("bob", self.pair["id"])
        with self.assertRaises(PermissionError): self.bridge.poll("invalid", wait=0)

    def test_queued_job_is_delivered_once_and_result_unblocks_collection(self):
        with ThreadPoolExecutor(1) as worker:
            result = worker.submit(self.bridge.collect, "alice", self.pair["id"], "lab_test_r1", ARGUMENTS, timeout=2)
            job = self.bridge.poll(self.token, wait=1)
            self.assertEqual(job["arguments"], ARGUMENTS)
            self.assertIsNone(self.bridge.poll(self.token, wait=0), "A claimed command must never be delivered again")
            with self.assertRaises(MCPError): self.bridge.collect("alice", self.pair["id"], "lab_test_r1", ARGUMENTS)
            self.bridge.finish(self.token, job["id"], OBSERVED)
            self.assertEqual(result.result(), OBSERVED)
            with self.assertRaises(MCPError): self.bridge.finish(self.token, job["id"], OBSERVED)

    def test_timeout_disconnect_and_expiry_do_not_accept_late_results(self):
        with ThreadPoolExecutor(1) as worker:
            result = worker.submit(self.bridge.collect, "alice", self.pair["id"], "lab_test_r1", ARGUMENTS, timeout=.1)
            job = self.bridge.poll(self.token, wait=1)
            with self.assertRaises(MCPError): result.result()
            with self.assertRaises(MCPError): self.bridge.finish(self.token, job["id"], OBSERVED)
            result = worker.submit(self.bridge.collect, "alice", self.pair["id"], "lab_test_r1", ARGUMENTS, timeout=2)
            self.bridge.poll(self.token, wait=1)
            self.bridge.disconnect("alice", self.pair["id"])
            with self.assertRaises(MCPError): result.result()
        self.assertIsNone(self.bridge.status("alice"))
        new = self.bridge.create("alice", "新电脑")
        self.now += 301
        with self.assertRaises(MCPError): self.bridge.claim(new["code"], [TOOL])

    def test_catalog_and_arguments_are_limited_to_explicit_options(self):
        for arguments in ({"command": "system-view"}, {**ARGUMENTS, "target": "other-device"}, {"command": []}):
            with self.assertRaises(MCPError): self.bridge.collect("alice", self.pair["id"], "lab_test_r1", arguments)
        for mutation in (lambda t: t["function"]["parameters"].update(additionalProperties=True),
                         lambda t: t["function"]["parameters"].update(required=[[]]),
                         lambda t: t.update(function=[])):
            tool = deepcopy(TOOL); mutation(tool)
            with self.assertRaises(MCPError): self.bridge.claim("0" * 16, [tool])
        with self.assertRaises(MCPError): self.bridge.claim("非英文连接码", [TOOL])
        self.now += 36
        self.assertEqual(self.bridge.status("alice")["state"], "offline")
        with self.assertRaises(MCPError): self.bridge.collect("alice", self.pair["id"], "lab_test_r1", ARGUMENTS)

    def test_result_delivery_failure_never_reexecutes_device_command(self):
        class Lab:
            bindings = {"lab_test_r1": True}
            count = 0
            def execute(self, *args):
                self.count += 1
                return OBSERVED
        class Unreachable:
            def post(self, *args): raise ConnectionError()
        lab = Lab()
        with self.assertRaises(ConnectionError): execute_job(lab, Unreachable(), {"id": "test", "tool": "lab_test_r1", "arguments": ARGUMENTS})
        self.assertEqual(lab.count, 1)

    def test_site_urls_cannot_embed_credentials_or_follow_redirects(self):
        for url in ("http://example.com", "https://user:secret@example.com", "https://example.com/path", "https://example.com?token=value"):
            with self.assertRaises(MCPError): Website(url)
        self.assertEqual(Website("http://127.0.0.1:8765").url, "http://127.0.0.1:8765")


class BridgeApiTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "runtime/tests"; base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        env = patch.dict(os.environ, {"LABAGENT_MCP_CONFIG": "", "LABAGENT_CLOUD_PERSISTENCE": ""})
        env.start(); self.addCleanup(env.stop)
        self.server = create_server(0, self.folder / "sessions.sqlite3", team_password="test-team", diagnosis_backend="local")
        self.addCleanup(self.server.server_close)
        user, token = self.server.personal.authenticate({"username": "student", "password": "test-student-password"}, register=True)
        self.cookie = "labagent_personal=" + token
        self.user = user

    def request(self, path, payload=None, *, agent=False, token="", cookie=None):
        import base64
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = {"Host": f"127.0.0.1:{self.server.server_port}", "Content-Type": "application/json", "Content-Length": str(len(body))}
        if agent:
            headers["Authorization"] = "Bearer " + token
        else:
            headers["Authorization"] = "Basic " + base64.b64encode(b"labagent:test-team").decode()
            headers["Cookie"] = self.cookie if cookie is None else cookie
        raw = ("POST" if payload is not None else "GET") + " " + path + " HTTP/1.0\r\n"
        raw += "".join(k + ": " + v + "\r\n" for k, v in headers.items())
        sock = RequestSocket(raw.encode() + b"\r\n" + body)
        with patch.object(Handler, "log_message"):
            self.server.RequestHandlerClass(sock, ("127.0.0.1", 12345), self.server)
        head, content = sock.output.getvalue().split(b"\r\n\r\n", 1)
        status, fields = head.split(b"\r\n", 1)
        return int(status.split()[1]), json.loads(content), BytesParser().parsebytes(fields + b"\r\n\r\n")

    def test_pair_through_handlers_execute_real_mcp_and_return_materials(self):
        config = self.folder / "mcp.json"
        config.write_text(json.dumps({"servers": {"test": fixture_spec()}}), encoding="utf-8")
        with LabMCPTools(config) as lab:
            status, pair, _ = self.request("/api/lab/bridge", {"name": "课堂实验电脑"})
            self.assertEqual(status, 201)
            status, grant, headers = self.request("/api/lab/agent/claim", {"code": pair["code"], "tools": lab.tools}, agent=True)
            self.assertEqual(status, 200)
            self.assertIsNone(headers["Set-Cookie"], "A bridge must not acquire team login")
            token = grant["token"]
            self.assertEqual(self.request("/api/lab/tools")[1]["tools"][0]["bridge_id"], pair["id"])
            with ThreadPoolExecutor(1) as worker:
                pending = worker.submit(self.request, "/api/lab/collect", {"bridge_id": pair["id"], "tool": "lab_test_r1", "arguments": ARGUMENTS})
                status, data, _ = self.request("/api/lab/agent/poll", {}, agent=True, token=token)
                self.assertEqual(status, 200)
                test = self
                class Peer:
                    def post(self, path, value):
                        status, data, _ = test.request(path, value, agent=True, token=token)
                        test.assertEqual(status, 200)
                        return data
                self.assertTrue(execute_job(lab, Peer(), data["job"]))
                status, data, _ = pending.result(timeout=3)
            self.assertEqual(status, 200)
            text = next(iter(data["materials"].values()))
            self.assertIn("10.12.0.1/24 up up", text)
            self.assertEqual(data["source"], "mcp_device_observation")
            self.assertNotIn(token, json.dumps(data))
            self.assertEqual(self.request("/api/lab/bridge/disconnect", {"id": pair["id"]})[0], 200)
            self.assertEqual(self.request("/api/lab/agent/poll", {}, agent=True, token=token)[0], 401)

    def test_pair_requires_personal_login_and_agent_token_cannot_read_sessions(self):
        self.assertEqual(self.request("/api/lab/bridge", {"name": "电脑"}, cookie="")[0], 401)
        self.assertEqual(self.request("/api/lab/agent/poll", {}, agent=True, token="invalid")[0], 401)
        self.assertEqual(self.request("/api/lab/tools", agent=True, token="invalid")[0], 401)
        self.assertEqual(self.request("/api/lab/agent/claim", {"code": "bad", "tools": [TOOL]}, agent=True)[0], 400)

    def test_collected_output_advances_current_step_in_same_conversation(self):
        store = self.server.RequestHandlerClass.keywords["store"]
        def run(payload):
            history = store.history(payload["session_id"]) if payload.get("session_id") else None
            observed = any("10.12.0.1/24 up up" in text for text in payload.get("configs", {}).values())
            return run_open_workflow(payload, self.server.workflow_database,
                lambda messages: call("submit_learning_step", step(current_step="分辨Physical和Protocol两列" if observed else "先读取接口状态")),
                previous_attempt=history["attempts"][-1] if history else None)
        self.server.run_diagnosis = run
        with patch.dict(os.environ, {**ENV, "LABAGENT_GUIDED_WORKFLOW": "1"}):
            status, first, _ = self.request("/api/diagnose", {"diagnostic_mode":"open_network", "configs":{},
                "question":"做到一半，不会看接口", "use_llm":True})
            self.assertEqual(status, 200)
            status, second, _ = self.request("/api/diagnose", {"diagnostic_mode":"open_network", "configs":{"MCP-R1.log": OBSERVED["text"]},
                "question":"做到一半，不会看接口", "use_llm":True, "session_id":first["session_id"],
                "interaction":{"type":"verify", "message":"请用刚采集的输出核对当前一步"}})
        self.assertEqual(status, 200)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["investigation"]["learning"]["current_step"], "分辨Physical和Protocol两列")
        history = store.history(first["session_id"])
        self.assertEqual(len(history["attempts"]), 2)
        self.assertEqual(history["attempts"][0]["input"]["configs"], {})
        self.assertEqual(history["attempts"][1]["input"]["configs"]["MCP-R1.log"], OBSERVED["text"])
