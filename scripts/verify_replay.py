"""Exercise real video OCR, operation reconstruction and optional model diagnosis."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from labagent.recordings import Recordings
from labagent.investigation import source_bundle
from labagent.operation_history import inspect_history
from labagent.material_service import MaterialService
from labagent.model import load_env
from labagent.report import generate_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    folder = ROOT / "runtime/replay-verification" / time.strftime("%Y%m%d-%H%M%S")
    service = Recordings(folder / "recordings")
    video = ROOT / "data/examples/实验操作回放.webm"
    with video.open("rb") as stream:
        job = service.upload(stream, video.stat().st_size, video.name)
    started = time.monotonic()
    while time.monotonic() - started < 240:
        status = service.status(job["id"])
        print(json.dumps({key: status.get(key) for key in ("status", "progress", "message")}, ensure_ascii=False), flush=True)
        if status["status"] == "error":
            raise RuntimeError(status["message"])
        if status["status"] == "ready":
            break
        time.sleep(3)
    else:
        raise RuntimeError("Video extraction timed out")
    materials = {status["material_name"]: status["transcript"], "实验要求.txt": "实验目标：R1通过OSPF访问R2的Loopback0地址10.2.2.2，Ping全部成功。"}
    operations = inspect_history(source_bundle({"configs": materials})[0])
    (folder / "operations.json").write_text(json.dumps(operations, ensure_ascii=False, indent=2), encoding="utf-8")
    issues = [event for event in operations["events"] if event["status"] == "error"]
    assert issues, "No CLI error found in actual OCR transcript"
    issue = issues[0]
    assert issue["device"] == "R2" and issue["view"] == "interface"
    assert abs(issue["seconds"] - 3) <= 0.5
    if args.live_model:
        app = MaterialService(folder / "sessions.sqlite3")
        response = app.invoke({"materials": materials, "question": "回溯录屏中的操作，解释关键错误步骤及怎样继续。", "evidence_kind": "demo", "recordings": [job["id"]]})
        (folder / "diagnosis.json").write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
        assert response["status"] == "success", response["reply"]
        assert any(tool["tool"] == "inspect_operation_timeline" and tool["status"] == "ok" for tool in response["result"]["llm"]["tool_trace"])
        (folder / "report.md").write_text(generate_report(app.store.history(response["session_id"])), encoding="utf-8")
        print(json.dumps({"session_id": response["session_id"], "reply": response["reply"]}, ensure_ascii=False), flush=True)
    print(json.dumps({"passed": True, "folder": str(folder), "ocr_events": operations["event_count"], "issue_time": issue["time_label"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
