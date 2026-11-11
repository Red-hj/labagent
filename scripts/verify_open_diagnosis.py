"""Explicit live open-network test; synthetic materials, real API/tool calls."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from datetime import datetime, timezone, timedelta
import json
from labagent.model import load_env, configured
from labagent.workflow import available, run_workflow
from labagent.store import Store
from labagent.report import generate_report


def main():
    load_env(ROOT / ".env")
    if not configured() or not available():
        raise SystemExit("Configure model and install workflow dependencies first.")
    folder = ROOT / "runtime/open-verification" / datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    store = Store(folder / "sessions.sqlite3")
    stages = json.loads((ROOT / "data/scenarios/open_network.json").read_text(encoding="utf-8"))["stages"]
    session = None
    summary = {"passed": False, "note": "合成网络材料；真实模型、材料读取与引用校验；未验证真实根因。", "stages": []}
    for number, payload in enumerate(stages, 1):
        if session:
            payload["session_id"] = session
        print(f"Open investigation stage {number}/2...", flush=True)
        result = store.save(payload, run_workflow(payload, folder / "checkpoints.sqlite3"))
        session = result["session_id"]
        model = result["llm"]
        hypotheses = result["investigation"].get("hypotheses", [])
        passed = (model["status"] == "ok" and bool(model.get("calls")) and bool(hypotheses)
                  and all(item["state"] == "unverified" and item["citation_check"] == "exact_seen_lines" for item in hypotheses)
                  and bool(result["investigation"].get("evidence_requests")) and result["status"] == "needs_information"
                  and result["findings"] == [] and result["workflow"]["paused"] and result["workflow"]["resumed"] == (number > 1))
        entry = {"stage": number, "model_status": model["status"], "hypotheses": len(hypotheses), "passed": passed}
        summary["stages"].append(entry)
        history = store.history(session)
        (folder / "record.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "report.md").write_text(generate_report(history), encoding="utf-8")
        (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(entry), flush=True)
        if not passed:
            break
    summary["passed"] = len(summary["stages"]) == 2 and all(item["passed"] for item in summary["stages"])
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Artifacts: " + str(folder), flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
