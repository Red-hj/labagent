"""Observed evidence changes the next check and bounds configuration advice."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from labagent import investigation
from labagent.diagnostic_planner import rank_checks
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
