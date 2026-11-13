import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from labagent.server import create_server
from labagent.store import Store
from test_engine import ROOT, case


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=folder)
        cls.server = create_server(0, Path(cls.temp.name) / "test.sqlite3")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        assert Path(cls.temp.name).resolve().is_relative_to((ROOT / "runtime" / "tests").resolve())
        cls.temp.cleanup()

    def request(self, path, payload=None, headers=None):
        options = {"headers": headers or {}}
        if payload is not None:
            options.update(data=json.dumps(payload).encode(), method="POST")
            options["headers"].setdefault("Content-Type", "application/json")
        with urlopen(Request(self.base + path, **options), timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_health_and_cases(self):
        self.assertEqual(self.request("/api/health")[1]["app"], "LabAgent")
        cases = self.request("/api/cases")[1]
        self.assertTrue({"team_lab_01", "team_lab_02", "team_lab_03", "team_lab_04", "team_lab_05"} <= {row["id"] for row in cases})
        self.assertNotIn("expected", self.request("/api/cases/01_area_mismatch")[1])

    def test_team_cases_load_diagnose_and_continue_with_repaired_input(self):
        for number in range(1, 6):
            name = f"lab_{number:02}"
            with self.subTest(case=name):
                payload = self.request("/api/cases/team_" + name)[1]
                original = json.loads((ROOT / "data/team_cases" / name / "input.json").read_text(encoding="utf-8"))
                expected = json.loads((ROOT / "data/team_cases" / name / "expected.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["configs"], original["configs"])
                self.assertNotIn("expected", payload)
                self.assertNotIn("repair", payload)
                self.assertEqual(payload["knowledge_scope"], "b-ospf")
                self.assertFalse(payload["use_llm"])
                result = self.request("/api/diagnose", payload)[1]
                self.assertEqual(result["status"], expected["status"])
                self.assertEqual(sorted(row["code"] for row in result["findings"]), sorted(row["code"] for row in expected["findings"]))
                repaired = json.loads((ROOT / "data/team_cases" / name / "repaired.json").read_text(encoding="utf-8"))
                second = self.request("/api/diagnose", {**payload, **repaired, "session_id": result["session_id"]})[1]
                self.assertEqual(second["status"], "awaiting_verification")
                self.assertEqual(second["findings"], [])
                self.assertEqual(second["knowledge_scope"], "b-ospf")
                history = self.request("/api/sessions/" + result["session_id"])[1]
                self.assertEqual(len(history["attempts"]), 2)

    def test_open_mode_accepts_non_ospf_material_without_topology(self):
        payload = {"diagnostic_mode": "open_network", "question": "终端DHCP地址正常但外网不通，缺少哪些证据？", "configs": {"PC1": "Default Gateway: 10.0.0.1"}, "use_llm": False}
        result = self.request("/api/diagnose", payload)[1]
        self.assertEqual(result["mode"], "open_network")
        self.assertEqual(result["status"], "needs_information")
        self.assertEqual(result["llm"]["status"], "not_configured")
        self.assertEqual(result["findings"], [])
        report = self.request("/api/sessions/" + result["session_id"] + "/report")[1]["markdown"]
        self.assertIn("排查方向与原文依据", report)

    def test_demo_provides_current_materials_for_each_stage(self):
        from labagent.connectivity import parse_ping_records
        demo = self.request("/api/demos/network_access")[1]
        self.assertEqual(demo["source"], "合成演示材料")
        first, second = demo["stages"]
        self.assertEqual(set(first["materials"]), set(second["materials"]))
        initial = next(iter(first["materials"].values()))
        repaired = next(iter(second["materials"].values()))
        self.assertIn("gateway-list 10.20.0.254", initial)
        self.assertIn("gateway-list 10.20.0.1", repaired)
        self.assertNotIn("10.20.0.254", repaired)
        self.assertTrue(any(not row["passed"] for row in parse_ping_records(initial.splitlines())))
        records = parse_ping_records(repaired.splitlines())
        self.assertTrue(records)
        self.assertTrue(all(row["passed"] for row in records))
        self.assertTrue(any(row["target"] == "10.30.0.10" for row in records))

    def test_session_modes_cannot_be_mixed(self):
        first = self.request("/api/diagnose", case())[1]
        payload = {"session_id": first["session_id"], "diagnostic_mode": "open_network", "question": "网络排查", "configs": {"PC1": "test"}, "use_llm": False}
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/diagnose", payload)
        self.assertEqual(caught.exception.code, 400)

    def test_session_repair_cycle_and_learning_record(self):
        result = self.request("/api/diagnose", case("01_area_mismatch"))[1]
        self.assertEqual(result["status"], "fault_found")
        fixed = case("03_verified_demo")
        fixed["session_id"] = result["session_id"]
        second = self.request("/api/diagnose", fixed)[1]
        self.assertEqual(second["status"], "verified_from_observations")
        self.assertEqual(second["learning_record"]["config_revisions"], 1)
        history = self.request("/api/sessions/" + result["session_id"])[1]
        self.assertEqual(len(history["attempts"]), 2)

    def test_fingerprint_endpoint(self):
        self.assertEqual(len(self.request("/api/fingerprint", case())[1]["config_fingerprint"]), 64)

    def test_knowledge_import_search_and_scope_api(self):
        with patch("labagent.knowledge.NOTES_DIR", Path(self.temp.name) / "notes"):
            input = {"title": "API 测试资料", "content": "青岚课程要求记录回程路由。", "scope": "api-course"}
            first = self.request("/api/knowledge/import", input)[1]
            self.assertTrue(first["created"])
            self.assertFalse(self.request("/api/knowledge/import", input)[1]["created"])
            self.assertIn("api-course", self.request("/api/knowledge/scopes")[1]["scopes"])
            results = self.request("/api/knowledge/search", {"query": "青岚课程", "scope": "api-course"})[1]["results"]
            self.assertEqual(results[0]["id"], first["document"]["id"])
            listing = self.request("/api/knowledge/documents?scope=api-course")[1]
            self.assertIn(first["document"]["id"], {doc["id"] for doc in listing["documents"]})

    def test_invalid_knowledge_scope_returns_400(self):
        with self.assertRaises(HTTPError) as context:
            self.request("/api/knowledge/search", {"query": "x", "scope": "../outside"})
        self.assertEqual(context.exception.code, 400)

    def test_scenario_report_and_project_export(self):
        bundle = self.request("/api/scenarios/three_routers")[1]
        payload = bundle["stages"][0]
        result = self.request("/api/diagnose", payload)[1]
        report = self.request("/api/sessions/" + result["session_id"] + "/report")[1]
        self.assertIn("预置演示案例", report["markdown"])
        exported = self.request("/api/export", {"session_id": result["session_id"]})[1]
        self.assertTrue(Path(exported["report_path"]).resolve().is_relative_to(ROOT / "runtime" / "exports"))
        self.assertTrue(Path(exported["report_path"]).is_file())

    def test_unknown_session_rejected(self):
        payload = case(); payload["session_id"] = "f" * 32
        with self.assertRaises(HTTPError) as context:
            self.request("/api/diagnose", payload)
        self.assertEqual(context.exception.code, 400)

    def test_missing_saved_session_has_a_specific_error(self):
        with self.assertRaises(HTTPError) as context:
            self.request("/api/sessions/" + "f" * 32)
        self.assertEqual(context.exception.code, 404)
        error = json.loads(context.exception.read())
        self.assertEqual(error["code"], "session_not_found")
        self.assertIn("新的排查", error["error"])

    def test_stream_reports_actual_graph_progress_and_saves_final_result(self):
        import os
        from test_investigation import ENV, stages, completion
        payload = stages()[0]
        with patch.dict(os.environ, ENV), patch("labagent.investigation.chat_completion", side_effect=lambda messages, **kwargs: completion(messages)):
            request = Request(self.base + "/api/diagnose/stream", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"})
            with urlopen(request, timeout=5) as response:
                self.assertIn("application/x-ndjson", response.headers["Content-Type"])
                events = [json.loads(line) for line in response if line.strip()]
        self.assertEqual(events[0]["stage"], "received")
        self.assertTrue(any(event.get("stage") == "tool" and "SW1" in event["message"] for event in events))
        self.assertTrue(any(event.get("stage") == "saving" for event in events))
        self.assertEqual(events[-1]["type"], "result")
        result = events[-1]["result"]
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertEqual(len(self.request("/api/sessions/" + result["session_id"])[1]["attempts"]), 1)

    def test_cross_origin_write_rejected(self):
        with self.assertRaises(HTTPError) as context:
            self.request("/api/diagnose", case(), {"Origin": "https://untrusted.invalid"})
        self.assertEqual(context.exception.code, 403)

    def test_bad_request_returns_400_not_500(self):
        with self.assertRaises(HTTPError) as context:
            self.request("/api/diagnose", {"configs": None})
        self.assertEqual(context.exception.code, 400)

    def test_static_page_and_no_path_traversal(self):
        with urlopen(self.base + "/") as response:
            self.assertIn("LabAgent", response.read().decode())
            self.assertIn("frame-ancestors", response.headers["Content-Security-Policy"])
        with self.assertRaises(HTTPError):
            self.request("/api/cases/../../.env")

    def test_history_repeated_hint_request_is_not_a_revision(self):
        payload = case("01_area_mismatch")
        first = self.request("/api/diagnose", payload)[1]
        payload["session_id"] = first["session_id"]; payload["hint_level"] = 3
        result = self.request("/api/diagnose", payload)[1]
        self.assertEqual(result["learning_record"]["config_revisions"], 0)
        self.assertEqual(result["learning_record"]["max_hint_level"], 3)


if __name__ == "__main__":
    unittest.main()
