"""Cloud-to-workstation observations through real handlers, graph and stdio MCP."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from labagent.lab_bridge import LabBridge
from labagent.cloud_workflow import CloudWorkflow
from labagent.lab_relay import RelayLabTools
from labagent.material_service import MaterialService
from labagent.mcp_client import LabMCPTools, MCPError
from labagent.server import create_server
from labagent.store import Store
import test_lab_bridge as bridge_fixture
from test_mcp_learning import ENV, fixture_spec, call, step

ROOT = Path(__file__).resolve().parents[1]
RELAY = "https://lab.example/api/lab/runtime/collect"
TOOL, ARGUMENTS, OBSERVED = bridge_fixture.TOOL, bridge_fixture.ARGUMENTS, bridge_fixture.OBSERVED


class RelayGrantTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.bridge = LabBridge(clock=lambda: self.now)
        self.pair = self.bridge.create("alice", "实验电脑")
        self.agent = self.bridge.claim(self.pair["code"], [TOOL])["token"]

    def test_grants_are_owner_scoped_and_close_with_the_diagnosis(self):
        with self.bridge.runtime_connection("bob", "https://lab.example") as missing:
            self.assertIsNone(missing)
        with self.bridge.runtime_connection("alice", "https://lab.example") as connection:
            self.assertEqual(connection["url"], RELAY)
            self.assertNotEqual(connection["token"], self.agent)
            with ThreadPoolExecutor(1) as worker:
                pending = worker.submit(self.bridge.runtime_collect, connection["token"], "lab_test_r1", ARGUMENTS)
                job = self.bridge.poll(self.agent, wait=1)
                self.bridge.finish(self.agent, job["id"], OBSERVED)
                self.assertEqual(pending.result(), OBSERVED)
        with self.assertRaises(PermissionError): self.bridge.runtime_collect(connection["token"], "lab_test_r1", ARGUMENTS)
        self.assertEqual(self.bridge.runtime_grants, {})

    def test_disconnect_and_failed_invocation_revoke_grants(self):
        with self.bridge.runtime_connection("alice", "https://lab.example") as connection:
            self.bridge.disconnect("alice", self.pair["id"])
            with self.assertRaises(PermissionError): self.bridge.runtime_collect(connection["token"], "lab_test_r1", ARGUMENTS)
        self.assertEqual(self.bridge.runtime_grants, {})
        pair = self.bridge.create("alice", "新电脑"); self.bridge.claim(pair["code"], [TOOL])
        with self.assertRaisesRegex(ValueError, "cloud failed"):
            with self.bridge.runtime_connection("alice", "https://lab.example") as failed:
                raise ValueError("cloud failed")
        with self.assertRaises(PermissionError): self.bridge.runtime_collect(failed["token"], "lab_test_r1", ARGUMENTS)

    def test_expired_grant_and_unlisted_command_are_rejected(self):
        with self.bridge.runtime_connection("alice", "https://lab.example") as connection:
            with self.assertRaises(MCPError): self.bridge.runtime_collect(connection["token"], "lab_test_r1", {"command":"system-view"})
            self.now += 601
            with self.assertRaises(PermissionError): self.bridge.runtime_collect(connection["token"], "lab_test_r1", ARGUMENTS)


class RelayAdapterTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"LABAGENT_LAB_RELAY_URL":RELAY}); env.start(); self.addCleanup(env.stop)
        self.connection = {"url":RELAY, "token":"a" * 43, "tools":[TOOL]}

    def test_trusted_endpoint_and_tool_choices_are_checked_before_sending(self):
        for connection in ({**self.connection,"url":"https://other.example/collect"}, {**self.connection,"token":"bad"}):
            with self.assertRaises(MCPError): RelayLabTools(connection)
        tools = RelayLabTools(self.connection)
        with patch("labagent.lab_relay.build_opener") as opener:
            with self.assertRaises(MCPError): tools.execute("lab_test_r1", {"command":"system-view"})
            opener.assert_not_called()

    def test_transport_failure_is_not_retried_and_does_not_expose_credentials(self):
        tools = RelayLabTools(self.connection)
        with patch("labagent.lab_relay.build_opener") as factory:
            factory.return_value.open.side_effect = HTTPError(RELAY, 401, "private text", {}, None)
            with self.assertRaises(MCPError) as error: tools.execute("lab_test_r1", ARGUMENTS)
            self.assertEqual(factory.return_value.open.call_count,1)
        self.assertNotIn("private", str(error.exception)); self.assertNotIn(self.connection["token"], str(error.exception))


class CloudRelayFlowTests(unittest.TestCase):
    request = bridge_fixture.BridgeApiTests.request

    def setUp(self):
        base = ROOT / "runtime/tests"; base.mkdir(parents=True,exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base); self.addCleanup(temp.cleanup); self.folder = Path(temp.name)
        env = patch.dict(os.environ, {**ENV, "LABAGENT_MCP_CONFIG":"", "LABAGENT_CLOUD_PERSISTENCE":"",
            "LABAGENT_GUIDED_WORKFLOW":"1", "LABAGENT_LAB_RELAY_URL":RELAY})
        env.start(); self.addCleanup(env.stop)
        self.tokens, self.messages, self.requests = [], [], []
        self.service = MaterialService(self.folder / "cloud.sqlite3")
        outer = self
        class Client:
            def invoke(self, request):
                outer.requests.append(deepcopy(request))
                return outer.service.invoke(request)
        self.server = create_server(0, self.folder / "web.sqlite3", team_password="test-team",
            diagnosis_backend="agentarts", cloud_client_factory=lambda session:Client())
        self.addCleanup(self.server.server_close)
        self.server.public_url = "https://lab.example"
        self.user, token = self.server.personal.authenticate({"username":"student","password":"test-student-password"},register=True)
        self.cookie = "labagent_personal=" + token
        class Opener:
            def open(self, request, timeout):
                outer.assertEqual(request.full_url,RELAY)
                token=request.get_header("Authorization")[7:]; outer.tokens.append(token)
                status, data, _ = outer.request("/api/lab/runtime/collect", json.loads(request.data), agent=True, token=token)
                if status != 200: raise HTTPError(RELAY,status,"relay failed",{},None)
                return io.BytesIO(json.dumps(data).encode())
        transport=patch("labagent.lab_relay.build_opener", return_value=Opener()); transport.start(); self.addCleanup(transport.stop)
        def model(messages, **options):
            self.messages.extend(deepcopy(messages))
            if messages[-1]["role"] != "tool":
                names=[t["function"]["name"] for t in options["tools"]]
                self.assertIn("lab_test_r1",names)
                return call("lab_test_r1",ARGUMENTS)
            observed=json.loads(messages[-1]["content"]); row=observed["lines"][-1]
            return call("submit_learning_step",step(evidence=[{"source_id":observed["source_id"],"line":row["line"],"quote":row["text"]}]))
        model_patch=patch("labagent.investigation.chat_completion",side_effect=model); model_patch.start(); self.addCleanup(model_patch.stop)

    def test_cloud_model_collects_real_stdio_output_and_saves_citations_without_tokens(self):
        config=self.folder / "mcp.json"; config.write_text(json.dumps({"servers":{"test":fixture_spec()}}),encoding="utf-8")
        with LabMCPTools(config) as lab:
            pair=self.server.lab_bridge.create(self.user["id"],"课堂电脑")
            agent=self.server.lab_bridge.claim(pair["code"],lab.tools)["token"]
            payload={"diagnostic_mode":"open_network","configs":{},"question":"帮我看看接口状态","use_llm":True}
            with ThreadPoolExecutor(1) as worker:
                pending=worker.submit(self.request,"/api/diagnose",payload)
                job=self.server.lab_bridge.poll(agent,wait=5)
                self.assertIsNotNone(job)
                observed=lab.execute(job["tool"],job["arguments"])
                self.server.lab_bridge.finish(agent,job["id"],observed)
                status,result,_=pending.result(timeout=10)
                self.assertEqual(status,200,result)
                self.assertTrue(result["collected_materials"])
                followup={**payload,"session_id":result["session_id"],"configs":result["collected_materials"],
                    "interaction":{"type":"verify","message":"我修改后请重新读取接口状态"}}
                pending=worker.submit(self.request,"/api/diagnose",followup)
                job=self.server.lab_bridge.poll(agent,wait=5)
                self.assertIsNotNone(job)
                observed=lab.execute(job["tool"],job["arguments"])
                observed["text"] += "\nsecond collection marker"
                self.server.lab_bridge.finish(agent,job["id"],observed)
                status,second,_=pending.result(timeout=10)
                self.assertEqual(status,200,second)
                self.assertEqual(second["session_id"],result["session_id"])
                self.assertIn("second collection marker",next(iter(second["collected_materials"].values())),"A refreshed tool output must replace the same saved material on the website")
        self.assertEqual(status,200,result)
        self.assertEqual(result["llm"]["status"],"ok")
        evidence=result["investigation"]["learning"]["evidence"][0]
        self.assertEqual(result["collected_materials"][evidence["device"]].splitlines()[evidence["line"]-1],evidence["quote"])
        store=self.server.RequestHandlerClass.keywords["store"]
        history=store.history(result["session_id"])
        self.assertIn("10.12.0.1/24 up up",json.dumps(history,ensure_ascii=False))
        cloud=self.service.store.history(result["agentarts"]["session_id"])
        for token in self.tokens:
            self.assertNotIn(token,json.dumps(history)); self.assertNotIn(token,json.dumps(cloud)); self.assertNotIn(token,json.dumps(self.messages))
            for database in self.folder.glob("*.sqlite3"): self.assertNotIn(token.encode(),database.read_bytes())
            self.assertEqual(self.request("/api/lab/runtime/collect",{"tool":"lab_test_r1","arguments":ARGUMENTS},agent=True,token=token)[0],401)
        self.assertEqual(self.server.lab_bridge.runtime_grants,{})
        self.assertEqual(len(self.tokens),2,"One device command is sent per requested turn")
        self.assertEqual(len(set(self.tokens)),2,"Each turn uses a fresh grant")

    def test_agent_token_cannot_be_used_as_a_cloud_diagnosis_grant(self):
        pair=self.server.lab_bridge.create(self.user["id"],"电脑")
        token=self.server.lab_bridge.claim(pair["code"],[TOOL])["token"]
        self.assertEqual(self.request("/api/lab/runtime/collect",{"tool":"lab_test_r1","arguments":ARGUMENTS},agent=True,token=token)[0],401)

    def test_other_account_and_forged_request_cannot_select_the_paired_computer(self):
        pair=self.server.lab_bridge.create(self.user["id"],"电脑")
        self.server.lab_bridge.claim(pair["code"],[TOOL])
        _, token=self.server.personal.authenticate({"username":"other","password":"test-student-password"},register=True)
        payload={"diagnostic_mode":"open_network","configs":{},"question":"接口在哪看","use_llm":True,
            "lab_owner":self.user["id"],"lab_connection":{"url":RELAY,"token":"forged","tools":[TOOL]}}
        with patch("labagent.investigation.chat_completion",return_value=call("submit_learning_step",step())):
            status,result,_=self.request("/api/diagnose",payload,cookie="labagent_personal="+token)
        self.assertEqual(status,200,result)
        self.assertNotIn("lab_connection",self.requests[-1])
        self.assertEqual(self.server.lab_bridge.runtime_grants,{})

    def test_old_runtime_must_acknowledge_the_temporary_tools(self):
        class OldClient:
            def invoke(self, request): return {"session_id":"a"*32}
        workflow=CloudWorkflow(Store(self.folder / "old-web.sqlite3"),lambda session:OldClient())
        with self.assertRaisesRegex(ValueError,"尚不支持实验电脑工具回传"):
            workflow.run({"configs":{}},lab_connection={"url":RELAY,"token":"a"*43,"tools":[TOOL]})
