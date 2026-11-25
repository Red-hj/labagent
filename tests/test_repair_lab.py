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

    def test_area_mismatch_can_keep_connectivity_on_measured_backup_routes(self):
        path = Path(__file__).resolve().parents[1] / "data/examples/frr_lab02_backup.json"
        result = compare_run(json.loads(path.read_text(encoding="utf-8")))
        fault, repaired = result["phases"]
        self.assertEqual((fault["passed_count"], repaired["passed_count"]), (3, 7))
        self.assertEqual(result["recommended"], "restore")
        checks = {row["id"]: row for row in fault["checks"]}
        for host in ("PC1-ping", "PC2-ping"):
            self.assertTrue(checks[host]["passed"])
            self.assertIn("5发5收", checks[host]["detail"])
        for router in ("R1", "R2"):
            self.assertFalse(checks[f"neighbor_{router}"]["passed"])
            self.assertFalse(checks[f"route_{router}"]["passed"])
            self.assertIn("static", checks[f"route_{router}"]["detail"])
            self.assertIn("S>*", checks[f"route_{router}"]["evidence"])

    def test_measured_partial_repair_stays_incomplete_until_full_retest_is_uploaded(self):
        root = Path(__file__).resolve().parents[1]
        path = root / 'data/examples/frr_lab02_partial.json'
        run = json.loads(path.read_text(encoding='utf-8'))
        result = compare_run(run)
        fault, partial, restored = result['phases']
        self.assertEqual([phase['passed_count'] for phase in (fault, partial, restored)], [0, 2, 8])
        self.assertEqual({row['id'] for row in partial['checks'] if row['passed']}, {'R1-direct-ping', 'R2_transit_admin'})
        self.assertEqual(partial['status'], 'incomplete')
        self.assertEqual(result['recommended'], 'restore')
        self.assertIn('ip link set dev transit up', restored['commands']['R2'])

        temp_root = root / 'runtime/tests'
        temp_root.mkdir(parents=True, exist_ok=True)
        model_settings = {"LABAGENT_LLM_URL": "https://model.invalid", "LABAGENT_LLM_MODEL": "test",
                          "LABAGENT_LLM_KEY": "test-value"}
        with tempfile.TemporaryDirectory(dir=temp_root) as folder, patch.dict(os.environ, model_settings):
            service = MaterialService(Path(folder) / 'partial-repair.sqlite3',
                                      lambda messages, **kwargs: {"content": "未经工具验算的回复"})
            name = 'FRR-分步修复.json'
            first = service.invoke({
                'materials': {name: json.dumps({**run, 'phases': run['phases'][:2]}, ensure_ascii=False)},
                'question': '核对只启用接口后的目标。',
            })
            self.assertEqual(first['status'], 'success')
            self.assertEqual(first['result']['llm']['status'], 'no_evidence')
            self.assertIn('2/8', first['reply'])
            self.assertIn('尚未满足全部实验目标', first['reply'])
            self.assertIsNone(first['result']['repair_comparisons'][0]['recommended'])
            second = service.invoke({
                'session_id': first['session_id'], 'materials': {name: path.read_text(encoding='utf-8')},
                'action': 'verify', 'message': '已补采完整修复结果，请核对。',
            })
            self.assertEqual(second['session_id'], first['session_id'])
            self.assertEqual(second['status'], 'success')
            self.assertEqual(second['result']['llm']['status'], 'no_evidence')
            self.assertEqual(second['result']['status'], 'comparison_complete')
            self.assertIn('8/8', second['reply'])
            self.assertEqual(second['result']['repair_comparisons'][0]['recommended'], 'restore')
            if second['result']['workflow']['backend'] == 'langgraph':
                self.assertEqual(len(second['result']['conversation']), 2)
            report = generate_report(service.store.history(first['session_id']))
            self.assertIn('2/8', report)
            self.assertIn('8/8', report)
            self.assertIn('FRRouting 10.2.2', report)

    def test_mixed_materials_keep_the_open_diagnosis_response(self):
        payload = {'configs': {'实测.json': json.dumps(recording()), '拓扑说明.txt': 'PC1 接 R1。'},
                   'question': '检查拓扑和实验记录。', 'use_llm': False}
        result = investigate(payload)
        self.assertTrue(result['repair_comparisons'])
        self.assertNotEqual(result['status'], 'comparison_complete')

    def test_changed_devices_addresses_and_fault_location_reuse_the_evaluator(self):
        path=Path(__file__).resolve().parents[1]/"data/examples/frr_repair_variant.json"
        record=json.loads(path.read_text(encoding="utf-8"));result=compare_run(record)
        self.assertEqual(set(record["phases"][0]["observations"]),{"Edge","Core","Access"})
        self.assertEqual([row["passed_count"] for row in result["phases"]],[1,1,3])
        self.assertEqual(result["recommended"],"ospf")
        self.assertIn("Core",result["phases"][2]["commands"])
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
