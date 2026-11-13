import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from labagent.investigation import source_bundle, validate_open
from labagent.operation_history import inspect_history
from labagent.recordings import Recordings, MAX_IMAGE_BYTES
from scripts.extract_recording import image_regions, extract_image

ROOT = Path(__file__).resolve().parents[1]


class ScreenshotTests(unittest.TestCase):
    def test_columns_form_one_citable_line_with_union_coordinates(self):
        result = SimpleNamespace(txts=["4 packets transmitted,", "4 received", "<R1>ping 10.0.0.2", "noise"],
            boxes=[[[10,50],[190,50],[190,70],[10,70]], [[200,51],[290,51],[290,71],[200,71]],
                   [[10,10],[260,10],[260,30],[10,30]], [[0,90],[10,90],[10,100],[0,100]]], scores=[.98,.95,.99,.3])
        rows = image_regions(result)
        self.assertEqual([row["line"] for row in rows], [1,2])
        self.assertEqual(rows[1]["text"], "4 packets transmitted, 4 received")
        self.assertEqual(rows[1]["box"], [10,50,290,71])

    def test_image_jobs_keep_the_original_and_survive_service_reopening(self):
        root = ROOT / "runtime/tests"; root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temp, patch("labagent.recordings.available", return_value=True), patch.object(Recordings, "_process"):
            service = Recordings(temp)
            job = service.upload(io.BytesIO(b"png-bytes"), 9, "报错.PNG")
            restored = Recordings(temp).status(job["id"])
            self.assertEqual(restored["kind"], "image")
            self.assertIn("/image", restored["image_url"])
            self.assertEqual((service.folder(job["id"]) / "image.png").read_bytes(), b"png-bytes")
            with self.assertRaisesRegex(ValueError, "20MB"):
                service.upload(io.BytesIO(), MAX_IMAGE_BYTES+1, "large.png")

    def test_invalid_image_returns_a_reupload_message(self):
        root = ROOT / "runtime/tests"; root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temp:
            folder = Path(temp)
            (folder / "image.png").write_bytes(b"not a picture")
            (folder / "status.json").write_text(json.dumps({"id":"a"*32,"name":"bad.png","suffix":".png","kind":"image"}), encoding="utf-8")
            with self.assertRaises(Exception):
                extract_image(folder)
            status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "error")
            self.assertIn("重新导出", status["message"])

    def test_screenshot_commands_remain_visible_evidence_without_invented_times(self):
        payload = {"configs":{"报错-a12345-截图识别.log":"[R2-GigabitEthernet0/0/0]network 10.2.2.2 0.0.0.0\nError: Unrecognized command"}, "question":"这一步怎么改", "recordings":["a"*32]*10}
        validate_open(payload)
        sources = source_bundle(payload)[0]
        self.assertEqual(sources["config:1"]["kind"], "screenshot_ocr")
        history = inspect_history(sources)
        self.assertEqual(history["error_count"], 1)
        self.assertIsNone(history["events"][0]["seconds"])
        self.assertEqual(history["events"][0]["evidence"][0]["line"], 1)


if __name__ == "__main__":
    unittest.main()
