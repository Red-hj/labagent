"""Send materials and follow-up questions to a deployed LabAgent runtime."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import argparse
from datetime import datetime
import json
import os

from labagent.agentarts_client import AgentArtsClient
from labagent.model import load_env


def main():
    parser = argparse.ArgumentParser(description="上传材料或继续追问 AgentArts 上的 LabAgent")
    parser.add_argument("materials", nargs="*", type=Path)
    parser.add_argument("--question", help="首次诊断的问题")
    parser.add_argument("--message", help="本轮追问")
    parser.add_argument("--action", choices=("ask", "repair", "update", "verify"))
    parser.add_argument("--session", help="上次显示的平台会话编号")
    parser.add_argument("--url", help="完整调用地址，默认读取项目本地环境变量")
    parser.add_argument("--no-model", action="store_true", help="检查 HTTP 和会话协议")
    args = parser.parse_args()
    load_env(ROOT / ".env")
    load_env(ROOT / "runtime/agentarts-client.env")
    url = args.url or os.environ.get("LABAGENT_AGENTARTS_URL")
    if not url:
        parser.error("请在 runtime/agentarts-client.env 设置 LABAGENT_AGENTARTS_URL")
    payload = {}
    try:
        if args.materials:
            materials = {}
            for path in args.materials:
                if path.name in materials:
                    raise ValueError("材料文件名重复，请分别命名后提交")
                materials[path.name] = path.read_text(encoding="utf-8-sig")
            payload["materials"] = materials
        for key in ("question", "message", "action"):
            if getattr(args, key) is not None:
                payload[key] = getattr(args, key)
        if args.no_model:
            payload["use_llm"] = False
        client = AgentArtsClient.from_environment(url=url, platform_session_id=args.session)
        result = client.invoke(payload)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + "\n")
    folder = ROOT / "runtime/agentarts-client" / client.platform_session_id
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result.get("reply") or result["status"])
    print("平台会话：" + client.platform_session_id)
    print("记录：" + str(output))
    return 0 if result["status"] in ("success", "not_configured") else 1


if __name__ == "__main__":
    raise SystemExit(main())
