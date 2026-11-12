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
