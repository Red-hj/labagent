import unittest

from labagent.connectivity import parse_ping_records
from labagent.investigation import execute_tool, source_bundle, validate_submission
from labagent.runtime_parser import parse_ping


class ConnectivityTests(unittest.TestCase):
    def test_vrp_multiline_and_raw_evidence(self):
        text = "<R1>ping 10.30.0.10\n  PING 10.30.0.10: 56 data bytes\n    Request time out\n  --- 10.30.0.10 ping statistics ---\n    5 packet(s) transmitted\n    0 packet(s) received\n    100.00% packet loss"
        rows = parse_ping_records(text.splitlines())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["target"], row["sent"], row["received"], row["passed"]), ("10.30.0.10", 5, 0, False))
        self.assertTrue(all(text.splitlines()[item["line"] - 1] == item["quote"] for item in row["evidence"]))
        self.assertEqual(parse_ping(text), {"target": "10.30.0.10", "sent": 5, "received": 0})

    def test_windows_chinese_and_english(self):
        outputs = [
            "正在 Ping 10.20.0.1 具有 32 字节的数据:\n来自 10.20.0.1 的回复: 字节=32\n10.20.0.1 的 Ping 统计信息:\n    数据包: 已发送 = 4，已接收 = 4，丢失 = 0 (0% 丢失)",
            "Pinging 10.20.0.1 with 32 bytes of data:\nReply from 10.20.0.1: bytes=32\nPing statistics for 10.20.0.1:\n    Packets: Sent = 4, Received = 4, Lost = 0 (0% loss)",
        ]
        for text in outputs:
            with self.subTest(text=text):
                rows = parse_ping_records(text.splitlines())
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["passed"])

    def test_linux_hostname_header(self):
        text = "PING server.lab (10.30.0.10) 56(84) bytes of data.\n--- server.lab ping statistics ---\n4 packets transmitted, 3 received, 25% packet loss, time 3005ms"
        row = parse_ping_records(text.splitlines())[0]
        self.assertEqual((row["target"], row["sent"], row["received"], row["passed"]), ("10.30.0.10", 4, 3, False))

    def test_different_and_repeated_targets_remain_separate(self):
        text = "Ping 10.20.0.1: 4 packets transmitted, 4 received\nPing 10.30.0.10: 4 packets transmitted, 0 received\nPing 10.20.0.1: 4 packets transmitted, 3 received"
        self.assertEqual([row["received"] for row in parse_ping_records(text.splitlines())], [4, 0, 3])

    def test_incomplete_outputs_do_not_borrow_other_counters(self):
        text = "<R1>ping 10.20.0.1\n5 packet(s) transmitted\n<R1>ping 10.30.0.10\n0 packet(s) received\n<R1>ping 10.20.0.1\n4 packet(s) received"
        self.assertEqual(parse_ping_records(text.splitlines()), [])
        same = "PING 10.20.0.1:\n5 packet(s) transmitted\nPING 10.20.0.1:\n4 packet(s) received"
        self.assertEqual(parse_ping_records(same.splitlines()), [])

    def test_invalid_addresses_and_counters_are_skipped(self):
        for text in ("Ping 999.1.1.1: 4 packets transmitted, 4 received", "Ping 10.0.0.1: 0 packets transmitted, 0 received", "Ping 10.0.0.1: 4 packets transmitted, 5 received", "Ping 10.0.0.1: -1 packets transmitted, 0 received"):
            with self.subTest(text=text):
                self.assertEqual(parse_ping_records(text.splitlines()), [])

    def test_sourced_ping_uses_destination_and_keeps_original_command(self):
        examples = (
            ("<R1>ping -a 1.1.1.1 -c 5 3.3.3.3", "3.3.3.3"),
            ("ping -a 1.1.1.1 3.3.3.3", "3.3.3.3"),
            ("pc1$ ping -I 10.12.0.1 10.30.0.10", "10.30.0.10"),
            ("pc1$ ping 10.30.0.10 -I10.12.0.1", "10.30.0.10"),
            ("C:\\>ping /a 10.30.0.10", "10.30.0.10"),
            ("ping -a 10.30.0.10", "10.30.0.10"),
            ("PC1 (10.12.0.1) 执行ping 10.30.0.10", "10.30.0.10"),
        )
        for command, target in examples:
            with self.subTest(command=command):
                lines = [command, "5 packets transmitted, 5 received"]
                record, = parse_ping_records(lines)
                self.assertEqual((record["target"], record["quote"], record["passed"]), (target, command, True))
                self.assertEqual(record["evidence"][-1]["quote"], lines[-1])

    def test_missing_destination_does_not_bind_counters_to_source_or_previous_ping(self):
        for lines in (
            ["<R1>ping -a 1.1.1.1", "5 packets transmitted, 5 received"],
            ["pc1$ ping -I 10.12.0.1 server.lab", "5 packets transmitted, 5 received"],
            ["PING 10.30.0.9", "5 packets transmitted", "<R1>ping -a 1.1.1.1", "5 received"],
        ):
            with self.subTest(lines=lines):
                self.assertEqual(parse_ping_records(lines), [])
        lines = ["pc1$ ping -I 10.12.0.1 server.lab",
                 "PING server.lab (10.30.0.10) from 10.12.0.1 eth0: 56(84) bytes of data.",
                 "5 packets transmitted, 5 received"]
        record, = parse_ping_records(lines)
        self.assertEqual((record["target"], record["line"], record["quote"]), ("10.30.0.10", 2, lines[1]))

    def test_sourced_ping_satisfies_only_the_destination_goal(self):
        from labagent.experiment_goals import check_goals

        payload = {"configs": {"R1.log": "<R1>ping -a 1.1.1.1 3.3.3.3\n5 packets transmitted, 5 received"},
                   "question": "检查 R1 到 3.3.3.3 的 Ping。"}
        sources, _ = source_bundle(payload)
        evidence = {"source_id": "question:initial", "device": "实验问题与要求", "line": 1, "quote": payload["question"]}
        goals = [{"id": address, "title": address, "kind": "reachability", "device": "R1", "target": address,
                  "expected": "", "instruction": evidence} for address in ("3.3.3.3", "1.1.1.1")]
        assessment = check_goals(goals, sources)
        checked, _ = execute_tool("check_submitted_pings", {}, payload, sources, set())
        self.assertEqual([check["status"] for check in assessment["checks"]], ["passed", "needs_evidence"])
        self.assertEqual(checked["pings"][0]["target"], "3.3.3.3")
        self.assertEqual(assessment["checks"][0]["evidence"], checked["pings"][0]["evidence"])

    def test_ping_keeps_the_source_reported_by_linux(self):
        header = "PING 10.30.0.10 (10.30.0.10) from 10.12.0.2 eth0: 56(84) bytes of data."
        counters = "5 packets transmitted, 5 received"
        for option, first_line in (("eth0", 1), ("10.12.0.1", 2)):
            with self.subTest(option=option):
                lines = [f"pc1$ ping -I {option} 10.30.0.10", header, counters]
                row, = parse_ping_records(lines)
                self.assertEqual((row["target"], row["source_address"], row["line"]), ("10.30.0.10", "10.12.0.2", first_line))
                self.assertTrue(all(lines[ref["line"] - 1] == ref["quote"] for ref in row["evidence"]))

    def test_distinct_source_addresses_keep_independent_retest_results(self):
        from labagent.experiment_goals import check_goals
        from labagent.network_map import build_map, explain_path

        target = "10.30.0.10"
        failed = f"<R1>ping -a 1.1.1.1 {target}\n5 packets transmitted, 0 received"
        passed = f"<R1>ping -a 2.2.2.2 {target}\n5 packets transmitted, 5 received"
        payload = {"configs": {"R1.log": failed + "\n" + passed}, "question": "核对 R1 的两次源地址测试"}
        goal = {"id": "reach", "title": payload["question"], "kind": "reachability", "device": "R1",
                "target": target, "expected": "", "instruction": {"source_id": "question:initial",
                    "device": "实验问题与要求", "line": 1, "quote": payload["question"]}}

        def review():
            sources, _ = source_bundle(payload)
            checked, _ = execute_tool("check_submitted_pings", {}, payload, sources, set())
            return sources, checked, check_goals([goal], sources)["checks"][0]

        sources, checked, check = review()
        self.assertEqual([(row["source_address"], row["received"]) for row in checked["pings"]], [("1.1.1.1", 0), ("2.2.2.2", 5)])
        self.assertEqual(check["status"], "failed")
        self.assertEqual(checked["superseded_pings"], [])
        route = explain_path(build_map(sources), "R1", target)
        self.assertEqual(route["status"], "failed")
        ping_steps = [step for step in route["steps"] if step["title"].startswith("端到端测试")]
        self.assertEqual([step["status"] for step in ping_steps], ["passed", "failed"])
        self.assertIn("2.2.2.2", ping_steps[0]["detail"])
        self.assertIn("1.1.1.1", ping_steps[1]["detail"])

        payload["interaction"] = {"type": "verify", "message":
            f"PING {target} ({target}) 56 data bytes\n5 packets transmitted, 5 received"}
        _, checked, check = review()
        self.assertEqual(len(checked["pings"]), 3)
        self.assertEqual(checked["superseded_pings"], [])
        self.assertEqual(check["status"], "failed")

        payload["interaction"]["message"] = f"<R1>ping -a 1.1.1.1 {target}\n5 packets transmitted, 5 received"
        _, checked, check = review()
        self.assertEqual([(row["source_address"], row["received"]) for row in checked["pings"]], [("1.1.1.1", 5), ("2.2.2.2", 5)])
        self.assertEqual([row["source_address"] for row in checked["superseded_pings"]], ["1.1.1.1"])
        self.assertEqual(check["status"], "passed")

    def test_later_complete_ping_in_one_log_replaces_earlier_failure(self):
        from labagent.experiment_goals import check_goals

        text = ("PING 10.30.0.10 (10.30.0.10) 56 data bytes\n4 packets transmitted, 0 received\n\n"
                "PING 10.30.0.10 (10.30.0.10) 56 data bytes\n4 packets transmitted, 4 received")
        payload = {"configs": {"PC1-run.log": text}, "question": "PC1到10.30.0.10的Ping无丢包"}
        sources, _ = source_bundle(payload)
        checked, _ = execute_tool("check_submitted_pings", {}, payload, sources, set())
        newest, = checked["pings"]
        replaced, = checked["superseded_pings"]
        self.assertEqual((newest["line"], newest["received"], replaced["line"], replaced["received"]), (4, 4, 1, 0))
        goal = {"id": "ping", "title": payload["question"], "device": "PC1", "kind": "reachability",
                "target": "10.30.0.10", "expected": "", "instruction": {"source_id": "question:initial",
                    "device": "实验问题与要求", "line": 1, "quote": payload["question"]}}
        assessment = check_goals([goal], sources)
        self.assertEqual(assessment["checks"][0]["status"], "passed")
        self.assertEqual(assessment["checks"][0]["evidence"], newest["evidence"])

    def test_multiline_counters_can_be_cited_in_model_submission(self):
        payload = {"configs": {"R1": "PING 10.30.0.10:\n5 packet(s) transmitted\n0 packet(s) received"}}
        sources, _ = source_bundle(payload)
        seen = set()
        checked, _ = execute_tool("check_submitted_pings", {}, payload, sources, seen)
        self.assertIn(("config:1", 3), seen)
        args = {"summary": "测试丢包", "hypotheses": [{"title": "目标不可达", "reasoning": "收到0个包", "evidence": [{"source_id": "config:1", "line": 3, "quote": "0 packet(s) received"}]}], "evidence_requests": [{"device": "R1", "information": "目标路由", "why": "定位下一跳"}]}
        self.assertEqual(validate_submission(args, sources, seen)["hypotheses"][0]["evidence"][0]["line"], 3)
        self.assertFalse(checked["pings"][0]["passed"])


if __name__ == "__main__":
    unittest.main()
