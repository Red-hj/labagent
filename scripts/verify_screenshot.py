"""Exercise actual screenshot OCR, coordinates and an optional model diagnosis."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.recordings import Recordings
from labagent.investigation import source_bundle
from labagent.operation_history import inspect_history
from labagent.material_service import MaterialService
from labagent.model import load_env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    folder = ROOT / "runtime/screenshot-verification" / time.strftime("%Y%m%d-%H%M%S")
    service = Recordings(folder / "recordings")
    sample = ROOT / "data/examples/终端报错截图.png"
    with sample.open("rb") as stream:
        job = service.upload(stream, sample.stat().st_size, sample.name)
    for _ in range(120):
        status = service.status(job["id"])
        if status["status"] == "error":
            raise RuntimeError(status["message"])
        if status["status"] == "ready":
            break
        time.sleep(1)
    else:
        raise RuntimeError("Screenshot OCR timed out")
    materials = {status["material_name"]: status["transcript"]}
    sources = source_bundle({"configs":materials})[0]
    operations = inspect_history(sources)
    issue = next(event for event in operations["events"] if event["status"] == "error")
    assert issue["device"] == "R2" and issue["view"] == "interface"
    assert issue["seconds"] is None
    for region in status["regions"]:
        assert sources["config:1"]["lines"][region["line"]-1] == region["text"]
        x1,y1,x2,y2 = region["box"]
        assert 0 <= x1 < x2 <= status["width"] and 0 <= y1 < y2 <= status["height"]
    (folder / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.live_model:
        app = MaterialService(folder / "sessions.sqlite3")
        result = app.invoke({"materials":materials, "question":"为什么截图中network命令报错？给我接着操作的步骤。", "recordings":[job["id"]], "evidence_kind":"demo"})
        (folder / "diagnosis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        assert result["status"] == "success", result["reply"]
        print(result["reply"], flush=True)
    print(json.dumps({"passed":True,"lines":len(status["regions"]),"events":operations["event_count"],"folder":str(folder)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
