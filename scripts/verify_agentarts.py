"""Exercise real AgentArts SDK routes locally; optionally call the live model."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import argparse
from datetime import datetime
import importlib.metadata
import json

from starlette.testclient import TestClient
from agentarts.sdk.runtime.model import SESSION_HEADER
from labagent.agentarts_runtime import create_app
from labagent.model import load_env, configured
from labagent.report import generate_report


def main():
    parser = argparse.ArgumentParser(description="Verify local AgentArts SDK HTTP routes and material sessions")
    parser.add_argument("--live-model", action="store_true", help="Call the configured model with synthetic materials")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    if args.live_model and not configured():
        parser.error("Configure the model in the project .env first")
    folder = ROOT / "runtime/agentarts-verification" / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True)
    app = create_app(folder / "sessions.sqlite3")
    material = ROOT / "data/examples/实验诊断材料_终端跨网段访问.txt"
    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
        request = {"materials": {material.name: material.read_text(encoding="utf-8")},
                   "question": "PC1获取地址后为何访问不了10.30.0.10？", "use_llm": args.live_model, "evidence_kind": "demo"}
        first_response = client.post("/invocations", json=request)
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        session_id = first["session_id"]
        assert first_response.headers[SESSION_HEADER] == session_id
        second_response = client.post("/invocations", json={"message": "为什么修改地址池后还需要重新获取租约？用一句话回答。"}, headers={SESSION_HEADER: session_id})
        assert second_response.status_code == 200, second_response.text
        second = second_response.json()
        assert second["session_id"] == session_id
        history = app.material_service.store.history(session_id)
        assert len(history["attempts"]) == 2
        if args.live_model:
            assert first["status"] == second["status"] == "success"
            assert len(second["result"]["conversation"]) == 2
        for name, result in (("initial", first), ("followup", second)):
            (folder / (name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "report.md").write_text(generate_report(history), encoding="utf-8")
    summary = {"passed": True, "sdk_version": importlib.metadata.version("agentarts-sdk"),
               "transport": "official_sdk_asgi", "live_model": args.live_model, "materials": "synthetic",
               "session_id": session_id, "attempts": len(history["attempts"]),
               "model_statuses": [first["result"]["llm"]["status"], second["result"]["llm"]["status"]]}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    print("Artifacts: " + str(folder), flush=True)


if __name__ == "__main__":
    main()
