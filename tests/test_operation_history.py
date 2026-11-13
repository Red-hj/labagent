import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent.investigation import source_bundle, execute_tool, investigate
from labagent.operation_history import inspect_history, prompt_info, time_label
from labagent.recordings import Recordings, changed_lines
from labagent.report import generate_report
from labagent.store import Store

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
