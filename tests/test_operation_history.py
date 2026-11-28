import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.investigation import source_bundle, execute_tool, investigate
from labagent.network_map import build_map
from labagent.operation_history import inspect_history, prompt_info, time_label
from labagent.recordings import Recordings, changed_lines
from labagent.report import generate_report
from labagent.store import Store
from scripts.verify_replay import score_events

ROOT = Path(__file__).resolve().parents[1]


def example():
    return (ROOT / "data/examples/终端操作回溯.log").read_text(encoding="utf-8")


def sources(text=None):
    return source_bundle({"configs": {"操作.log": text if text is not None else example()}})[0]


class OperationHistoryTests(unittest.TestCase):
    def test_finds_the_wrong_view_at_its_recorded_time(self):
        history = inspect_history(sources())
        event = next(item for item in history["events"] if item["id"] == history["first_issue_id"])
        self.assertEqual(history["error_count"], 1)
        self.assertEqual(event["command"], "network 10.2.2.2 0.0.0.0")
        self.assertEqual((event["seconds"], event["device"], event["view"]), (3, "R2", "interface"))
        self.assertIn("区域视图", event["explanation"])
        self.assertFalse(event["recovered"])

    def test_entered_commands_do_not_invent_success(self):
        history = inspect_history(sources("[00:01] [R2]ip address 10.0.0.1 24"))
        self.assertEqual(history["events"][0]["status"], "issued")
        self.assertEqual(history["changes"], [])

    def test_failed_commands_do_not_enter_accepted_changes(self):
        history = inspect_history(sources())
        self.assertNotIn("network 10.2.2.2 0.0.0.0", [change["command"] for change in history["changes"]])
        self.assertIn("network 10.12.0.2 0.0.0.0", [change["command"] for change in history["changes"]])

    def test_later_retry_in_correct_view_recovers_the_old_error(self):
        text = example() + "\n[00:14] [R2-ospf-1-area-0.0.0.0]network 10.2.2.2 0.0.0.0\n[00:15] [R2-ospf-1-area-0.0.0.0]"
        history = inspect_history(sources(text))
        failed = next(item for item in history["events"] if item["status"] == "error")
        self.assertTrue(failed["recovered"])
        self.assertEqual(history["unresolved_error_count"], 0)

    def test_later_failed_retry_does_not_resolve_an_error(self):
        text = example() + "\n[00:14] [R2]network 10.2.2.2 0.0.0.0\nError: Unrecognized command"
        self.assertEqual(inspect_history(sources(text))["unresolved_error_count"], 2)

    def test_frr_unknown_command_links_to_the_successful_retry(self):
        text = "\n".join(("[00:02.000] R2# router ospf", "[00:02.000] % Unknown command: router ospf",
                          "[00:03.500] R2# configure terminal", "[00:04.500] R2(config)# router ospf",
                          "[00:04.500] R2(config-router)#"))
        history = inspect_history(sources(text))
        first, _, retry = history["events"]
        self.assertEqual((first["status"], retry["status"]), ("error", "returned"))
        self.assertEqual(first["retry_event_id"], retry["id"])
        annotations = {"events": [
            {"id": "error", "device": "R2", "view": "user", "command": "router ospf", "seconds": 1.625,
             "status": "error", "retry_to": "retry"},
            {"id": "retry", "device": "R2", "view": "system", "command": "router ospf", "seconds": 4.125,
             "status": "returned"},
        ]}
        summary = score_events(history, annotations)["summary"]
        self.assertEqual(summary["commands"], {"matched": 2, "total": 2})
        self.assertEqual(summary["in_time"], {"matched": 2, "total": 2})
        self.assertEqual(summary["retry_matches"], {"matched": 1, "total": 1})
        self.assertEqual(summary["unmatched_detected_events"], 1)

    def test_log_comparison_pairs_retries_without_video_time(self):
        raw = "\n".join(("R2# router ospf", "% Unknown command: router ospf",
                         "R2# configure terminal", "R2(config)# router ospf", "R2(config-router)#"))
        annotations = {"events": [
            {"id": "a", "device": "R2", "view": "user", "command": "router ospf", "seconds": 2,
             "status": "error", "retry_to": "b"},
            {"id": "b", "device": "R2", "view": "system", "command": "router ospf", "seconds": 6,
             "status": "returned"},
        ]}
        result = score_events(inspect_history(sources(raw)), annotations, tolerance=None)
        self.assertEqual(result["summary"], {
            "commands": {"matched": 2, "total": 2}, "unmatched_detected_events": 1,
            "view_matches": {"matched": 2, "total": 2}, "status_matches": {"matched": 2, "total": 2},
            "retry_matches": {"matched": 1, "total": 1},
        })
        self.assertNotIn("tolerance_seconds", result)
        self.assertNotIn("observed_seconds", result["rows"][0])
        self.assertNotIn("in_time", result["rows"][0])

    def test_replay_scores_a_quick_retry_in_command_order(self):
        text = "\n".join(("[00:00.500] R2# router ospf", "[00:00.500] % Unknown command: router ospf",
                          "[00:01.000] R2# configure terminal", "[00:01.000] R2(config)# router ospf",
                          "[00:01.000] R2(config-router)#"))
        history = inspect_history(sources(text))
        annotations = {"events": [
            {"id": "error", "device": "R2", "view": "user", "command": "router ospf", "seconds": 1.0,
             "status": "error", "retry_to": "retry"},
            {"id": "retry", "device": "R2", "view": "system", "command": "router ospf", "seconds": 1.5,
             "status": "returned"},
        ]}
        scores = score_events(history, annotations)
        self.assertEqual([row["observed_seconds"] for row in scores["rows"]], [0.5, 1.0])
        self.assertEqual(scores["summary"]["commands"], {"matched": 2, "total": 2})
        self.assertEqual(scores["summary"]["in_time"], {"matched": 2, "total": 2})
        self.assertEqual(scores["summary"]["view_matches"], {"matched": 2, "total": 2})
        self.assertEqual(scores["summary"]["status_matches"], {"matched": 2, "total": 2})
        self.assertEqual(scores["summary"]["retry_matches"], {"matched": 1, "total": 1})

    def test_selected_frr_routes_are_output_and_not_device_prompts(self):
        for marker in ("O>*", "0>*", "0> *"):
            with self.subTest(marker=marker):
                route = f"{marker} 192.168.20.0/24 [110/20] via 10.12.0.2"
                text = "\n".join(("[00:02] R1# show ip route ospf", route,
                                  "C> 2001:db8::/64 is directly connected, eth0", "[00:02] R1#"))
                history = inspect_history(sources(text))
                self.assertEqual(history["devices"], ["R1"])
                event, = history["events"]
                self.assertEqual(event["status"], "returned")
                self.assertTrue(any(route == row["quote"] for row in event["response_evidence"]))
        self.assertEqual(prompt_info("0> show ip route")[:2], ("0", "user"))

    def test_device_names_and_ios_views_remain_distinct(self):
        self.assertEqual(prompt_info("[Lab-Edge-7-ospf-10-area-0.0.0.1]network 10.0.0.0 0.0.0.255")[:2], ("Lab-Edge-7", "ospf_area"))
        self.assertEqual(prompt_info("R4(config-router)#network 10.0.0.0 0.0.0.255 area 0")[:2], ("R4", "router"))
        self.assertEqual(prompt_info("R4(config-if)#shutdown")[:2], ("R4", "interface"))

    def test_later_undo_is_the_latest_change_for_the_same_setting(self):
        text = "[00:01] [R2-GigabitEthernet0/0/0]shutdown\n[00:02] [R2-GigabitEthernet0/0/0]undo shutdown\n[00:03] [R2-GigabitEthernet0/0/0]"
        history = inspect_history(sources(text))
        self.assertEqual(len(history["latest_changes"]), 1)
        self.assertEqual(history["latest_changes"][0]["operation"], "remove")

    def test_error_words_inside_display_output_are_not_cli_errors(self):
        text = "<R1>display current-configuration\ndescription Error: app description\nreturn\n<R1>"
        self.assertEqual(inspect_history(sources(text))["error_count"], 0)

    def test_original_citations_include_timestamps_and_sensitive_lines_are_redacted(self):
        current = sources(example() + "\n[00:14] [R2]password PRIVATE-TEST-VALUE\nError: wrong input")
        history = inspect_history(current)
        self.assertNotIn("PRIVATE-TEST-VALUE", json.dumps(history))
        self.assertEqual(history["error_count"], 1)
        for event in history["events"]:
            for citation in event["evidence"] + event["response_evidence"]:
                self.assertEqual(current[citation["source_id"]]["lines"][citation["line"] - 1], citation["quote"])

    def test_history_tool_preserves_a_retry_when_later_operations_fill_the_summary(self):
        logs = {
            "重试录像.log": ("[00:02.500] R2# router ospf\n% Unknown command: router ospf\n"
                            "[00:06.500] R2(config)# router ospf\nR2(config-router)#"),
            "后续录像.log": "\n".join(f"R1# show ip route {index}" for index in range(15)),
        }
        payload = {"configs": logs, "question": "回溯报错与后续重试"}
        current, _ = source_bundle(payload)
        summary, _ = execute_tool("inspect_operation_timeline", {}, payload, current, set())
        error, retry, *recent = summary["events"]
        self.assertEqual(error["retry_event_id"], retry["id"])
        self.assertEqual((error["seconds"], retry["seconds"]), (2.5, 6.5))
        self.assertEqual(retry["evidence"][0]["quote"], "[00:06.500] R2(config)# router ospf")
        self.assertEqual(retry["status"], "returned")
        self.assertEqual([event["command"] for event in recent], [f"show ip route {i}" for i in range(4, 15)])
        self.assertEqual(len({event["id"] for event in summary["events"]}), 13)

    def test_long_timeline_keeps_late_error_retry_and_report_locations(self):
        route_log = "\n".join(["Early-Edge# show ip route summary"] + ["R1# show ip route ospf"] * 420 + ["R1#"])
        recording = ("[00:02.500] R2# router ospf\n% Unknown command: router ospf\n"
                     "[00:04.500] R2# configure terminal\n"
                     "[00:06.500] R2(config)# router ospf\nR2(config-router)#")
        payload = {"diagnostic_mode": "open_network", "use_llm": False, "question": "回溯报错",
                   "configs": {"查询.log": route_log, "重试-录屏识别.log": recording}}
        current, _ = source_bundle(payload)
        full = inspect_history(current, full=True)
        result = investigate(payload)
        timeline = result["operation_history"]
        self.assertEqual((timeline["event_count"], len(timeline["events"])), (424, 400))
        self.assertEqual(len(full["events"]), 424)
        self.assertEqual(full["events"][0]["command"], "show ip route summary")
        self.assertEqual(timeline["events"], full["events"][-400:])
        early_device = next(item for item in build_map(current)["nodes"] if item["id"] == "Early-Edge")
        self.assertIn(full["events"][0]["evidence"][0], early_device["evidence"])
        error, navigation, retry = timeline["events"][-3:]
        self.assertEqual(timeline["first_issue_id"], error["id"])
        self.assertEqual(timeline["focus_id"], error["id"])
        self.assertEqual(error["retry_event_id"], retry["id"])
        self.assertEqual((error["seconds"], retry["seconds"]), (2.5, 6.5))
        self.assertEqual(navigation["command"], "configure terminal")
        self.assertEqual((error["line"], retry["line"]), (1, 4))
        report = generate_report({"session_id": "test", "attempts": [
            {"created_at": "2026-11-28T08:00:00+08:00", "input": payload, "result": result}],
                                  "learning_record": {"config_revisions": 0}})
        self.assertIn("识别424步操作、1处报错。", report)
        self.assertIn("下方列出400步", report)
        self.assertIn("关键操作：00:02.5", report)
        self.assertIn("重试位置：00:06.5 · 重试-录屏识别.log 第4行", report)

        # The same pair must survive when it precedes all 421 routine operations.
        early = inspect_history(dict(reversed(list(current.items()))))
        early_error = early["events"][0]
        self.assertEqual(early["first_issue_id"], early_error["id"])
        self.assertEqual(early_error["retry_event_id"], early["events"][1]["id"])
        self.assertEqual(early["events"][1]["command"], "router ospf")

    def test_summary_keeps_unresolved_focus_after_earlier_recovered_errors(self):
        attempts = [f"R{i}# router ospf\n% Unknown command: router ospf\n"
                    f"R{i}(config)# router ospf\nR{i}(config-router)#" for i in range(1, 9)]
        attempts.append("R9# router ospf\n% Unknown command: router ospf")
        payload = {"configs": {"操作.log": "\n".join(attempts)}, "question": "回溯待解决的报错"}
        current, _ = source_bundle(payload)
        summary, _ = execute_tool("inspect_operation_timeline", {}, payload, current, set())
        events = {event["id"]: event for event in summary["events"]}
        focus = events[summary["first_issue_id"]]
        self.assertEqual((focus["device"], focus["status"], focus["recovered"]), ("R9", "error", False))
        errors = [event for event in events.values() if event["status"] == "error"]
        self.assertEqual(len(errors), 8)
        self.assertEqual(summary["error_count"], 9)
        for error in errors:
            if error["recovered"]:
                retry = events[error["retry_event_id"]]
                self.assertEqual((retry["device"], retry["status"]), (error["device"], "returned"))

    def test_history_tool_registers_evidence_and_focus_requires_a_real_event(self):
        payload = {"configs": {"操作.log": example()}, "question": "排查操作"}
        current, _ = source_bundle(payload); seen, context = set(), {}
        history, _ = execute_tool("inspect_operation_timeline", {}, payload, current, seen, diagnostic_context=context)
        args = {"summary": "网络命令输入视图错误", "hypotheses": [], "evidence_requests": [{"device": "R2", "information": "修改后输出", "why": "检查配置"}], "operation_focus": history["first_issue_id"]}
        result, _ = execute_tool("submit_hypotheses", args, payload, current, seen, diagnostic_context=context)
        self.assertEqual(result["operation_focus"], history["first_issue_id"])
        args["operation_focus"] = "config:1:999"
        with self.assertRaises(ValueError):
            execute_tool("submit_hypotheses", args, payload, current, seen, diagnostic_context=context)

    def test_report_and_restored_result_contain_the_same_time_and_error(self):
        payload = {"diagnostic_mode": "open_network", "configs": {"操作.log": example()}, "question": "排查操作", "use_llm": False}
        result = investigate(payload)
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            store = Store(Path(temp) / "sessions.sqlite3")
            saved = store.save(payload, result)
            restored = store.history(saved["session_id"])
            report = generate_report(restored)
            self.assertIn("## 实验过程回溯", report)
            self.assertIn("00:03", report)
            self.assertIn("接口视图", report)
            self.assertEqual(restored["attempts"][0]["result"]["operation_history"]["error_count"], 1)

    def test_report_distinguishes_recordings_with_the_same_timestamp(self):
        materials = {
            "启动 | A-录屏识别.log": "录屏识别\n[00:02.500] R1# show ip ospf neighbor\n[00:02.500] R1#",
            "重试 | B-录屏识别.log": ("[00:02.500] R2# router ospf\n[00:02.501] % Unknown command: router ospf\n"
                                    "[00:04.500] R2# configure terminal\n"
                                    "[00:06.500] R2(config)# router ospf\n[00:06.500] R2(config-router)#"),
            "复测.log": "PC1# ping 192.168.20.10\nPC1#",
        }
        payload = {"diagnostic_mode": "open_network", "configs": materials, "question": "排查操作", "use_llm": False}
        result = investigate(payload)
        report = generate_report({"session_id": "test", "attempts": [
            {"created_at": "2026-11-26T08:00:00+08:00", "input": payload, "result": result}],
                                  "learning_record": {"config_revisions": 0}})
        self.assertIn(r"| 00:02.5 | R1 / 用户视图 | show ip ospf neighbor | 终端已返回 | 启动 \| A-录屏识别.log 第2行 |", report)
        self.assertIn(r"| 00:02.5 | R2 / 用户视图 | router ospf | 报错后已重新输入 | 重试 \| B-录屏识别.log 第1行 |", report)
        self.assertIn("| 无时间戳 | PC1 / 用户视图 | ping 192.168.20.10 | 终端已返回 | 复测.log 第1行 |", report)
        self.assertIn("关键操作：00:02.5 · R2 · 用户视图\n\n重试 " + r"\| B-录屏识别.log 第1行", report)
        self.assertIn(r"重试位置：00:06.5 · 重试 \| B-录屏识别.log 第4行", report)
        self.assertIn(r"- 重试 \| B-录屏识别.log 第2行：[00:02.501] % Unknown command: router ospf", report)

    def test_plain_logs_keep_line_locations_without_fabricating_video_time(self):
        result = inspect_history(sources("[R2]network 10.2.2.2 0.0.0.0\nError: Unrecognized command"))
        self.assertIsNone(result["events"][0]["seconds"])
        self.assertEqual(time_label(None), "无时间戳")


class RecordingTests(unittest.TestCase):
    def test_scrolling_frames_preserve_new_commands_and_real_retries(self):
        self.assertEqual(changed_lines(["a", "b", "c"], ["b", "c", "d"]), ["d"])
        self.assertEqual(changed_lines(["a", "b"], ["a", "b"]), [])
        self.assertEqual(changed_lines(["a", "b", "c"], ["b", "c", "a"]), ["a"])
        self.assertEqual(changed_lines(["Error: old failure", "[R2-ospf-1]area 0", "[R2-ospf-1-area-0.0.0.0]network 10.0.0.0 0.0.0.255"],
            ["Error: old failure", "[R2-ospf-1]area 0", "[R2-ospf-1-area-O.0.0.0]network 10.0.0.0 0.0.0.255", "[R2-ospf-1-area-0.0.0.0]return"]), ["[R2-ospf-1-area-0.0.0.0]return"])

    def test_short_command_keeps_a_single_exit_across_device_switch(self):
        previous = ["R1# exit", "R2#"]
        current = ["R1# exit", "R2# show ip ospf neighbor", "R2#"]
        self.assertEqual(changed_lines(previous, current), current[1:])

    def test_command_completed_on_the_last_visible_prompt(self):
        previous = ["R2# router ospf", "% Unknown command: router ospf", "R2# configure terminal",
                    "R2(config)# router ospf", "R2(config-router)#"]
        current = previous[:-1] + ["R2(config-router)# end", "R2#"]
        self.assertEqual(changed_lines(previous, current), current[-2:])
        self.assertEqual(changed_lines(current, current), [])

    def test_scrolled_prompt_can_be_completed_with_a_repeated_command(self):
        previous = ["Older output", "<R2> display ospf peer", "Peer output", "<R2>"]
        current = previous[1:-1] + ["<R2> display ospf peer", "New peer output", "<R2>"]
        self.assertEqual(changed_lines(previous, current), current[2:])

    def test_upload_is_project_local_and_persisted_status_can_be_reopened(self):
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp, patch("labagent.recordings.available", return_value=True), patch.object(Recordings, "_process"):
            service = Recordings(temp)
            result = service.upload(io.BytesIO(b"test-video"), 10, "case.mp4")
            restored = Recordings(temp).status(result["id"])
            self.assertEqual(restored["status"], "queued")
            self.assertEqual((service.folder(result["id"]) / "video.mp4").read_bytes(), b"test-video")
            with self.assertRaises(ValueError):
                service.folder("../escape")

    def test_invalid_and_interrupted_uploads_have_specific_feedback(self):
        folder = ROOT / "runtime/tests"; folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as temp, patch("labagent.recordings.available", return_value=True):
            service = Recordings(temp)
            with self.assertRaisesRegex(ValueError, "mp4"):
                service.upload(io.BytesIO(b"x"), 1, "test.exe")
            with self.assertRaisesRegex(ValueError, "中断"):
                service.upload(io.BytesIO(b"x"), 3, "test.mp4")


if __name__ == "__main__":
    unittest.main()
