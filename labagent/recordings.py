"""Project-local video uploads and persisted OCR jobs."""
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIDEO_SUFFIXES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska"}
MAX_VIDEO_BYTES = 80 * 1024 * 1024


def write_status(folder, value):
    temporary = folder / "status.tmp"
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    for attempt in range(8):
        try:
            temporary.replace(folder / "status.json")
            return
        except PermissionError:
            # Windows readers briefly hold the destination without delete sharing.
            if attempt == 7:
                raise
            time.sleep(0.025 * (attempt + 1))


def available():
    return all(importlib.util.find_spec(name) is not None for name in ("rapidocr", "onnxruntime", "cv2"))


class Recordings:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.slot = threading.Semaphore(1)

    def folder(self, recording_id):
        if not re.fullmatch(r"[a-f0-9]{32}", recording_id):
            raise ValueError("录屏编号无效")
        return self.root / recording_id

    def status(self, recording_id):
        path = self.folder(recording_id) / "status.json"
        if not path.exists():
            return None
        result = json.loads(path.read_text(encoding="utf-8"))
        if result["status"] == "ready":
            result["transcript"] = (path.parent / "transcript.log").read_text(encoding="utf-8")
        return result

    def upload(self, stream, length, filename):
        suffix = Path(filename).suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            raise ValueError("录屏支持mp4、webm、mov、mkv")
        if not 0 < length <= MAX_VIDEO_BYTES:
            raise ValueError("单份录屏请保持在80MB以内")
        if not available():
            raise ValueError("请先安装requirements-replay.txt中的录屏识别组件")
        recording_id = uuid.uuid4().hex
        folder = self.folder(recording_id)
        folder.mkdir()
        remaining = length
        with (folder / ("video" + suffix)).open("wb") as target:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("录屏上传中断，请重新选择文件")
                target.write(chunk)
                remaining -= len(chunk)
        value = {"id": recording_id, "name": Path(filename).name[:160], "suffix": suffix, "status": "queued", "progress": 0,
                 "message": "录屏已上传，等待识别", "video_url": f"/api/recordings/{recording_id}/video"}
        write_status(folder, value)
        threading.Thread(target=self._process, args=(folder,), daemon=True).start()
        return value

    def _process(self, folder):
        with self.slot:
            try:
                with (folder / "worker.log").open("wb") as log:
                    process = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/extract_recording.py"), str(folder.resolve())],
                                             cwd=ROOT, stdout=log, stderr=log, timeout=1800)
                status = self.status(folder.name)
                if process.returncode and status["status"] != "error":
                    raise RuntimeError("录屏识别进程退出，请查看项目内worker.log")
            except Exception as error:
                status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
                write_status(folder, {**status, "status": "error", "message": str(error)[:200]})


def changed_lines(previous, current):
    """Only append newly visible terminal lines; preserve legitimate retries."""
    normalize = lambda line: "".join(line.lower().split())
    old, new = [normalize(line) for line in previous], [normalize(line) for line in current]
    for size in range(min(len(old), len(new)), 0, -1):
        if old[-size:] == new[:size]:
            return current[size:]
    if old == new:
        return []
    from difflib import SequenceMatcher
    # Anchor at the latest visible command. Small OCR spelling changes in older
    # rows must not append old error output after a newer command.
    for previous_line in reversed(old[-4:]):
        if len(previous_line) < 8:
            continue
        matches = [index for index, line in enumerate(new) if SequenceMatcher(None, previous_line, line, autojunk=False).ratio() >= 0.9]
        if matches:
            return current[matches[-1] + 1:]
    # OCR can revise one word while the rest of the terminal is unchanged.
    # A shared final line means there is no newly visible output below it.
    if old and new and old[-1] == new[-1]:
        return []
    match = SequenceMatcher(None, old, new, autojunk=False).find_longest_match()
    if match.size >= 2:
        return current[match.b + match.size:]
    return current
