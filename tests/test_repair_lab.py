from copy import deepcopy
import unittest

from labagent.repair_lab import RUN_KIND, compare_run, route_to


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
