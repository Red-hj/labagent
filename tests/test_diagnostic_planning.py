"""Observed evidence changes the next check and bounds configuration advice."""
from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from labagent import investigation
from labagent.diagnostic_planner import rank_checks
from labagent.engine import fingerprint
from labagent.experiment_goals import mark_stale_neighbor_materials
from labagent.investigation_context import observed_device_commands, prepare_investigation
from labagent.investigation_verification import verify_excerpt_claims, verify_minimal_ospf_wildcard_advice
from labagent.report import generate_report
from scripts.evaluate_diagnostic_workflow import bare_prompt, cases
from test_investigation import ENV


def hypotheses():
    return [{"id": name, "title": name, "state": "active", "evidence": []} for name in ("area", "advertisement", "return")]


def probe(command, outcomes, *, device="R1", cost=1):
    return {"id": command, "device": device, "command": command, "cost": cost, "purpose": "区分候选原因",
            "predictions": [{"hypothesis_id": item["id"], "result": outcome} for item, outcome in zip(hypotheses(), outcomes)]}


def submission(**updates):
    return {"summary": "继续核对当前实验", "hypotheses": [], "evidence_requests": [
        {"device": "R3", "information": "完整 OSPF 配置", "why": "确认当前接口状态"}], **updates}


def sources_and_seen(current):
    sources = investigation.source_bundle(current)[0]
    return sources, {(key, line) for key, source in sources.items() for line in range(1, len(source["lines"]) + 1)}


class DiagnosticCheckPlanningTests(unittest.TestCase):
    def test_unknown_predictions_do_not_claim_the_causes_are_ruled_out(self):
        result = rank_checks(hypotheses(), [probe("display ospf peer", ["unknown"] * 3)])
        self.assertIsNone(result["selected"])
        self.assertIn("预期结果不足", result["reason"])
        self.assertNotIn("均已排除", result["reason"])

    def test_report_shows_all_observed_reason_but_not_an_obsolete_recommendation(self):
        plan = rank_checks(hypotheses(), [probe("display ospf peer", ["a", "b", "c"])],
                           observed_commands={("R1", "display ospf peer")})
        result = {"mode":"open_network", "status":"needs_information", "llm":{"status":"ok"},
                  "investigation":{"experiment_goal":"核对实验", "summary":"已核对当前材料", "outcome":"investigating", "diagnostic_plan":plan}}
        history = {"session_id":"a"*32, "attempts":[{"created_at":"2026-11-28T00:00:00+00:00",
                   "input":{"question":"核对实验"}, "result":result}], "learning_record":{"config_revisions":1}}
        report = generate_report(history)
        self.assertIn("无需重复采集", report)
        self.assertIn("已提供输出：R1 · display ospf peer", report)
        plan["selected"] = {"device":"R2", "command":"display ospf interface"}
        for outcome, status in (("ready_for_retest","needs_information"), ("investigating","cancelled"),
                                ("observations_passed","verified_from_observations")):
            with self.subTest(outcome=outcome, status=status):
                result["status"] = status
                result["investigation"]["outcome"] = outcome
                self.assertNotIn("display ospf interface", generate_report(history))

    def test_observed_device_command_yields_to_a_new_discriminating_check(self):
        tests = [probe("DISPLAY   OSPF PEER", ["a", "b", "c"]), probe("display ospf interface", ["a", "b", "b"])]
        result = rank_checks(hypotheses(), tests, observed_commands={("r1", "display ospf peer")})
        self.assertEqual(result["selected"]["command"], "display ospf interface")
        self.assertTrue(result["tests"][-1]["already_observed"])
        result = rank_checks(hypotheses(), tests, observed_commands={("R2", "display ospf peer")})
        self.assertEqual(result["selected"]["command"], "DISPLAY   OSPF PEER")

    def test_all_observed_checks_do_not_select_a_repeat(self):
        result = rank_checks(hypotheses(), [probe("ping 10.3.3.3", ["fail"] * 3)],
                             observed_commands={("R1", "ping 10.3.3.3")})
        self.assertIsNone(result["selected"])
        self.assertIn("无需重复", result["reason"])

    def test_uniform_symptom_does_not_outrank_a_targeted_check_at_zero_pairs(self):
        result = rank_checks(hypotheses(), [probe("ping 10.3.3.3", ["fail"] * 3),
                             probe("display ospf interface", ["not_enabled", "unknown", "unknown"])])
        self.assertEqual(result["selected"]["command"], "display ospf interface")
        self.assertEqual(result["selected"]["pair_count"], 0)

    def test_current_output_metadata_and_ping_counts_are_observed_not_old_chat(self):
        current = {"configs": {"collection.log": "设备：R2\n命令：display ospf peer\n邻居状态 Full",
                               "R3.cfg": "sysname R3\ninterface GE0/0/0\n ip address 10.3.3.3 255.255.255.0"},
                   "question": "诊断"}
        sources = investigation.source_bundle(current, [{"intent": "ask", "user": "<R4>display ospf peer\nFull"}])[0]
        observed = observed_device_commands(sources)
        self.assertIn(("R2", "display ospf peer"), observed)
        self.assertNotIn(("R4", "display ospf peer"), observed)
        current = cases()[0]["vrp-partial"]
        observed = observed_device_commands(investigation.source_bundle(current)[0])
        self.assertIn(("R1", "ping 10.3.3.3"), observed)

    def test_original_frr_json_reuses_neighbor_table_for_either_command(self):
        case = Path(__file__).resolve().parents[1] / "data/team_cases/lab_02/map_materials/repaired"
        log = (case / "R1-run.log").read_text(encoding="utf-8")
        text, json_command = "show ip ospf neighbor", "show ip ospf neighbor json"
        current = {"configs": {"R1-run.log": log}, "question": "选择 R1 的下一项检查。"}
        sources, seen = sources_and_seen(current)
        proposals = [probe(command, ["a", "b", "c"]) for command in (text, json_command, "show ip ospf interface")]
        evidence = {"source_id": "config:1", "line": 1, "quote": sources["config:1"]["lines"][0]}
        candidates = [{**item, "evidence": [evidence]} for item in hypotheses()]
        plan, _ = investigation.execute_tool("plan_diagnostic_checks", {"hypotheses": candidates, "tests": proposals},
                                             current, sources, seen)
        self.assertEqual(plan["selected"]["command"], "show ip ospf interface")
        self.assertEqual({row["command"] for row in plan["tests"] if row["already_observed"]}, {text, json_command})
        self.assertNotIn(("R2", text), observed_device_commands(sources))
        route_text = ("<R2>show ip ospf neighbor\nNeighbor ID Pri State Up Time Dead Time Address Interface\n"
                      "1.1.1.1 1 Full/- 6.070s 3.908s 10.12.0.1 transit:10.12.0.2")
        current["configs"]["R2-run.log"] = route_text
        observed = observed_device_commands(sources_and_seen(current)[0])
        self.assertIn(("R2", json_command), observed)

    def test_frr_neighbor_reuse_requires_current_usable_evidence_for_the_goal(self):
        text, json_command = "show ip ospf neighbor", "show ip ospf neighbor json"
        question = "R1 的 OSPF 邻居 2.2.2.2 应达到 Full。"
        goal = {"id": "adjacency", "title": question, "kind": "ospf_neighbor", "device": "R1",
                "target": "2.2.2.2", "expected": "Full",
                "instruction": {"source_id": "question:initial", "line": 1, "quote": question}}
        old = '{"neighbors":{"2.2.2.2":[{"nbrState":"Full/-"}]}}'
        other = '{"neighbors":{"3.3.3.3":[{"nbrState":"Full/-"}]}}'
        conflict = '{"neighbors":{"2.2.2.2":[{"nbrState":"Full/-"},{"nbrState":"Init/-"}]}}'
        current = {"question": question, "configs": {"R1-old.log": f"<R1>{json_command}\n{old}"}}
        for capture, complete, observed, status in (
            ('{"neighbors":{}}', False, False, "needs_evidence"),
            ('{"neighbors":{}}', True, True, "failed"),
            (other, False, False, "needs_evidence"),
            (other, True, True, "failed"),
            (conflict, True, False, "needs_evidence"),
            (old[:-1], True, False, "needs_evidence"),
            (old, False, True, "passed"),
        ):
            with self.subTest(capture=capture, complete=complete):
                current["command_outputs"] = [{"device": "R1", "command": json_command, "text": capture,
                                               "complete": complete, "config_fingerprint": fingerprint(current)}]
                sources, seen = sources_and_seen(current)
                context = {}
                review, _ = investigation.execute_tool("check_experiment_goals", {"goals": [goal]}, current, sources,
                                                        seen, diagnostic_context=context)
                evidence = {"source_id": "output:1", "line": 1, "quote": capture}
                candidates = [{**item, "evidence": [evidence]} for item in hypotheses()]
                plan, _ = investigation.execute_tool("plan_diagnostic_checks", {"hypotheses": candidates, "tests": [
                    probe(text, ["a", "b", "c"]), probe(json_command, ["a", "b", "c"])]},
                    current, sources, seen, diagnostic_context=context)
                self.assertEqual(review["checks"][0]["status"], status)
                self.assertEqual([row["already_observed"] for row in plan["tests"]], [observed, observed])
                self.assertEqual(plan["selected"] is None, observed)

    def test_frr_neighbor_reuse_expires_when_the_carried_configuration_changes(self):
        command = "show ip ospf neighbor json"
        text = '<R1>show ip ospf neighbor json\n{"neighbors":{"2.2.2.2":[{"nbrState":"Full/-"}]}}'
        before = {"R1.cfg": "sysname R1\nospf 1\n area 1\nreturn", "R1-neighbor.log": text}
        current = {"configs": {**before, "R1.cfg": before["R1.cfg"].replace("area 1", "area 0")}}
        sources, _ = sources_and_seen(current)
        observed = observed_device_commands(sources, previous_materials=before)
        self.assertNotIn(("R1", command), observed)
        sources["config:2"]["collected"] = True
        observed = observed_device_commands(sources, previous_materials=before)
        self.assertIn(("R1", command), observed)
        self.assertIn(("R1", "show ip ospf neighbor"), observed)

    def test_neighbor_approval_waits_for_fresh_capture_across_rounds_and_report(self):
        log_path = Path(__file__).resolve().parents[1] / "data/team_cases/lab_02/map_materials/repaired/R1-run.log"
        command = "show ip ospf neighbor json"
        body = log_path.read_text(encoding="utf-8").split(f"<R1>{command}\n", 1)[1]
        material = "R1-neighbors.log"
        before = {"R1.cfg": "sysname R1\nospf 1\n area 0\nreturn", material: f"<R1>{command}\n{body}"}
        after = {**before, "R1.cfg": before["R1.cfg"].replace("area 0", "area 1")}
        question = "R1 的 OSPF 邻居 2.2.2.2 应达到 Full。"
        goal = {"id": "neighbor", "title": question, "kind": "ospf_neighbor", "device": "R1",
                "target": "2.2.2.2", "expected": "Full",
                "instruction": {"source_id": "question:initial", "line": 1, "quote": question}}
        original = {"configs": before, "question": question}
        sources, seen = sources_and_seen(original)
        first, _ = investigation.execute_tool("check_experiment_goals", {"goals": [goal]}, original, sources, seen)
        previous, previous_materials = {"goal_assessment": first}, None
        attempts = []
        for number, (configs, fresh, status) in enumerate((
                (before, False, "passed"), (after, False, "needs_evidence"),
                (after, False, "needs_evidence"), (after, True, "passed")), 1):
            payload = {"configs": dict(configs), "question": question, "use_llm": False}
            if number == 3:
                payload["interaction"] = {"type": "ask", "message": "还需要复测邻居吗？"}
            if fresh:
                payload["command_outputs"] = [{"device": "R1", "command": command, "text": body, "complete": True,
                                               "config_fingerprint": fingerprint(payload)}]
            result = investigation.investigate(payload, previous, previous_materials=previous_materials)
            analysis = result["investigation"]
            check, = analysis["goal_assessment"]["checks"]
            self.assertEqual(check["status"], status)
            if number > 1:
                self.assertEqual(analysis["experiment_state"]["stale_neighbor_materials"], [material])
            if status == "needs_evidence":
                self.assertEqual(check["evidence"], [])
            if fresh:
                self.assertEqual([ref["source_id"] for ref in check["evidence"]], ["output:1", "output:1"])
                for ref in check["evidence"]:
                    self.assertEqual(ref["quote"], body.splitlines()[ref["line"] - 1])

            sources, _ = sources_and_seen(payload)
            stale = set(previous.get("experiment_state", {}).get("stale_neighbor_materials", []))
            mark_stale_neighbor_materials(sources, previous_materials, stale)
            observed = observed_device_commands(sources, previous_materials=previous_materials, criteria=[goal])
            self.assertEqual(("R1", command) in observed, status == "passed")
            attempts.append({"input": payload, "result": result, "created_at": f"2026-11-28T08:00:0{number}+00:00"})
            previous, previous_materials = analysis, configs

        report = generate_report({"session_id": "neighbor-retest", "attempts": attempts,
                                  "learning_record": {"config_revisions": 1}})
        for row in ("| 1 | 1/1 | 0 | 0 |", "| 2 | 0/1 | 0 | 1 |", "| 3 | 0/1 | 0 | 1 |", "| 4 | 1/1 | 0 | 0 |"):
            self.assertIn(row, report)
        self.assertIn('"nbrState":"Full', report)

    def test_changed_config_allows_retest_of_old_outputs_but_keeps_live_capture(self):
        before = {"R1.cfg":"sysname R1\nospf 1\n area 1\nreturn",
                  "R1-peer.log":"<R1>display ospf peer\nNo neighbors",
                  "R1-ping.log":"<R1>ping 10.3.3.3\n5 packets transmitted, 0 received",
                  "R2-peer.log":"<R2>display ospf peer\nNo neighbors"}
        current = {"configs":{**before, "R1.cfg":before["R1.cfg"].replace("area 1", "area 0"),
                              "R2-peer.log":"<R2>display ospf peer\nFull"}, "question":"核对修改后的新状态"}
        sources, _ = sources_and_seen(current)
        observed = observed_device_commands(sources, previous_materials=before)
        self.assertNotIn(("R1", "display ospf peer"), observed)
        self.assertNotIn(("R1", "ping 10.3.3.3"), observed)
        self.assertIn(("R2", "display ospf peer"), observed)
        next(source for source in sources.values() if source["device"] == "R1-peer.log")["collected"] = True
        self.assertIn(("R1", "display ospf peer"), observed_device_commands(sources, previous_materials=before))

    def test_unchanged_config_still_reuses_uploaded_outputs(self):
        configs = {"R1.cfg":"sysname R1\nospf 1\n area 0\nreturn",
                   "R1.log":"<R1>display ospf peer\nFull"}
        sources, _ = sources_and_seen({"configs":configs})
        self.assertIn(("R1", "display ospf peer"), observed_device_commands(sources, previous_materials=configs))

    def test_planning_can_select_a_previously_observed_command_after_configuration_change(self):
        before = {"R1.cfg":"sysname R1\nospf 1\n area 1\nreturn", "R1.log":"<R1>display ospf peer\nNo neighbors"}
        current = {"configs":{**before, "R1.cfg":before["R1.cfg"].replace("area 1", "area 0")}, "question":"复测"}
        sources, seen = sources_and_seen(current)
        candidates = hypotheses()
        for item in candidates:
            item["evidence"] = [{"source_id":"config:2", "line":2, "quote":"No neighbors"}]
        plan, _ = investigation.execute_tool("plan_diagnostic_checks", {"hypotheses":candidates,
            "tests":[probe("display ospf peer", ["a", "b", "c"])]}, current, sources, seen, previous_materials=before)
        self.assertEqual(plan["selected"]["command"], "display ospf peer")
        self.assertFalse(plan["selected"]["already_observed"])

    def test_planning_tool_uses_observations_and_keeps_the_plan(self):
        current = {"configs": {"R1.log": "<R1>display ospf peer\nFull"}, "question": "下一项检查"}
        sources, seen = sources_and_seen(current)
        candidates = hypotheses()
        for item in candidates:
            item["evidence"] = [{"source_id": "config:1", "line": 2, "quote": "Full"}]
        context = {}
        plan, _ = investigation.execute_tool("plan_diagnostic_checks", {"hypotheses": candidates, "tests": [
            probe("display ospf peer", ["a", "b", "c"]), probe("display ospf interface", ["a", "b", "b"])]},
            current, sources, seen, diagnostic_context=context)
        result, _ = investigation.execute_tool("submit_hypotheses", submission(), current, sources, seen, diagnostic_context=context)
        self.assertEqual(result["diagnostic_plan"], plan)
        self.assertEqual(result["evidence_requests"][0]["information"], "display ospf interface")

    def test_first_repair_submission_needs_no_artificial_evidence_request(self):
        result = investigation.validate_submission(submission(outcome="ready_for_retest", steps=["修改已定位的接口区域，再复测。"],
                                                              evidence_requests=[]), {}, set())
        self.assertEqual(result["evidence_requests"], [])
        for steps in ([], [""]):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                investigation.validate_submission(submission(outcome="ready_for_retest", steps=steps, evidence_requests=[]), {}, set())

    def test_ready_repair_does_not_restore_an_old_pending_request(self):
        current = cases()[0]["vrp-supplemented"]
        current["interaction"] = {"type": "repair", "message": "请给最小修改"}
        prior_request = {"device": "R3", "information": "完整配置", "why": "此前尚未提供"}
        previous = {"outcome": "investigating", "pending_requests": [prior_request]}
        def complete(messages):
            if messages[-1]["role"] != "tool":
                source = next(source for source in investigation.source_bundle(current)[0].values() if source["device"] == "R3.cfg")
                name, args = "read_source", {"source_id": source["source_id"], "start_line": 1, "end_line": len(source["lines"])}
            else:
                name, args = "submit_hypotheses", submission(outcome="ready_for_retest", evidence_requests=[],
                    steps=["在 R3 为目标接口启用 OSPF，然后复测。"])
            return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args)}}]}
        with patch.dict(os.environ, {**ENV, "LABAGENT_MCP_CONFIG": ""}):
            result = investigation.investigate(current, previous, complete)
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertEqual(result["investigation"]["pending_requests"], [])
        self.assertEqual(result["runtime_missing"], [])

    def test_checked_observed_route_removes_parenthesized_request_but_keeps_retest(self):
        current = cases()[0]["vrp-healthy"]
        sources, seen = sources_and_seen(current)
        context = {}
        route, _ = investigation.execute_tool("check_route_selection", {"device": "R1", "target": "10.3.3.3"},
            current, sources, seen, diagnostic_context=context)
        self.assertEqual(route["status"], "observed")
        requests = [
            {"device": "R1", "information": "完整当前 IPv4 路由表（display ip routing-table、show ip route 或 ip -4 route show table main）", "why": "核对当前选路"},
            {"device": "R1", "information": "当前路由表", "why": "修改后复测"},
            {"device": "R1", "information": "当前路由表和邻居状态", "why": "核对两项"},
            {"device": "R3", "information": "当前路由表", "why": "另一设备"},
        ]
        result, _ = investigation.execute_tool("submit_hypotheses", submission(evidence_requests=requests),
            current, sources, seen, diagnostic_context=context)
        self.assertEqual(result["evidence_requests"], requests[1:])

    def test_goal_disabled_context_removes_tools_and_their_prompt_instructions(self):
        current = cases()[0]["vrp-partial"]
        sources = investigation.source_bundle(current)[0]
        _, messages, tools = prepare_investigation(current, sources, goal_checks=False, registry=investigation.tool_registry)
        names = {tool["function"]["name"] for tool in tools}
        for name in ("check_experiment_goals", "plan_diagnostic_checks"):
            self.assertNotIn(name, names)
            self.assertNotIn(name, messages[0]["content"])
        self.assertIn("read_source", names)
        self.assertEqual(bare_prompt(messages), messages)
        history = [{"role": "system", "content": "保留规则\n调用check_experiment_goals\n继续读取材料"}, {"role": "user", "content": "问题"}]
        filtered = bare_prompt(history)
        self.assertEqual(filtered[0]["content"], "保留规则\n继续读取材料")
        self.assertEqual(filtered[1:], history[1:])

    def test_existing_citation_exports_preserve_new_teammate_line_reference(self):
        sources = investigation.source_bundle({"configs": {"实验说明.txt": "目标一；目标二"}})[0]
        ref = investigation.checked_citation({"source_id": "config:1", "line": 1}, sources,
                                            {("config:1", 1)}, allow_line_reference=True)
        self.assertEqual(ref["quote"], "目标一；目标二")
        self.assertEqual(investigation.source_bundle.__module__, "labagent.investigation_context")
        self.assertEqual(investigation.validate_submission.__module__, "labagent.investigation_verification")


class ConfigurationAdviceVerificationTests(unittest.TestCase):
    def test_partial_physical_interface_with_failed_ping_requests_its_device(self):
        current = deepcopy(cases()[0]["vrp-partial"])
        current["configs"]["R3.cfg"] = current["configs"]["R3.cfg"].replace("LoopBack0", "GigabitEthernet0/0/1")
        sources, seen = sources_and_seen(current)
        result, _ = investigation.execute_tool("submit_hypotheses", submission(summary="R3 漏配了 OSPF", outcome="ready_for_retest",
                                              steps=["在 R3 添加 network"], evidence_requests=[]), current, sources, seen)
        self.assertEqual(result["steps"], [])
        self.assertEqual(result["evidence_requests"][0]["device"], "R3")
        self.assertIn("GigabitEthernet0/0/1", result["evidence_requests"][0]["information"])

    def test_no_or_multiple_pings_cannot_prove_absence_in_an_excerpt(self):
        for output in ("", "<R1>ping 10.3.3.3\n5 packet(s) transmitted\n0 packet(s) received\n<R1>ping 10.4.4.4\n5 packet(s) transmitted\n0 packet(s) received"):
            with self.subTest(output=output):
                current = {"configs": {"R3-摘录.cfg": "sysname R3\ninterface GigabitEthernet0/0/1\n ip address 10.3.3.3 255.255.255.0\nreturn",
                                       "R1.log": output or "没有新的运行输出"}, "question": "排查 OSPF"}
                sources, seen = sources_and_seen(current)
                with self.assertRaisesRegex(ValueError, "不能断言"):
                    investigation.execute_tool("submit_hypotheses", submission(summary="R3 的 GE0/0/1 未宣告 OSPF"),
                                               current, sources, seen)
                verify_excerpt_claims(submission(summary="R3 的 GE0/0/1 是否宣告尚无法判断"), sources, seen)

    def test_complete_unread_config_needs_a_read_then_accepts_the_missing_claim(self):
        sources, seen = sources_and_seen({"configs": {"R3.cfg": "sysname R3\ninterface LoopBack0\n ip address 10.3.3.3 255.255.255.255\nreturn"}})
        wrong = submission(summary="R3 的 LoopBack0 未宣告 OSPF")
        with self.assertRaises(ValueError):
            verify_excerpt_claims(wrong, sources, {("config:1", 1)})
        verify_excerpt_claims(wrong, sources, seen)

    def test_uncertain_hypothesis_does_not_authorize_an_unproven_configuration_step(self):
        sources, seen = sources_and_seen({"configs": {"R3-摘录.cfg": "sysname R3\ninterface LoopBack0\n ip address 10.3.3.3 255.255.255.255"}})
        result = submission(summary="R3 是否宣告尚不能确定", steps=["在 R3 的 Area 0 添加 network 10.3.3.3 0.0.0.0"])
        with self.assertRaisesRegex(ValueError, "不能断言"):
            verify_excerpt_claims(result, sources, seen)

    def test_other_device_or_uncertain_hypothesis_is_not_a_confirmed_absence(self):
        current = {"configs": {"R3-摘录.cfg": "sysname R3\ninterface GE0/0/1\n ip address 10.3.3.3 255.255.255.0"}}
        sources, seen = sources_and_seen(current)
        verify_excerpt_claims(submission(summary="R30 未宣告该网段"), sources, seen)
        verify_excerpt_claims(submission(summary="R3 可能未宣告，需要先查看完整配置"), sources, seen)

    def test_operational_evidence_must_match_the_claimed_interface(self):
        current = {"configs": {"R3-摘录.cfg": "sysname R3\ninterface GE0/0/1\n ip address 10.3.3.3 255.255.255.0",
                               "R3.log": "<R3>display ospf interface GE0/0/10\n Interface: GE0/0/10\n Area: 0.0.0.0"}}
        sources, seen = sources_and_seen(current)
        claim = submission(summary="R3 的 GE0/0/1 未加入 OSPF")
        with self.assertRaises(ValueError):
            verify_excerpt_claims(claim, sources, seen)
        current["configs"]["R3.log"] = current["configs"]["R3.log"].replace("GE0/0/10", "GigabitEthernet0/0/1")
        sources, seen = sources_and_seen(current)
        verify_excerpt_claims(claim, sources, seen)

    def test_exact_interface_network_must_not_be_widened_but_unmatched_address_can_change(self):
        config = "sysname R1\ninterface GE0/0/0\n ip address 10.12.0.2 255.255.255.0\n#\nospf 1\n area 0\n  network 10.12.0.2 0.0.0.0\nreturn"
        sources, _ = sources_and_seen({"configs": {"R1.cfg": config}})
        for text in ("R1 改为 network 10.12.0.2 0.0.0.255", "R1 的 10.12.0.2 反掩码由0.0.0.0改为0.0.0.255"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "精确匹配"):
                verify_minimal_ospf_wildcard_advice(submission(steps=[text]), sources)
        verify_minimal_ospf_wildcard_advice(submission(steps=["R1 无需修改 network 10.12.0.2 0.0.0.255"]), sources)
        verify_minimal_ospf_wildcard_advice(submission(steps=["R2 改为 network 10.12.0.2 0.0.0.255"]), sources)
        verify_minimal_ospf_wildcard_advice(submission(steps=["R1 添加 network 10.3.3.3 0.0.0.0"]), sources)

    def test_wildcard_check_is_wired_into_submission(self):
        current = {"configs": {"R1.cfg": "sysname R1\ninterface GE0/0/0\n ip address 10.12.0.2 255.255.255.0\n#\nospf 1\n area 0\n  network 10.12.0.2 0.0.0.0\nreturn"}, "question": "最小修改"}
        sources, seen = sources_and_seen(current)
        with self.assertRaisesRegex(ValueError, "精确匹配"):
            investigation.execute_tool("submit_hypotheses", submission(outcome="ready_for_retest", evidence_requests=[],
                steps=["在 R1 修改为 network 10.12.0.2 0.0.0.255"]), current, sources, seen)


if __name__ == "__main__":
    unittest.main()
