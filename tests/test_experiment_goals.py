from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.demos import ospf_goals_demo, network_access_demo
from labagent.diagnostic_planner import rank_checks, validate_plan
from labagent.experiment_goals import check_goals
from labagent.investigation import source_bundle, execute_tool, investigate
from labagent.report import generate_report
from labagent.store import Store
from labagent.workflow import run_open_workflow, available
from test_investigation import ENV

ROOT = Path(__file__).resolve().parents[1]


def payload(stage=0):
    return {"diagnostic_mode": "open_network", "question": "按说明完成OSPF实验", "use_llm": True,
            "configs": ospf_goals_demo()["stages"][stage]["materials"], "evidence_kind": "demo"}


def goals():
    lines = payload()["configs"]["实验说明.txt"].splitlines()
    def item(number, kind, target, expected=""):
        return {"id": f"G{number}", "title": lines[number - 1], "kind": kind, "device": "R1", "target": target,
                "expected": expected, "instruction": {"source_id": "config:1", "line": number, "quote": lines[number - 1]}}
    return [item(1, "reachability", "10.3.3.3"), item(2, "route_protocol", "10.3.3.3/32", "OSPF"), item(3, "config_absent", "ip route-static")]


def checked(stage):
    return check_goals(goals(), source_bundle(payload(stage))[0])


def call(name, args):
    return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


def scripted_completion(stage, *, falsely_pass=False):
    def complete(messages):
        tools = [message for message in messages if message["role"] == "tool"]
        if not tools:
            return call("read_source", {"source_id": "config:1", "start_line": 1, "end_line": 3})
        if len(tools) == 1:
            return call("check_experiment_goals", {"goals": goals()} if stage == 0 else {})
        if len(tools) == 2:
            return call("check_submitted_pings", {})
        return call("submit_hypotheses", {"summary": "全部目标完成" if stage == 2 or falsely_pass else "访问已恢复，OSPF要求仍未满足。",
                    "hypotheses": [], "evidence_requests": [{"device": "R1", "information": "当前路由来源", "why": "核对OSPF目标"}] if stage == 0 else [],
                    "outcome": "observations_passed" if stage == 2 or falsely_pass else "investigating", "verification_targets": ["10.3.3.3"]})
    return complete


class ExperimentGoalTests(unittest.TestCase):
    def test_explicit_backup_repair_replaces_wrong_mixed_diagnosis(self):
        from scripts.evaluate_diagnostic_workflow import cases

        current = cases()[0]["frr-static-fallback"]
        sources = source_bundle(current)[0]
        line = sources["config:1"]["lines"]
        criteria = [{"id": str(n), "title": line[n - 1], "kind": kind, "device": device,
                     "target": "192.168.20.10", "expected": expected,
                     "instruction": {"source_id": "config:1", "line": n, "quote": line[n - 1]}}
                    for n, kind, device, expected in ((1, "reachability", "PC1", ""), (2, "route_protocol", "R1", "OSPF"))]
        submission = {"summary": "静态 200 会压过 OSPF 110", "reply": "删掉备用静态路由。",
                      "hypotheses": [], "evidence_requests": [{"device": "R1", "information": "删除后路由表", "why": "复测"}],
                      "steps": ["在 R1 删除静态路由。"]}
        for permission in (True, False):
            if not permission:
                current["configs"]["实验说明.txt"] = "\n".join(line[:3])
                sources = source_bundle(current)[0]
            context, seen = {}, {("config:1", 1), ("config:1", 2)}
            execute_tool("check_experiment_goals", {"goals": criteria}, current, sources, seen, diagnostic_context=context)
            result, _ = execute_tool("submit_hypotheses", submission, current, sources, seen, diagnostic_context=context)
            if permission:
                self.assertEqual(result["outcome"], "ready_for_retest")
                self.assertIn("保留允许的备用静态路由", result["reply"])
                self.assertIn("no ip ospf area 0.0.0.1", result["steps"][1])
                self.assertNotIn("删除", json.dumps([result["reply"], result["steps"]], ensure_ascii=False))
            else:
                self.assertEqual(result["outcome"], "investigating")

    def test_explicit_frr_area_goal_expands_both_endpoints_despite_weak_model_extraction(self):
        from scripts.evaluate_diagnostic_workflow import cases

        requests, _ = cases()
        for name, statuses in (("frr-static-fallback", ["passed", "failed"]),
                               ("frr-healthy-with-backup", ["passed", "passed"])):
            current = requests[name]
            sources, _ = source_bundle(current)
            weak = {"id": "weak", "title": "接口存在", "kind": "config_contains", "device": "R1",
                    "target": "interface transit", "expected": "", "instruction": {
                        "source_id": "config:1", "line": 3, "quote": sources["config:1"]["lines"][2]}}
            seen = {("config:1", 3)}
            result, _ = execute_tool("check_experiment_goals", {"goals": [weak]}, current, sources, seen)
            self.assertEqual([(g["device"], g["target"], g["expected"], g["status"]) for g in result["checks"]],
                             [(device, "transit", "0.0.0.0", status) for device, status in zip(("R1", "R2"), statuses)])
            for goal in result["checks"]:
                self.assertEqual(len(goal["evidence"]), 2)
                for ref in goal["evidence"]:
                    self.assertEqual(ref["quote"], sources[ref["source_id"]]["lines"][ref["line"] - 1])
            if name == "frr-static-fallback":
                with self.assertRaisesRegex(ValueError, "实验目标还未全部满足"):
                    execute_tool("submit_hypotheses", {"outcome": "observations_passed"}, current, sources, {("config:1", 3)})

    def test_missing_endpoint_or_unrelated_area_line_cannot_pass(self):
        current = {"configs": {"实验说明.txt": "R1、R2 的 transit 接口必须在 Area 0。",
                               "R1.cfg": "interface transit\n ip ospf area 0\nexit",
                               "R2.cfg": "interface transit\nexit\ninterface lan\n ip ospf area 0\nexit"}}
        sources, _ = source_bundle(current)
        result = check_goals([], sources)
        self.assertEqual([g["status"] for g in result["checks"]], ["passed", "needs_evidence"])
        self.assertEqual(result["status"], "in_progress")

    def test_area_goal_does_not_invent_an_interface_from_topology_only(self):
        current = payload()
        sources, _ = source_bundle(current)
        goal = {"id": "a1", "title": "区域", "kind": "ospf_interface_area", "device": "R3",
                "target": "GigabitEthernet0/0/0", "expected": "0", "instruction": {
                    "source_id": "config:1", "line": 4, "quote": sources["config:1"]["lines"][3]}}
        with self.assertRaisesRegex(ValueError, "原文须同时写出设备、接口"):
            execute_tool("check_experiment_goals", {"goals": [goal]}, current, sources, {("config:1", 4)})

    def test_vrp_interface_area_can_follow_network_wildcard_and_keeps_original_citations(self):
        current = {"question": "R1 的 GE0/0/0 接口必须在 Area 1。", "configs": {"R1.cfg":
                   "sysname R1\n#\ninterface GigabitEthernet0/0/0\n ip address 10.1.2.3 255.255.255.0\n#\n"
                   "ospf 1\n area 0.0.0.1\n  network 10.1.2.0 0.0.0.255\n#\nreturn"}}
        result = check_goals([], source_bundle(current)[0])["checks"][0]
        self.assertEqual(result["status"], "passed")
        self.assertEqual([ref["line"] for ref in result["evidence"]], [3, 4, 7, 8])

    def test_marked_excerpt_with_return_cannot_prove_command_absent(self):
        current = payload(2)
        current["configs"]["R1.cfg"] = "sysname R1\n# 配置摘录\n#\nreturn"
        sources, _ = source_bundle(current)
        result = check_goals(goals(), sources)
        self.assertEqual(result["checks"][2]["status"], "needs_evidence")
        read, _ = execute_tool("read_source", {"source_id": "config:2", "start_line": 1, "end_line": 4}, current, sources, set())
        self.assertTrue(read["range_complete"])
        self.assertEqual(read["configuration_coverage"], [{"device": "R1", "start_line": 1, "end_line": 4,
                                                               "scope": "partial_or_unconfirmed", "all_lines_read": True}])

    def test_reading_some_lines_of_complete_config_does_not_prove_absence_to_model(self):
        current = payload(2)
        sources, _ = source_bundle(current)
        seen = set()
        last = len(sources["config:2"]["lines"])
        for first, end, read_full in ((1, 2, False), (3, last, True)):
            read, _ = execute_tool("read_source", {"source_id": "config:2", "start_line": first, "end_line": end}, current, sources, seen)
            self.assertTrue(read["range_complete"])
            self.assertEqual(read["configuration_coverage"][0]["scope"], "complete_snapshot")
            self.assertEqual(read["configuration_coverage"][0]["all_lines_read"], read_full)

    def test_live_frr_route_rows_distinguish_static_fallback_from_unselected_static(self):
        run = json.loads((ROOT / "data/examples/frr_lab02_backup.json").read_text(encoding="utf-8"))
        for phase, protocol in zip(run["phases"], ("Static", "OSPF")):
            current = {"configs": {f"{router}-路由.log": data["route_output"] for router, data in phase["observations"].items()
                                   if router in ("R1", "R2")}}
            sources, _ = source_bundle(current)
            for router, target, hop in (("R1", "192.168.20.10", "10.12.0.2"), ("R2", "192.168.10.10", "10.12.0.1")):
                seen = set()
                checked, _ = execute_tool("check_route_selection", {"device": router, "target": target}, current, sources, seen)
                self.assertEqual(checked["status"], "observed")
                self.assertEqual([(item["protocol"], item["next_hop"]) for item in checked["routes"]], [(protocol, hop)])
                ref = checked["evidence"][0]
                self.assertIn((ref["source_id"], ref["line"]), seen)
                self.assertEqual(ref["quote"], sources[ref["source_id"]]["lines"][ref["line"] - 1])

    def test_route_selection_needs_a_table_for_the_right_device_and_unfiltered_scope(self):
        legend = "       > - selected route, * - FIB route, q - queued, r - rejected, b - backup\n"
        route = "S>* 192.168.20.0/24 [200/0] via 10.12.0.2, transit"
        materials = {
            "R1.cfg": "hostname R1\nip route 192.168.20.0/24 10.12.0.2 200",
            "R1-filtered.log": "R1# show ip route static\n" + legend + route,
            "R1-mislabeled.log": "R2# show ip route\n" + legend + route,
        }
        current = {"configs": materials}
        sources, _ = source_bundle(current)
        checked, _ = execute_tool("check_route_selection", {"device": "R1", "target": "192.168.20.10"}, current, sources, set())
        self.assertEqual(checked["status"], "needs_evidence")
        self.assertEqual(checked["routes"], [])
        self.assertIn("show ip route", checked["request"]["information"])

    def test_route_selection_prefers_longest_installed_prefix_and_keeps_equal_cost_next_hops(self):
        table = ("R1# show ip route\n       > - selected route, * - FIB route, q - queued, r - rejected, b - backup\n"
                 "S>* 0.0.0.0/0 [200/0] via 10.12.0.2, transit\n"
                 "O>  192.168.20.0/24 [110/20] via 10.12.0.2, transit\n"
                 "O>* 192.168.20.10/32 [110/20] via 10.12.0.2, transit\n"
                 "  *                          via 10.12.0.3, transit\n")
        current = {"configs": {"R1.log": table}}
        sources, _ = source_bundle(current)
        for target, choices in (("192.168.20.10", ["10.12.0.2", "10.12.0.3"]), ("192.168.20.11", ["10.12.0.2"]),
                                ("192.168.20.0/24", [])):
            result, _ = execute_tool("check_route_selection", {"device": "R1", "target": target}, current, sources, set())
            self.assertEqual([entry["next_hop"] for entry in result["routes"]], choices)

    def test_vrp_route_selection_uses_the_downloaded_row_and_preserves_interface(self):
        table = ("<R1>display ip routing-table\nDestination/Mask Proto Pre Cost Flags NextHop Interface\n"
                 "192.168.20.0/24 OSPF 10 20 R 10.12.0.2 GigabitEthernet 1/0/1\n"
                 "192.168.20.0/24 Static 60 0 RD 10.12.0.3 GigabitEthernet 1/0/2\n")
        current = {"configs": {"R1.log": table}}
        sources, _ = source_bundle(current)
        result, _ = execute_tool("check_route_selection", {"device": "R1", "target": "192.168.20.10"}, current, sources, set())
        self.assertEqual(result["routes"], [{"network": "192.168.20.0/24", "protocol": "Static",
                                            "next_hop": "10.12.0.3", "interface": "GigabitEthernet 1/0/2"}])

    def test_route_tool_sets_the_missing_request_and_accepts_a_complete_route_answer(self):
        materials = {"R1.log": "<R1>display ip routing-table\nDestination/Mask Proto Pre Cost Flags NextHop Interface\n"
                     "192.168.20.0/24 OSPF 10 20 D 10.12.0.2 GE1/0/1"}
        for configs, expected in ((materials, "observed"), ({"R1.cfg": "sysname R1"}, "needs_evidence")):
            current, context, seen = {"configs": configs}, {}, set()
            sources, _ = source_bundle(current)
            result, _ = execute_tool("check_route_selection", {"device": "R1", "target": "192.168.20.10"},
                                    current, sources, seen, diagnostic_context=context)
            self.assertEqual(result["status"], expected)
            submitted, finished = execute_tool("submit_hypotheses", {"summary": result["detail"], "hypotheses": [], "evidence_requests": []},
                                              current, sources, seen, diagnostic_context=context)
            self.assertTrue(finished)
            self.assertEqual(submitted["evidence_requests"], [] if expected == "observed" else [result["request"]])

    def test_route_table_reply_uses_only_checked_rows_even_if_model_adds_wrong_advice(self):
        run = json.loads((ROOT / "data/examples/frr_lab02_backup.json").read_text(encoding="utf-8"))
        table = run["phases"][0]["observations"]["R1"]["route_output"]
        for configs in ({"R1-routes.log": table}, {"R1.cfg": "hostname R1"}):
            with self.subTest(configs=list(configs)):
                current, context, seen = {"configs": configs}, {}, set()
                sources, _ = source_bundle(current)
                query = {"device": "R1", "target": "192.168.20.10"}
                checked, _ = execute_tool("check_route_selection", query, current, sources, seen, diagnostic_context=context)
                wrong_reply = "管理距离 200 低于 110，删除备用静态路由。"
                response = {"answer_mode": "route_table", "summary": wrong_reply, "reply": wrong_reply,
                            "steps": ["删除备用静态路由"], "hypotheses": [], "evidence_requests": []}
                submitted, finished = execute_tool("submit_hypotheses", response, current, sources, seen, diagnostic_context=context)
                self.assertTrue(finished)
                self.assertEqual(submitted["reply"], checked["detail"])
                self.assertEqual(submitted["summary"], checked["detail"])
                self.assertEqual(submitted["steps"], [])
                self.assertEqual(submitted["evidence_requests"], [checked["request"]] if checked["request"] else [])
                self.assertNotIn("200", submitted["reply"])
                if checked["status"] == "observed":
                    self.assertIn("出接口 transit", submitted["reply"])

    def test_route_table_answer_requires_a_current_route_check(self):
        current = {"configs": {"R1.cfg": "hostname R1"}}
        sources, _ = source_bundle(current)
        with self.assertRaisesRegex(ValueError, "先调用check_route_selection"):
            execute_tool("submit_hypotheses", {"answer_mode": "route_table"}, current, sources, {("config:1", 1)})

    def test_device_config_section_in_combined_material_is_cited_and_checked(self):
        text = (ROOT / "data/examples/实验诊断材料_终端跨网段访问.txt").read_text(encoding="utf-8")
        criterion = {"id":"gateway","title":"R1网关配置","kind":"config_contains","device":"R1",
                     "target":"gateway-list 10.20.0.1","expected":"",
                     "instruction":{"source_id":"config:1","line":7,"quote":text.splitlines()[6]}}
        current = {"configs":{"综合材料.txt":text.replace("10.20.0.254","10.20.0.1")}}
        sources = source_bundle(current)[0]
        result = check_goals([criterion],sources)["checks"][0]
        self.assertEqual(result["status"],"passed")
        self.assertEqual(result["evidence"][0]["line"],28)
        self.assertEqual(result["evidence"][0]["quote"],sources["config:1"]["lines"][27])

    def test_config_excerpt_does_not_prove_absence_or_another_devices_config(self):
        goal = {"id":"c","title":"检查网关","kind":"config_absent","device":"R1",
                "target":"gateway-list 10.20.0.254","expected":"","instruction":{}}
        for text in ("三、R1相关配置节选\ngateway-list 10.20.0.1\n四、PC1当前地址信息\nDefault Gateway: 10.20.0.1",
                     "三、R2相关配置节选\ngateway-list 10.20.0.1\nreturn"):
            sources=source_bundle({"configs":{"材料.txt":text}})[0]
            self.assertEqual(check_goals([goal],sources)["checks"][0]["status"],"needs_evidence")

    def test_config_section_ends_at_next_document_section(self):
        goal = {"id":"c","title":"检查网关","kind":"config_contains","device":"R1",
                "target":"gateway-list 10.20.0.1","expected":"","instruction":{}}
        text="三、R1相关配置节选\ngateway-list 10.20.0.254\n四、修改建议\ngateway-list 10.20.0.1"
        self.assertEqual(check_goals([goal],source_bundle({"configs":{"材料.txt":text}})[0])["checks"][0]["status"],"needs_evidence")

    def test_three_stage_demo_distinguishes_connectivity_from_course_requirements(self):
        self.assertEqual([item["status"] for item in checked(0)["checks"]], ["failed", "needs_evidence", "passed"])
        static = checked(1)
        self.assertEqual([item["status"] for item in static["checks"]], ["passed", "failed", "failed"])
        self.assertIn("STATIC", static["checks"][1]["detail"])
        self.assertEqual(checked(2)["status"], "passed")

    def test_each_observation_citation_matches_source_lines(self):
        sources, _ = source_bundle(payload(1))
        for row in check_goals(goals(), sources)["checks"]:
            for citation in row["evidence"]:
                self.assertEqual(sources[citation["source_id"]]["lines"][citation["line"] - 1], citation["quote"])

    def test_renamed_devices_and_addresses_use_the_same_checkers(self):
        original = json.dumps(payload(2), ensure_ascii=False).replace("R1", "Edge7").replace("10.3.3.3", "172.19.8.9")
        changed = json.loads(original)
        criteria = json.loads(json.dumps(goals(), ensure_ascii=False).replace("R1", "Edge7").replace("10.3.3.3", "172.19.8.9"))
        self.assertEqual(check_goals(criteria, source_bundle(changed)[0])["status"], "passed")

    def test_wrong_device_does_not_satisfy_route_goal(self):
        current = payload(2)
        current["configs"]["R1-运行.log"] = current["configs"]["R1-运行.log"].replace("<R1>", "<R2>")
        result = check_goals(goals(), source_bundle(current)[0])
        self.assertEqual(result["checks"][1]["status"], "needs_evidence")

    def test_longest_matching_route_controls_protocol(self):
        current = payload(2)
        current["configs"]["R1-运行.log"] = current["configs"]["R1-运行.log"].replace("10.3.3.3/32 OSPF", "10.3.3.0/24 OSPF").replace("<R1>ping", "10.3.3.3/32 Static 60 0 RD 10.12.0.2 GE0/0/0\n<R1>ping")
        criteria = goals()[1:2]; criteria[0]["target"] = "10.3.3.3"
        self.assertEqual(check_goals(criteria, source_bundle(current)[0])["checks"][0]["status"], "failed")

    def test_partial_config_cannot_prove_command_absent(self):
        current = payload(2); current["configs"]["R1.cfg"] = "sysname R1\nospf 1"
        result = check_goals(goals(), source_bundle(current)[0])
        self.assertEqual(result["checks"][2]["status"], "needs_evidence")

    def test_route_with_malformed_row_does_not_prove_completion(self):
        current = payload(2)
        current["configs"]["R1-运行.log"] = current["configs"]["R1-运行.log"].replace("10.3.3.3/32 OSPF", "10.999.3.3/32 OSPF")
        self.assertEqual(check_goals(goals(), source_bundle(current)[0])["checks"][1]["status"], "needs_evidence")

    def test_new_chat_route_supersedes_previous_static_route(self):
        current = payload(1)
        current["interaction"] = {"type": "verify", "message": payload(2)["configs"]["R1-运行.log"]}
        result = check_goals(goals(), source_bundle(current)[0])
        self.assertEqual(result["checks"][1]["status"], "passed")
        self.assertEqual(result["checks"][2]["status"], "failed")  # Uploaded config still contains static route.

    def test_old_chat_results_are_used_only_when_materials_are_unchanged(self):
        current = payload(0)
        history = [{"intent": "verify", "user": payload(2)["configs"]["R1-运行.log"]}]
        sources, _ = source_bundle(current, history)
        self.assertEqual(check_goals(goals(), sources, include_history=True)["checks"][1]["status"], "passed")
        self.assertEqual(check_goals(goals(), sources, include_history=False)["checks"][1]["status"], "needs_evidence")

    def test_existing_gateway_demo_keeps_device_attribution(self):
        materials = network_access_demo()["stages"][1]["materials"]
        current = {"configs": materials}
        goal = {**goals()[0], "device": "PC1", "target": "10.30.0.10"}
        self.assertEqual(check_goals([goal], source_bundle(current)[0])["status"], "passed")

    def test_new_goals_require_read_instruction_and_preserve_saved_criteria(self):
        current = payload(0); sources, _ = source_bundle(current); context = {}
        with self.assertRaises(ValueError):
            execute_tool("check_experiment_goals", {"goals": goals()}, current, sources, set(), diagnostic_context=context)
        seen = {("config:1", index) for index in range(1, 4)}
        execute_tool("check_experiment_goals", {"goals": goals()}, current, sources, seen, diagnostic_context=context)
        result, _ = execute_tool("check_experiment_goals", {}, current, sources, seen, diagnostic_context=context)
        self.assertEqual(len(result["criteria"]), 3)
        smaller = deepcopy(goals()[1]); smaller["expected"] = "Static"
        with self.assertRaises(ValueError):
            execute_tool("check_experiment_goals", {"goals": [smaller]}, current, sources, seen, diagnostic_context=context)

    def test_ping_success_cannot_override_failed_ospf_goal(self):
        current = payload(1); current["interaction"] = {"type": "verify", "message": "核对本轮结果"}
        sources, _ = source_bundle(current); context = {"criteria": goals()}; seen = {("config:1", 1)}
        pings, _ = execute_tool("check_submitted_pings", {}, current, sources, seen)
        args = {"summary": "通过", "hypotheses": [], "evidence_requests": [], "outcome": "observations_passed", "verification_targets": ["10.3.3.3"]}
        with self.assertRaisesRegex(ValueError, "实验目标还未全部满足"):
            execute_tool("submit_hypotheses", args, current, sources, seen, diagnostic_context=context, checked_pings=pings)

    def test_first_submission_can_finish_when_all_goals_already_pass(self):
        current = payload(2); sources, _ = source_bundle(current); seen = set()
        pings, _ = execute_tool("check_submitted_pings", {}, current, sources, seen)
        result, _ = execute_tool("submit_hypotheses", {"summary": "实验目标全部满足", "hypotheses": [], "evidence_requests": [],
            "outcome": "observations_passed", "verification_targets": ["10.3.3.3"]}, current, sources, seen, diagnostic_context={"criteria": goals()}, checked_pings=pings)
        self.assertEqual(result["outcome"], "observations_passed")

    def test_problem_field_can_supply_a_cited_experiment_requirement(self):
        current = payload(0); current["question"] = "R1必须通过OSPF学习10.3.3.3/32。"
        sources, _ = source_bundle(current); seen = set()
        execute_tool("read_source", {"source_id": "question:initial", "start_line": 1, "end_line": 1}, current, sources, seen)
        goal = goals()[1]; goal["instruction"] = {"source_id": "question:initial", "line": 1, "quote": current["question"]}
        result, _ = execute_tool("check_experiment_goals", {"goals": [goal]}, current, sources, seen)
        self.assertEqual(result["criteria"][0]["instruction"]["source_id"], "question:initial")

    def test_user_recovery_report_keeps_only_pending_course_requirements(self):
        current = payload(1); current["interaction"] = {"type": "ask", "message": "已经通了"}
        sources, _ = source_bundle(current); seen = {("message:current", 1)}
        result, _ = execute_tool("submit_hypotheses", {"summary": "恢复了", "hypotheses": [], "evidence_requests": [],
            "outcome": "user_reported_resolved", "resolution_evidence": [{"source_id": "message:current", "line": 1, "quote": "已经通了"}]},
            current, sources, seen, diagnostic_context={"criteria": goals()})
        self.assertEqual(result["outcome"], "goals_pending")
        self.assertEqual(len(result["evidence_requests"]), 2)
        self.assertNotIn("ping", " ".join(item["information"] for item in result["evidence_requests"]))

    def test_model_failure_keeps_saved_goals_and_current_tool_results(self):
        previous = {"goal_assessment": checked(0), "experiment_goal": "通过OSPF访问目标", "verification_targets": ["10.3.3.3"]}
        with patch.dict("os.environ", ENV):
            result = investigate(payload(1), previous, lambda messages: {"content": "plain answer"})
        self.assertEqual(result["llm"]["status"], "no_evidence")
        self.assertEqual(len(result["investigation"]["goal_assessment"]["criteria"]), 3)
        self.assertEqual(result["investigation"]["goal_assessment"]["checks"][1]["status"], "failed")

    def test_disabled_model_keeps_goals_for_the_next_attempt(self):
        current = payload(1); current["use_llm"] = False
        result = investigate(current, {"goal_assessment": checked(0)})
        self.assertEqual(len(result["investigation"]["goal_assessment"]["criteria"]), 3)
        self.assertEqual(result["investigation"]["goal_assessment"]["checks"][1]["status"], "failed")

    def test_selected_check_becomes_the_next_material_request(self):
        current = payload(0); sources, _ = source_bundle(current); seen = {("config:1", 1)}
        hypotheses = [{"id": name, "title": name, "state": "active", "evidence": [{"source_id": "config:1", "line": 1, "quote": sources["config:1"]["lines"][0]}]} for name in ("advertisement", "neighbor")]
        tests = [{"id": "peer", "device": "R2", "command": "display ospf peer", "purpose": "区分邻接与发布问题", "cost": 1,
                  "predictions": [{"hypothesis_id": "advertisement", "result": "full"}, {"hypothesis_id": "neighbor", "result": "not_full"}]}]
        context = {}
        execute_tool("plan_diagnostic_checks", {"hypotheses": hypotheses, "tests": tests}, current, sources, seen, diagnostic_context=context)
        result, terminal = execute_tool("submit_hypotheses", {"summary": "先查邻居", "hypotheses": [], "evidence_requests": [
            {"device": "R1", "information": "收集全部配置", "why": "普通材料请求"}]}, current, sources, seen, diagnostic_context=context)
        self.assertTrue(terminal)
        self.assertEqual(result["evidence_requests"][0]["information"], "display ospf peer")

    @unittest.skipUnless(available(), "Install workflow dependencies")
    def test_goals_survive_workflow_reopen_and_report_matches_final_checks(self):
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp, patch.dict("os.environ", ENV):
            store = Store(Path(temp) / "sessions.sqlite3"); database = Path(temp) / "workflow.sqlite3"
            first = store.save(payload(0), run_open_workflow(payload(0), database, scripted_completion(0)))
            self.assertEqual(first["llm"]["status"], "ok")
            middle = payload(1); middle.update(session_id=first["session_id"], interaction={"type": "verify", "message": "按实验要求核对"})
            bad = investigate(middle, first["investigation"], scripted_completion(1, falsely_pass=True))
            self.assertNotEqual(bad["status"], "verified_from_observations")
            second = store.save(middle, run_open_workflow(middle, database, scripted_completion(1)))
            self.assertEqual(len(second["investigation"]["goal_assessment"]["criteria"]), 3)
            report = generate_report(store.history(first["session_id"]))
            self.assertIn("STATIC", report); self.assertIn("未满足", report)
            last = payload(2); last.update(session_id=first["session_id"], interaction={"type": "verify", "message": "已按OSPF要求修正"})
            final = store.save(last, run_open_workflow(last, database, scripted_completion(2)))
            self.assertEqual(final["status"], "verified_from_observations")
            self.assertEqual(final["investigation"]["goal_assessment"]["status"], "passed")
            self.assertFalse(final["workflow"]["paused"])
            self.assertIn("## 实验目标逐项核对", generate_report(store.history(first["session_id"])))


class DiagnosticPlannerTests(unittest.TestCase):
    def candidates(self):
        return [{"id": value, "title": value, "state": "active", "evidence": []} for value in ("area", "advertisement", "return_path")]

    def probe(self, name, labels, cost=1):
        return {"id": name, "device": "R1", "command": name, "purpose": "区分原因", "cost": cost,
                "predictions": [{"hypothesis_id": key, "result": value} for key, value in zip(("area", "advertisement", "return_path"), labels)]}

    def test_selects_discriminating_check_over_repeated_ping(self):
        result = rank_checks(self.candidates(), [self.probe("ping", ["fail"] * 3), self.probe("display ospf peer", ["not_full", "full", "full"])])
        self.assertEqual(result["selected"]["command"], "display ospf peer")
        self.assertEqual(result["selected"]["pair_count"], 2)

    def test_cost_changes_selection_without_fake_probabilities(self):
        result = rank_checks(self.candidates(), [self.probe("many_commands", ["a", "b", "c"], 5), self.probe("one_command", ["a", "b", "b"], 1)])
        self.assertEqual(result["selected"]["id"], "one_command")

    def test_ruled_out_and_unknown_predictions_do_not_inflate_score(self):
        candidates = self.candidates(); candidates[0]["state"] = "ruled_out"
        result = rank_checks(candidates, [self.probe("peer", ["bad", "full", "full"]), self.probe("route", ["unknown", "present", "absent"])])
        self.assertEqual(result["selected"]["id"], "route")
        self.assertEqual(result["selected"]["pair_count"], 1)
        self.assertIsNone(rank_checks(candidates, [self.probe("unknown", ["unknown"] * 3)])["selected"])

    def test_plan_rejects_duplicate_predictions_and_uncited_exclusion(self):
        candidates = self.candidates(); candidates[0]["state"] = "ruled_out"
        with self.assertRaisesRegex(ValueError, "本轮材料依据"):
            validate_plan({"hypotheses": candidates, "tests": []}, lambda item: item)
        for item in candidates:
            item["evidence"] = [{}]
        test = self.probe("peer", ["full"] * 3)
        test["predictions"].append(test["predictions"][0])
        with self.assertRaises(ValueError):
            validate_plan({"hypotheses": candidates, "tests": [test]}, lambda item: item)


if __name__ == "__main__":
    unittest.main()
