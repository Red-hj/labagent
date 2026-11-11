"""Run the 3-stage synthetic scenario and save the actual execution report."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import json
from labagent.workflow import available, run_workflow
from labagent.store import Store
from labagent.report import generate_report


def main():
    if not available():
        raise SystemExit("Run setup.cmd first, then use .venv/Scripts/python.exe")
    bundle = json.loads((ROOT / "data/scenarios/three_routers.json").read_text(encoding="utf-8"))
    folder = ROOT / "runtime" / "workflow-demo"
    folder.mkdir(parents=True, exist_ok=True)
    store = Store(folder / "sessions.sqlite3")
    session = None
    for payload in bundle["stages"]:
        if session:
            payload["session_id"] = session
        result = store.save(payload, run_workflow(payload, folder / "checkpoints.sqlite3"))
        session = result["session_id"]
        print(result["status"], "paused=" + str(result["workflow"]["paused"]), "resumed=" + str(result["workflow"]["resumed"]))
    history = store.history(session)
    (folder / "record.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    (folder / "report.md").write_text(generate_report(history), encoding="utf-8")
    print(folder / "report.md")


if __name__ == "__main__":
    main()
