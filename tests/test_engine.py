import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from labagent.adapters import diagnose_ospf_tool
from labagent.engine import diagnose, fingerprint
from labagent.model import tutor
from labagent.parser import area_id, interface_id, parse_config

ROOT = Path(__file__).resolve().parents[1]


def case(name="02_static_ok"):
    return json.loads((ROOT / "data" / "cases" / (name + ".json")).read_text(encoding="utf-8"))


class EngineTests(unittest.TestCase):
    def test_all_hand_defined_cases(self):
        for path in (ROOT / "data" / "cases").glob("*.json"):
            with self.subTest(case=path.stem):
                payload = json.loads(path.read_text(encoding="utf-8"))
                expected = payload.pop("expected")
                result = diagnose(payload)
                self.assertEqual({"codes": sorted({x["code"] for x in result["findings"]}), "unknown_codes": sorted({x["code"] for x in result["unknowns"]}), "status": result["status"]}, expected)

    def test_unseen_names_addresses_and_area(self):
        payload = case()
        payload["configs"] = {"EdgeA": payload["configs"]["R1"].replace("10.12.0", "172.27.8").replace("area 0", "area 42"), "EdgeB": payload["configs"]["R2"].replace("10.12.0", "172.27.8").replace("area 0", "area 43")}
        payload["topology"]["links"][0]["endpoints"][0]["device"] = "EdgeA"
        payload["topology"]["links"][0]["endpoints"][1]["device"] = "EdgeB"
        payload["requirements"] = {"expected_area": "42"}
        result = diagnose(payload)
        self.assertEqual({f["code"] for f in result["findings"]}, {"AREA_MISMATCH", "AREA_POLICY_VIOLATION"})
        policy = next(f for f in result["findings"] if f["code"] == "AREA_POLICY_VIOLATION")
        self.assertEqual(policy["devices"], ["EdgeB"])
        self.assertEqual(policy["expected"], "0.0.0.42")

    def test_direct_interface_enable(self):
        payload = case()
        for name in payload["configs"]:
            payload["configs"][name] = payload["configs"][name].replace(" undo shutdown", " undo shutdown\n ospf enable 1 area 0").replace("  network 10.12.0.0 0.0.0.255\n", "")
        self.assertEqual(diagnose(payload)["status"], "awaiting_verification")

    def test_ambiguous_area_is_unknown(self):
        payload = case()
        payload["configs"]["R2"] = payload["configs"]["R2"].replace("#\nreturn", " area 1\n  network 10.12.0.0 0.0.0.255\n#\nreturn")
        result = diagnose(payload)
        self.assertEqual(result["status"], "needs_information")
        self.assertIn("AMBIGUOUS_OSPF_BINDING", {f["code"] for f in result["unknowns"]})

    def test_ip_parse_error_never_verifies(self):
        payload = case("03_verified_demo")
        payload["configs"]["R2"] = payload["configs"]["R2"].replace("10.12.0.2 255.255.255.0", "999.0.0.2 255.255.255.0")
        payload["observations"]["config_fingerprint"] = fingerprint(payload)
        result = diagnose(payload)
        self.assertEqual(result["status"], "needs_information")
        self.assertIn("PARSE_ERROR", {f["code"] for f in result["unknowns"]})

    def test_missing_interface_does_not_invent_area_error(self):
        payload = case()
        payload["topology"]["links"][0]["endpoints"][1]["interface"] = "GE9/9/9"
        result = diagnose(payload)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["status"], "needs_information")

    def test_stale_observations_cannot_verify(self):
        payload = case("03_verified_demo")
        payload["configs"]["R2"] += "# configuration revised\n"
        result = diagnose(payload)
        self.assertEqual(result["status"], "awaiting_verification")
        self.assertTrue(all(row["status"] == "UNKNOWN" for row in result["runtime_checks"]))

    def test_stale_failed_observations_do_not_invent_current_fault(self):
        payload = case("03_verified_demo")
        payload["observations"]["config_fingerprint"] = "old-config"
        payload["observations"]["pings"][0]["received"] = 0
        self.assertEqual(diagnose(payload)["findings"], [])

    def test_loss_and_neighbor_failure_are_detected_when_bound(self):
        payload = case("03_verified_demo")
        payload["observations"]["neighbors"][0]["state"] = "Init"
        payload["observations"]["pings"][0]["received"] = 2
        result = diagnose(payload)
        self.assertEqual({f["code"] for f in result["findings"]}, {"PING_FAILED", "NEIGHBOR_NOT_FULL"})

    def test_boolean_packet_counts_are_not_valid_integers(self):
        payload = case("03_verified_demo")
        payload["observations"]["pings"][0].update(sent=True, received=True)
        self.assertEqual(diagnose(payload)["status"], "awaiting_verification")

    def test_conflicting_duplicate_observations_cannot_verify(self):
        payload = case("03_verified_demo")
        payload["observations"]["neighbors"].append({"link_id": "r1-r2", "state": "Down"})
        self.assertEqual(diagnose(payload)["status"], "awaiting_verification")

    def test_static_fault_cannot_be_overridden_by_success_observations(self):
        payload = case("03_verified_demo")
        payload["configs"]["R2"] = payload["configs"]["R2"].replace(" area 0", " area 1")
        payload["observations"]["config_fingerprint"] = fingerprint(payload)
        self.assertEqual(diagnose(payload)["status"], "fault_found")

    def test_unspecified_ping_target_prevents_verification(self):
        payload = case("03_verified_demo")
        payload["topology"]["ping_targets"] = []
        payload["observations"]["config_fingerprint"] = fingerprint(payload)
        self.assertEqual(diagnose(payload)["status"], "awaiting_verification")

    def test_evidence_points_to_actual_input_line(self):
        payload = case("01_area_mismatch")
        for item in diagnose(payload)["findings"]:
            for evidence in item["evidence"]:
                self.assertEqual(payload["configs"][evidence["device"]].splitlines()[evidence["line"] - 1].strip(), evidence["text"])
                self.assertIn("area ", evidence["text"])

    def test_retrieval_has_provenance(self):
        docs = diagnose(case("01_area_mismatch"))["knowledge"]
        self.assertEqual(docs[0]["id"], "ospf-area")
        self.assertTrue(all(doc["source_type"] and doc["reference_url"].startswith("https://") for doc in docs))

    def test_tool_adapter_uses_same_contract(self):
        payload = case()
        result = diagnose_ospf_tool(payload["configs"], payload["topology"], payload["requirements"])
        self.assertEqual(result["status"], "awaiting_verification")
        self.assertEqual(result["mode"], "local_rules")

    def test_hint_levels_do_not_change_diagnosis(self):
        payload = case("01_area_mismatch")
        first = diagnose(payload)
        payload["hint_level"] = 3
        third = diagnose(payload)
        self.assertEqual(first["findings"], third["findings"])
        self.assertNotEqual(first["hint"], third["hint"])

    def test_malformed_requests_are_rejected(self):
        for mutate in (lambda p: p.update(configs=[]), lambda p: p.update(hint_level=True), lambda p: p.update(topology={"links": []}), lambda p: p.update(observations={"neighbors": [None]}), lambda p: p.update(observations={"neighbors": [{"link_id": "x", "state": None}]}), lambda p: p.update(requirements=[])):
            payload = case()
            mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                diagnose(payload)

    def test_llm_not_configured_preserves_engine(self):
        payload = case()
        payload["use_llm"] = True
        with patch.dict(os.environ, {}, clear=True):
            result = diagnose(payload)
        self.assertEqual(result["llm"]["status"], "not_configured")
        self.assertEqual(result["status"], "awaiting_verification")

    def test_llm_failure_does_not_leak_key(self):
        with patch.dict(os.environ, {"LABAGENT_LLM_URL": "https://model.invalid/chat", "LABAGENT_LLM_MODEL": "test", "LABAGENT_LLM_KEY": "secret-value"}), patch("labagent.model.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("secret-value")
            result = tutor(diagnose(case()))
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret-value", json.dumps(result))

    def test_parser_interface_aliases_and_area_normalization(self):
        self.assertEqual(interface_id("GigabitEthernet0/0/1"), interface_id("GE0/0/1"))
        self.assertEqual(area_id("42"), "0.0.0.42")

    def test_quit_restores_process_context(self):
        payload = case()
        payload["configs"]["R1"] = payload["configs"]["R1"].replace("#\nreturn", " quit\n silent-interface GE0/0/0\n quit\nreturn")
        self.assertIn("SILENT_INTERFACE", {f["code"] for f in diagnose(payload)["findings"]})


if __name__ == "__main__":
    unittest.main()
