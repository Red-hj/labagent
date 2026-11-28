"""Replay submitted FRR captures; test criteria are explicit self-authored requirements."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.engine import fingerprint
from labagent.experiment_goals import check_goals
from labagent.investigation import source_bundle, execute_tool
from labagent.report import generate_report
from labagent.runtime_parser import parse_linux_routes
from labagent.store import Store
from labagent.workflow import available, run_open_workflow
from test_experiment_goals import call
from test_investigation import ENV

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "data/team_cases/lab_02/map_materials"
INSTRUCTIONS = "PC1必须Ping通192.168.20.10。\nPC2必须Ping通192.168.10.10。\nR1到192.168.20.0/24必须使用OSPF路由。\nR2到192.168.10.0/24必须使用OSPF路由。"


def material_payload(phase):
    return {"diagnostic_mode": "open_network", "question": "按本次验证要求核对连通性和OSPF路由。", "use_llm": True,
            "configs": {**{path.name: path.read_text(encoding="utf-8") for path in (CAPTURES / phase).iterdir()},
                        "验证要求.txt": INSTRUCTIONS}, "evidence_kind": "demo"}


def criteria(payload):
    sources, _ = source_bundle(payload)
    source_id = next(key for key, source in sources.items() if source["device"] == "验证要求.txt")
    return [{"id": f"G{i}", "title": INSTRUCTIONS.splitlines()[i - 1], "device": device, "kind": kind,
             "target": target, "expected": "OSPF" if kind == "route_protocol" else "",
             "instruction": {"source_id": source_id, "line": i, "quote": INSTRUCTIONS.splitlines()[i - 1]}}
            for i, (device, kind, target) in enumerate((("PC1", "reachability", "192.168.20.10"),
                ("PC2", "reachability", "192.168.10.10"), ("R1", "route_protocol", "192.168.20.0/24"),
                ("R2", "route_protocol", "192.168.10.0/24")), 1)]


def checked_route(materials, device="R1", target="192.168.20.10"):
    payload = {"configs": materials}
    sources, _ = source_bundle(payload)
    return execute_tool("check_route_selection", {"device": device, "target": target}, payload, sources, set())[0]


class LinuxRouteTests(unittest.TestCase):
    def test_promptless_route_and_ping_materials_from_mcp_preserve_device_and_goals(self):
        for phase, expected in (("fault", ["failed", "failed", "needs_evidence", "needs_evidence"]),
                                ("repaired", ["passed"] * 4)):
            with self.subTest(phase=phase):
                request = material_payload(phase)
                materials = {}
                for device, command in (("R1", "ip route show table main"), ("R2", "ip route show table main"),
                                        ("PC1", "ping"), ("PC2", "ping")):
                    raw = request["configs"][f"{device}-run.log"]
                    if command == "ping":
                        text = "PING " + raw.split("\nPING ", 1)[1]
                        command += " " + text.split()[1]
                    else:
                        text = raw.split(f"<{device}>{command}\n", 1)[1].split(f"\n<{device}>", 1)[0]
                    materials[f"MCP-{device}-sample.log"] = f"设备：{device}\n命令：{command}\n{text}"
                request["configs"] = {**materials, "验证要求.txt": INSTRUCTIONS}
                sources, _ = source_bundle(request)
                result = check_goals(criteria(request), sources)
                self.assertEqual([g["status"] for g in result["checks"]], expected)
                for check in result["checks"]:
                    for ref in check["evidence"]:
                        self.assertEqual(ref["quote"], materials[ref["device"]].splitlines()[ref["line"] - 1])

    def test_promptless_mcp_vrp_complete_table_preserves_command_scope(self):
        body = ("Routing Table : Public\nDestinations : 1 Routes : 1\nDestination/Mask Proto Pre Cost Flags NextHop Interface\n"
                "10.12.0.0/24 Direct 0 0 D 10.12.0.1 GE0/0/0")
        for suffix, expected in (("", "no_match"), (" protocol ospf", "needs_evidence")):
            text = f"设备：R1\n命令：display ip routing-table{suffix}\n{body}"
            row = checked_route({"MCP-R1.log": text}, target="10.3.3.3")
            self.assertEqual(row["status"], expected)
            if expected == "no_match":
                self.assertIn(4, [ref["line"] for ref in row["evidence"]])

    def test_mcp_metadata_does_not_override_an_explicit_device_prompt(self):
        text = ("设备：R1\n命令：ip route show table main\n<R2>ip route show table main\n"
                "192.168.20.0/24 via 10.12.0.2 dev transit proto ospf metric 20")
        self.assertEqual(checked_route({"MCP-R1.log": text})["status"], "needs_evidence")
        result = checked_route({"MCP-R1.log": text}, device="R2")
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["evidence"][0]["line"], 4)

    def test_repaired_capture_routes_are_observed_with_original_line_citations(self):
        payload = material_payload("repaired")
        sources, _ = source_bundle(payload)
        for device, target, hop in (("R1", "192.168.20.10", "10.12.0.2"), ("R2", "192.168.10.10", "10.12.0.1")):
            result, _ = execute_tool("check_route_selection", {"device": device, "target": target}, payload, sources, set())
            self.assertEqual(result["status"], "observed")
            self.assertEqual([(r["protocol"], r["next_hop"], r["interface"]) for r in result["routes"]], [("OSPF", hop, "transit")])
            ref = result["evidence"][0]
            self.assertEqual(ref["quote"], sources[ref["source_id"]]["lines"][ref["line"] - 1])
            self.assertIn("proto ospf", ref["quote"])

    def test_missing_protocol_is_not_inferred_to_be_static(self):
        text = "<R1>ip route\ndefault via 10.12.0.2 dev transit"
        self.assertEqual(parse_linux_routes(text)[0]["protocol"], "Unknown")
        payload = material_payload("repaired")
        payload["configs"]["R1-run.log"] = text
        checks = check_goals(criteria(payload), source_bundle(payload)[0])["checks"]
        self.assertEqual(checks[2]["status"], "needs_evidence")

    def test_longest_prefix_then_metric_selects_route_and_retains_equal_cost_paths(self):
        text = ("R1# ip -4 route show table main\n"
                "default via 10.12.0.9 dev transit proto static metric 1\n"
                "192.168.20.0/24 via 10.12.0.8 dev transit proto static metric 200\n"
                "192.168.20.0/24 nhid 11 via 10.12.0.2 dev transit proto ospf metric 20\n"
                "192.168.20.0/24 via inet 10.12.0.3 dev transit proto ospf metric 20\n")
        result = checked_route({"R1.log": text})
        self.assertEqual([row["next_hop"] for row in result["routes"]], ["10.12.0.2", "10.12.0.3"])

    def test_linkdown_is_local_to_the_matching_route(self):
        down = "10.12.0.0/24 dev transit proto kernel scope link src 10.12.0.1 linkdown"
        selected = "192.168.20.0/24 nhid 17 via 10.12.0.2 dev other proto ospf metric 20"
        current = {"R1.log": f"R1# ip -4 route show table main\n{down}\n{selected}"}
        observed = checked_route(current)
        self.assertEqual(observed["status"], "observed")
        self.assertEqual(observed["evidence"][0]["quote"], selected)
        unknown = checked_route(current, target="10.12.0.2")
        self.assertEqual(unknown["status"], "needs_evidence")
        self.assertIn("linkdown", unknown["detail"])
        self.assertEqual(unknown["evidence"][0]["quote"], down)

    def test_verified_complete_linux_command_distinguishes_absence_from_excerpt(self):
        rows = ("10.12.0.0/24 dev transit proto kernel scope link src 10.12.0.1 linkdown\n"
                "192.168.10.0/24 dev lan proto kernel scope link src 192.168.10.1")
        for variants, expected in (
            ({}, "no_match"),
            ({"complete": False}, "needs_evidence"),
            ({"command": "ip -4 route show table 100"}, "needs_evidence"),
            ({"text": rows + "\n..."}, "needs_evidence"),
            ({"text": rows + "\nR2# ip route\n192.168.30.0/24 dev lan proto kernel"}, "needs_evidence"),
            ({"config_fingerprint": "old"}, "needs_evidence"),
        ):
            with self.subTest(variants=variants):
                current = {"configs": {"requirements.txt": "R1 需要 OSPF 到 192.168.20.0/24。"}}
                current["command_outputs"] = [{"device": "R1", "command": "ip -4 route show table main",
                    "text": rows, "complete": True, "config_fingerprint": fingerprint(current), **variants}]
                sources, _ = source_bundle(current)
                result, _ = execute_tool("check_route_selection", {"device": "R1", "target": "192.168.20.0/24"},
                                         current, sources, set())
                self.assertEqual(result["status"], expected)
                if expected == "no_match":
                    self.assertIsNone(result["request"])
                    self.assertEqual([item["quote"] for item in result["evidence"]], rows.splitlines())
                    goal = {**criteria(material_payload("fault"))[2],
                            "instruction": {"source_id": "config:1", "line": 1, "quote": current["configs"]["requirements.txt"]}}
                    self.assertEqual(check_goals([goal], sources)["checks"][0]["status"], "failed")

    def test_non_main_filtered_and_configuration_commands_are_not_observations(self):
        for command in ("ip route show table 100", "ip route show proto ospf", "ip route get 192.168.20.10",
                        "ip route add 192.168.20.0/24 via 10.12.0.2", "ip -6 route"):
            with self.subTest(command=command):
                text = "R1# " + command + "\n192.168.20.0/24 via 10.12.0.2 dev transit proto ospf"
                self.assertEqual(checked_route({"R1.log": text})["status"], "needs_evidence")

    def test_new_malformed_or_unsupported_table_does_not_reuse_old_good_output(self):
        old = "<R1>ip route\n192.168.20.0/24 via 10.12.0.2 dev transit proto ospf\n"
        for invalid in ("192.168.20.0/24 malformed row", "blackhole 192.168.20.0/24 proto static",
                        "192.168.20.0/24 nhid 99 proto ospf", "RTNETLINK answers: Permission denied"):
            with self.subTest(invalid=invalid):
                result = checked_route({"R1.log": old + "<R1>ip route\n" + invalid})
                self.assertEqual(result["status"], "needs_evidence")
                self.assertEqual(result["routes"], [])

    def test_explicit_device_prompts_prevent_cross_device_route_attribution(self):
        text = ("R1# ip route\n10.12.0.0/24 dev transit proto kernel\n"
                "R2# ip route\n192.168.20.0/24 via 10.12.0.2 dev transit proto ospf\n"
                "R2# show running-config\nhostname R2")
        self.assertEqual(checked_route({"R1-mixed.log": text})["status"], "needs_evidence")
        self.assertEqual(checked_route({"R1-mixed.log": text}, "R2")["status"], "observed")

    def test_fault_and_repair_captures_and_static_substitution_keep_protocol_goal(self):
        broken, repaired = material_payload("fault"), material_payload("repaired")
        self.assertNotEqual(check_goals(criteria(broken), source_bundle(broken)[0])["status"], "passed")
        self.assertEqual(check_goals(criteria(repaired), source_bundle(repaired)[0])["status"], "passed")
        static = deepcopy(repaired)
        static["configs"]["R1-run.log"] = static["configs"]["R1-run.log"].replace("proto ospf", "proto static")
        assessment = check_goals(criteria(static), source_bundle(static)[0])
        self.assertEqual([row["status"] for row in assessment["checks"]], ["passed", "passed", "failed", "passed"])

    @unittest.skipUnless(available(), "Install workflow dependencies")
    def test_same_name_upload_replaces_older_observations_and_survives_session_restore(self):
        repaired = (CAPTURES / "repaired/R1-run.log").read_text(encoding="utf-8")
        fault = (CAPTURES / "fault/R1-run.log").read_text(encoding="utf-8")
        ping_passed = (CAPTURES / "repaired/PC1-run.log").read_text(encoding="utf-8")
        ping_failed = (CAPTURES / "fault/PC1-run.log").read_text(encoding="utf-8")
        materials = {"R1-retest.log": repaired, "PC1-retest.log": ping_passed, "验证要求.txt": INSTRUCTIONS,
                     "R1-previous.log": repaired, "PC1-previous.log": ping_passed}
        request = {"diagnostic_mode": "open_network", "question": "核对 R1 路由来源和 PC1 的 Ping。", "use_llm": True,
                   "configs": materials, "learning": {"level": 1}}

        def complete(messages):
            manifest = json.loads(messages[1]["content"])
            instruction = next(item for item in manifest["materials"] if item["device"] == "验证要求.txt")
            tools = [item["tool_call_id"] for item in messages if item["role"] == "tool"]
            if not tools:
                return call("read_source", {"source_id": instruction["source_id"], "start_line": 1, "end_line": 3})
            if len(tools) == 1:
                goals = criteria({"configs": {"验证要求.txt": INSTRUCTIONS}})
                selected = [goals[0], goals[2]]
                for goal in selected:
                    goal["instruction"]["source_id"] = instruction["source_id"]
                return call("check_experiment_goals", {} if manifest["saved_goals"] else {"goals": selected})
            assessment = json.loads(messages[-1]["content"])
            passed = assessment["status"] == "passed"
            return call("submit_learning_step", {"stage": "completed" if passed else "observe",
                "current_step": "核对本轮复测结果", "explanation": "按实验要求查看两项观测。",
                "action": "查看 R1 路由表和 PC1 的 Ping。", "expected_observation": "核对路由协议与丢包情况。",
                "question": "本轮两项结果是否都满足实验要求？", "evidence": []})

        base = ROOT / "runtime/tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory, patch.dict("os.environ", ENV):
            store = Store(Path(directory) / "sessions.sqlite3")
            database = Path(directory) / "workflow.sqlite3"
            first_payload = deepcopy(request)
            first = store.save(first_payload, run_open_workflow(first_payload, database, complete))
            self.assertEqual(first["status"], "verified_from_observations")
            refreshed = {"R1-retest.log": fault, "PC1-retest.log": ping_failed}
            second_payload = {**request, "session_id": first["session_id"], "configs": {**materials, **refreshed},
                              "interaction": {"type": "verify", "message": "已更新 R1 的路由表和 PC1 的 Ping。"}}
            second = store.save(second_payload, run_open_workflow(second_payload, database, complete))
            attempt = store.history(first["session_id"])["attempts"][-1]
            # Use the saved order when the graph checkpoint has expired and the browser resends all files.
            resumed = {**second_payload, "configs": {"验证要求.txt": INSTRUCTIONS, **materials, **refreshed}}
            restored = run_open_workflow(resumed, Path(directory) / "restored.sqlite3", complete, previous_attempt=attempt)
            for result in (second, restored):
                self.assertEqual(result["llm"]["status"], "ok")
                self.assertEqual(result["status"], "needs_information")
                checks = result["investigation"]["goal_assessment"]["checks"]
                self.assertEqual([row["status"] for row in checks], ["failed", "needs_evidence"])
                sources = {source["source_id"]: source["device"] for source in result["source_manifest"]}
                for row, name in zip(checks, ("PC1-retest.log", "R1-retest.log")):
                    self.assertTrue(row["evidence"])
                    for ref in row["evidence"]:
                        self.assertEqual(ref["device"], name)
                        self.assertEqual(ref["quote"], refreshed[name].splitlines()[ref["line"] - 1])
                    self.assertEqual(sources[row["instruction"]["source_id"]], "验证要求.txt")

    @unittest.skipUnless(available(), "Install workflow dependencies")
    def test_fault_to_repaired_workflow_finishes_with_all_four_goals_and_matching_report(self):
        def scripted(payload, repaired):
            goals = criteria(payload)
            def complete(messages):
                tools = [message for message in messages if message["role"] == "tool"]
                if not tools:
                    name, args = "read_source", {"source_id": goals[0]["instruction"]["source_id"], "start_line": 1, "end_line": 4}
                elif len(tools) == 1:
                    name, args = "check_experiment_goals", {} if repaired else {"goals": goals}
                elif len(tools) == 2:
                    name, args = "check_submitted_pings", {}
                else:
                    name = "submit_hypotheses"
                    args = {"summary": "四项目标均通过" if repaired else "故障材料中的实验目标仍未完成", "hypotheses": [],
                            "evidence_requests": [] if repaired else [{"device": "R1", "information": "修复后路由表和双向Ping", "why": "核对实验目标"}],
                            "outcome": "observations_passed" if repaired else "investigating", "verification_targets": ["192.168.20.10", "192.168.10.10"]}
                return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}
            return complete
        base = ROOT / "runtime/tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as directory, patch.dict("os.environ", ENV):
            store = Store(Path(directory) / "sessions.sqlite3")
            database = Path(directory) / "workflow.sqlite3"
            first_payload = material_payload("fault")
            first = store.save(first_payload, run_open_workflow(first_payload, database, scripted(first_payload, False)))
            self.assertNotEqual(first["status"], "verified_from_observations")
            final_payload = material_payload("repaired")
            final_payload.update(session_id=first["session_id"], interaction={"type": "verify", "message": "已同名更新修复后的原始输出，请核对全部目标。"})
            final = store.save(final_payload, run_open_workflow(final_payload, database, scripted(final_payload, True)))
            self.assertEqual(final["status"], "verified_from_observations")
            self.assertEqual(final["investigation"]["outcome"], "observations_passed")
            self.assertEqual(final["investigation"]["goal_assessment"]["status"], "passed")
            report = generate_report(store.history(first["session_id"]))
            self.assertIn("OSPF", report)
            self.assertIn("192.168.20.0/24", report)
            goal_section = report.split("## 实验目标逐项核对", 1)[1].split("\n## ", 1)[0]
            initial_checks, final_checks = goal_section.split("### 第2轮", 1)
            self.assertIn("| 1 | 0/4 | 2 | 2 |", initial_checks)
            self.assertIn("| 2 | 4/4 | 0 | 0 |", initial_checks)
            self.assertIn("| R1到192.168.20.0/24必须使用OSPF路由。 | 待补充输出 |", initial_checks)
            self.assertIn("| R1到192.168.20.0/24必须使用OSPF路由。 | 已满足 |", final_checks)
            self.assertNotIn("| 待补充输出 |", final_checks)


if __name__ == "__main__":
    unittest.main()
