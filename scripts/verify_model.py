"""Explicit live test: three synthetic stages, real model and local tools.

Uses the existing project environment. This script never runs as part of tests.
No credentials, request headers, private reasoning or raw provider errors saved.
"""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import json
from datetime import datetime, timezone, timedelta
from labagent.model import load_env, configured, public_config
from labagent.workflow import available, run_workflow
from labagent.store import Store
from labagent.report import generate_report


def main():
    load_env(ROOT / ".env")
    if not configured() or not available():
        raise SystemExit("Configure .env and run setup.cmd first.")
    stamp = datetime.now(timezone(timedelta(hours=8)))
    folder = ROOT / "runtime" / "model-verification" / stamp.strftime("%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True)
    store = Store(folder / "sessions.sqlite3")
    stages = json.loads((ROOT / "data/scenarios/three_routers.json").read_text(encoding="utf-8"))["stages"]
    expected = ["fault_found", "fault_found", "verified_from_observations"]
    summary = {"created_at": stamp.isoformat(), "model_config": public_config(), "evidence_kind": "synthetic_network_data_live_model_calls",
               "note": "网络配置和运行输出为合成数据；模型接口与本地工具实际执行。不是实机准确率或 AgentArts 部署证明。", "stages": [], "passed": False}
    session = None
    for index, payload in enumerate(stages):
        payload["use_llm"] = True
        payload["hint_level"] = index + 1
        if session:
            payload["session_id"] = session
        print(f"Running stage {index + 1}/3 with real model...", flush=True)
        result = store.save(payload, run_workflow(payload, folder / "checkpoints.sqlite3"))
        session = result["session_id"]
        model = result.get("llm", {})
        planning = [step for step in result["workflow"]["steps"] if step["component"] == "检查规划"]
        entry = {"stage": index + 1, "hint_level": payload["hint_level"], "status": result["status"], "expected": expected[index], "model_status": model.get("status"),
                 "model_calls": model.get("calls", []), "error_code": model.get("error_code"),
                 "tools": [{key: item.get(key) for key in ("tool", "arguments", "status")} for item in model.get("tool_trace", [])],
                 "planning": planning, "paused": result["workflow"]["paused"], "resumed": result["workflow"]["resumed"]}
        entry["passed"] = (entry["status"] == expected[index] and model.get("status") == "ok" and model.get("evidence_tools_used") is True
                           and bool(model.get("calls")) and all(step["planner"] == "model" for step in planning)
                           and result["workflow"]["resumed"] == (index > 0) and result["workflow"]["paused"] == (index < 2))
        summary["stages"].append(entry)
        history = store.history(session)
        (folder / "record.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "report.md").write_text(generate_report(history), encoding="utf-8")
        (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"stage": index + 1, "status": entry["status"], "model_status": entry["model_status"], "tools": [item["tool"] for item in entry["tools"]], "passed": entry["passed"]}), flush=True)
        if not entry["passed"]:
            break
    summary["passed"] = len(summary["stages"]) == 3 and all(stage["passed"] for stage in summary["stages"])
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Artifacts: " + str(folder), flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
