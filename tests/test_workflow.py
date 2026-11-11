from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.workflow import available, run_workflow
from labagent.runtime_parser import normalize_outputs, parse_routes, parse_neighbors
from labagent.engine import fingerprint
from labagent.report import generate_report
from labagent.store import Store
from test_engine import ROOT, case


def stages():
    return json.loads((ROOT / "data" / "scenarios" / "three_routers.json").read_text(encoding="utf-8"))["stages"]


@unittest.skipUnless(available(), "Install requirements-workflow.txt for real graph tests")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.db = Path(self.temp.name) / "graph.sqlite3"
        self.store = Store(Path(self.temp.name) / "sessions.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_real_graph_pause_resume_and_report_across_reopened_connections(self):
        values = stages()
        for index, payload in enumerate(values):
            if index:
                payload["session_id"] = session
            result = self.store.save(payload, run_workflow(payload, self.db))
            session = result["session_id"]
            self.assertEqual(result["workflow"]["backend"], "langgraph")
            self.assertEqual(result["workflow"]["resumed"], index > 0)
            self.assertEqual(result["workflow"]["paused"], index < 2)
            codes = {item["code"] for item in result["findings"]}
            if index == 0:
                self.assertIn("AREA_MISMATCH", codes)
            elif index == 1:
                self.assertEqual(codes, {"ROUTE_MISSING", "PING_FAILED"})
            else:
                self.assertEqual(result["status"], "verified_from_observations")
        history = self.store.history(session)
        report = generate_report(history)
        self.assertEqual(len(history["attempts"]), 3)
        self.assertIn("预置演示案例", report)
        self.assertIn("实验目标", report)
        self.assertIn("route_lookup", report)
        self.assertIn("AREA_MISMATCH", report)
        self.assertIn("ROUTE_MISSING", report)

    def test_planner_selects_actual_unexecuted_tool(self):
        from labagent import workflow
        executed = []
        original = workflow.check_link
        def tool(link, *args):
            executed.append(link["id"])
            return original(link, *args)
        def planner(payload, pending, completed):
            self.assertEqual(set(executed), set(completed))
            return pending[-1]
        with patch("labagent.workflow.check_link", side_effect=tool):
            result = run_workflow(stages()[0], self.db, planner)
        self.assertEqual(executed, ["r2-r3", "r1-r2"])
        self.assertTrue(result["workflow"]["paused"])

    def test_bad_planner_cannot_skip_checks(self):
        result = run_workflow(stages()[0], self.db, lambda *args: "nonexistent")
        checked = [step["link_id"] for step in result["workflow"]["steps"] if step["component"] == "实际诊断工具"]
        self.assertEqual(set(checked), {"r1-r2", "r2-r3"})

    def test_stale_command_outputs_cannot_verify(self):
        payload = stages()[2]
        payload["configs"]["R1"] += "# changed\n"
        result = run_workflow(payload, self.db)
        self.assertEqual(result["status"], "awaiting_verification")
        self.assertEqual(result["runtime_checks"], [])
        self.assertTrue(result["command_output_parsing"]["warnings"])

    def test_incomplete_table_does_not_prove_route_absence(self):
        payload = stages()[1]
        for output in payload["command_outputs"]:
            if output["command"] == "display ip routing-table":
                output["complete"] = False
        result = run_workflow(payload, self.db)
        self.assertNotIn("ROUTE_MISSING", {item["code"] for item in result["findings"]})
        self.assertTrue(any("路由表" in item for item in result["runtime_missing"]))

    def test_longest_prefix_route_is_selected(self):
        payload = stages()[2]
        for output in payload["command_outputs"]:
            if output["command"] == "display ip routing-table" and output["device"] == "R1":
                output["text"] += "0.0.0.0/0 Static 60 0 RD 10.12.0.99 GE0/0/0\n"
        result = run_workflow(payload, self.db)
        check = next(c for c in result["runtime_checks"] if c["check"] == "route_lookup" and c["device"] == "R1")
        self.assertEqual(check["route"]["network"], "3.3.3.3/32")

    def test_reverse_route_failure_is_visible(self):
        payload = stages()[2]
        for output in payload["command_outputs"]:
            if output["device"] == "R3" and output["command"] == "display ip routing-table":
                output["text"] = "Destination/Mask Proto Pre Cost Flags NextHop Interface\n10.23.0.0/24 Direct 0 0 D 10.23.0.3 GE0/0/0\n"
        result = run_workflow(payload, self.db)
        missing = next(f for f in result["findings"] if f["code"] == "ROUTE_MISSING")
        self.assertEqual(missing["devices"], ["R3"])
        self.assertNotEqual(result["status"], "verified_from_observations")

    def test_wait_report_does_not_claim_completion(self):
        payload = case()
        result = self.store.save(payload, run_workflow(payload, self.db))
        report = generate_report(self.store.history(result["session_id"]))
        self.assertIn("等待运行复测", report)
        self.assertIn("待补充", report)


class OutputParserTests(unittest.TestCase):
    def test_unrelated_full_neighbor_does_not_satisfy_link(self):
        payload = stages()[2]
        payload["command_outputs"][0]["text"] = payload["command_outputs"][0]["text"].replace("10.12.0.2", "10.12.0.99")
        prepared, parsed = normalize_outputs(payload)
        self.assertNotIn("r1-r2", [item["link_id"] for item in prepared["observations"]["neighbors"]])

    def test_malformed_route_row_invalidates_whole_table(self):
        self.assertIsNone(parse_routes("Destination/Mask Proto Pre Cost Flags NextHop Interface\n3.3.3.3/32 malformed row\n"))

    def test_new_outputs_do_not_rebind_old_manual_observations(self):
        payload = stages()[2]
        payload["observations"] = {"config_fingerprint": "old", "neighbors": [{"link_id": "r1-r2", "state": "Down"}], "pings": []}
        prepared, _ = normalize_outputs(payload)
        self.assertEqual(prepared["observations"]["config_fingerprint"], fingerprint(payload))
        self.assertTrue(all(row["state"] == "Full" for row in prepared["observations"]["neighbors"]))
