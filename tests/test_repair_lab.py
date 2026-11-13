from copy import deepcopy
import unittest

from labagent.repair_lab import RUN_KIND, compare_run, route_to
from labagent.investigation import source_bundle,execute_tool,investigate
from labagent.store import Store
from labagent.report import generate_report
from pathlib import Path
import json
import tempfile
import os
from unittest.mock import patch
from labagent.material_service import MaterialService


def recording():
    def phase(key, protocol, received, static=False):
        return {"id":key,"title":key,"commands":{},"observations":{"R1":{
            "config":"hostname R1\n" + ("ip route 10.3.3.3/32 10.12.0.2\n" if static else ""),
            "routes":{"10.3.3.3/32":[{"protocol":protocol,"selected":True,"installed":True}]},
            "pings":{"ping":"PING 10.3.3.3\n3 packets transmitted, " + str(received) + " packets received"}}}}
    goals=[{"id":"ping","title":"Ping通过","kind":"ping","device":"R1","target":"10.3.3.3","count":3},
           {"id":"route","title":"OSPF路由","kind":"route_protocol","device":"R1","target":"10.3.3.3/32","expected":"ospf"},
           {"id":"static","title":"动态路由配置","kind":"config_absent","device":"R1","target":"ip route "}]
    return {"kind":RUN_KIND,"schema_version":1,"run_id":"unit","title":"对照","environment":{"engine":"FRRouting"},"goals":goals,
            "phases":[phase("baseline","connected",0),phase("static","static",3,True),phase("ospf","ospf",3)]}


class RepairLabTests(unittest.TestCase):
    def test_comparison_can_complete_without_requesting_unrelated_materials(self):
        payload={"materials":{"运行.json":json.dumps(recording(),ensure_ascii=False,indent=2)},"question":"比较这两种方案"}
        def completion(messages):
            name="submit_hypotheses" if any(item["role"]=="tool" for item in messages) else "compare_repair_candidates"
            args={"summary":"OSPF方案满足全部目标","hypotheses":[],"evidence_requests":[]} if name=="submit_hypotheses" else {"source_id":"config:1"}
            return {"tool_calls":[{"id":name,"function":{"name":name,"arguments":json.dumps(args)}}]}
        root=Path(__file__).resolve().parents[1]/"runtime/tests";root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder,patch.dict(os.environ,{"LABAGENT_LLM_URL":"https://model.invalid","LABAGENT_LLM_MODEL":"test","LABAGENT_LLM_KEY":"test-value"}):
            response=MaterialService(Path(folder)/"sessions.sqlite3",completion).invoke(payload)
            self.assertEqual(response["result"]["status"],"comparison_complete")
            self.assertFalse(response["result"]["workflow"]["paused"])

    def test_tool_returns_original_line_citations_and_masks_sensitive_config(self):
        run=recording();run["phases"][1]["observations"]["R1"]["config"]+="password PRIVATE-CONFIG-VALUE\n"
        payload={"configs":{"运行.json":json.dumps(run,ensure_ascii=False,indent=2)},"question":"比较"};sources=source_bundle(payload)[0]
        result,_=execute_tool("compare_repair_candidates",{"source_id":"config:1"},payload,sources,set(),diagnostic_context={})
        self.assertNotIn("PRIVATE-CONFIG-VALUE",json.dumps(result))
        self.assertTrue(result["source_evidence"])
        for cite in result["source_evidence"]:
            self.assertEqual(sources[cite["source_id"]]["lines"][cite["line"]-1],cite["quote"])
    def test_tool_reads_observations_and_result_report_survive_restore(self):
        run=recording()
        payload={"diagnostic_mode":"open_network","configs":{"运行.json":json.dumps(run,ensure_ascii=False,indent=2)},"question":"比较修复方案","use_llm":False}
        sources=source_bundle(payload)[0];seen=set()
        compared,_=execute_tool("compare_repair_candidates",{"source_id":"config:1"},payload,sources,seen)
        self.assertEqual(compared["recommended"],"ospf")
        self.assertIn(("config:1",len(sources["config:1"]["lines"])),seen)
        result=investigate(payload)
        self.assertEqual(result["repair_comparisons"][0]["recommended"],"ospf")
        root=Path(__file__).resolve().parents[1]/"runtime/tests";root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            store=Store(Path(folder)/"repair.sqlite3");saved=store.save(payload,result)
            report=generate_report(store.history(saved["session_id"]))
            self.assertIn("候选修复的运行对照",report)
            self.assertIn("满足全部实验目标的方案：ospf",report)

    def test_actual_frr_run_matches_live_protocol_and_ping_observations(self):
        path=Path(__file__).resolve().parents[1]/"data/examples/frr_repair_run.json"
        result=compare_run(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual([row["passed_count"] for row in result["phases"]],[1,1,3])
        self.assertEqual(result["recommended"],"ospf")
        self.assertIn("quay.io/frrouting/frr@sha256:",result["environment"]["image_digest"])
    def test_connectivity_recovery_does_not_complete_ospf_requirements(self):
        result=compare_run(recording())
        self.assertEqual([row["passed_count"] for row in result["phases"]],[1,1,3])
        self.assertTrue(result["phases"][1]["checks"][0]["passed"])
        self.assertFalse(result["phases"][1]["checks"][1]["passed"])
        self.assertEqual(result["recommended"],"ospf")
        self.assertIn("+ip route",result["phases"][1]["changes"][0]["diff"])

    def test_claimed_result_is_recomputed_from_observations(self):
        run=recording();run["phases"][1].update(status="passed",passed_count=3)
        self.assertEqual(compare_run(run)["phases"][1]["status"],"incomplete")

    def test_independent_candidates_are_not_merged_into_one_current_ping_test(self):
        payload={"configs":{"实验.json":json.dumps(recording(),ensure_ascii=False,indent=2)},"question":"比较"}
        sources=source_bundle(payload)[0]
        checked,_=execute_tool("check_submitted_pings",{},payload,sources,set())
        self.assertEqual(checked["pings"],[])

    def test_uninstalled_ospf_route_does_not_satisfy_the_route_goal(self):
        run=recording();run["phases"][2]["observations"]["R1"]["routes"]["10.3.3.3/32"][0]["installed"]=False
        self.assertIsNone(compare_run(run)["recommended"])

    def test_longest_prefix_determines_the_actual_forwarding_route(self):
        table={"0.0.0.0/0":[{"protocol":"static"}],"10.3.3.0/24":[{"protocol":"ospf"}]}
        self.assertEqual(route_to(table,"10.3.3.3/32")[0]["protocol"],"ospf")
        self.assertEqual(route_to(table,"192.168.1.1")[0]["protocol"],"static")

    def test_comparison_keeps_imported_observations_unchanged(self):
        run=recording();original=deepcopy(run);compare_run(run);self.assertEqual(run,original)

    def test_unknown_format_and_missing_baseline_have_clear_errors(self):
        with self.assertRaisesRegex(ValueError,"运行记录"):
            compare_run({})
        run=recording();run["phases"][0]["id"]="fixed"
        with self.assertRaisesRegex(ValueError,"baseline"):
            compare_run(run)


if __name__ == "__main__":
    unittest.main()
