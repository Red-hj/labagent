"""Run from any directory; every generated file stays under LabAgent."""
import sys
sys.dont_write_bytecode = True

import argparse
import json
from pathlib import Path

from labagent.engine import diagnose
from labagent.model import load_env

ROOT = Path(__file__).resolve().parent


def main():
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="LabAgent local OSPF assistant")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="Start local web UI and HTTP API")
    serve.add_argument("--port", type=int, default=8765)
    check = commands.add_parser("diagnose", help="Diagnose a JSON input without starting the server")
    check.add_argument("input", type=Path)
    check.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "diagnose":
        payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
        from labagent.workflow import run_workflow
        text = json.dumps(run_workflow(payload), ensure_ascii=False, indent=2)
        if args.output:
            target = args.output.resolve()
            if not target.is_relative_to(ROOT):
                parser.error("输出文件必须位于 LabAgent 项目文件夹内")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        else:
            print(text)
    else:
        from labagent.server import serve
        serve(getattr(args, "port", 8765))


if __name__ == "__main__":
    main()
