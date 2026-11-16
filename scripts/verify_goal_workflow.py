"""Run the three-stage goals demo through the configured model and real tools."""
from pathlib import Path
import json
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from labagent.demos import ospf_goals_demo
from labagent.material_service import MaterialService
from labagent.model import load_env
from labagent.report import generate_report


def main():
    load_env(ROOT / ".env")
    output = ROOT / "runtime/goal-verification" / time.strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=True)
    service = MaterialService(output / "sessions.sqlite3")
    stages = ospf_goals_demo()["stages"]
    summary, session_id = [], None
    for index, stage in enumerate(stages):
        request = {"materials": stage["materials"], "evidence_kind": "demo"}
        if session_id:
            request.update(session_id=session_id, action="verify", message="已更新材料，请逐项核对实验目标，说明已经满足什么，还差什么。")
        else:
            request["question"] = "按实验说明排查R1到10.3.3.3的访问，选择最有价值的下一项检查，并核对OSPF要求。"
        start = time.monotonic()
        response = service.invoke(request)
        session_id = response["session_id"]
        result = response["result"]
        (output / f"stage-{index + 1}.json").write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
        row = {"stage": index + 1, "model_status": result["llm"]["status"], "status": result["status"],
               "seconds": round(time.monotonic() - start, 2), "reply": response["reply"],
               "goals": [{"kind": item["kind"], "status": item["status"]} for item in result["investigation"].get("goal_assessment", {}).get("checks", [])],
               "tools": [{"name": item["tool"], "status": item["status"]} for item in result["llm"]["tool_trace"]]}
        summary.append(row)
        (output / "summary.json").write_text(json.dumps({"session_id": session_id, "stages": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if response["status"] != "success":
            raise RuntimeError(f"Stage {index + 1}: {response['status']}; see {output}")
    history = service.store.history(session_id)
    (output / "report.md").write_text(generate_report(history), encoding="utf-8")
    assert summary[0]["goals"], "The model did not establish experimental goals"
    assert any(item["status"] == "failed" and item["kind"] == "route_protocol" for item in summary[1]["goals"])
    assert summary[1]["status"] != "verified_from_observations"
    assert summary[2]["status"] == "verified_from_observations"
    assert all(item["status"] == "passed" for item in summary[2]["goals"])
    print(f"Passed: {output}", flush=True)


if __name__ == "__main__":
    main()
