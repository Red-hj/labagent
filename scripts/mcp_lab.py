"""Inspect a configured MCP server or save its observation for ordinary upload."""
import argparse
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.mcp_client import LabMCPTools
from labagent.investigation import safe_text


def main():
    parser = argparse.ArgumentParser(description="Connect LabAgent to a local network-lab MCP server")
    parser.add_argument("config", type=Path)
    parser.add_argument("--tool")
    parser.add_argument("--arguments", default="{}")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.tool and (not args.output or not args.output.resolve().is_relative_to(ROOT / "runtime")):
        parser.error("采集结果请用--output保存到项目runtime目录")
    with LabMCPTools(args.config) as lab:
        if not args.tool:
            print(json.dumps({"tools": lab.tools, "warnings": lab.warnings}, ensure_ascii=False, indent=2))
            return
        if args.tool not in lab.bindings:
            parser.error("未开放此检查，请先查看工具清单")
        observed = lab.execute(args.tool, json.loads(args.arguments))
        text = "设备：" + observed["device"] + "\n命令：" + observed["command"] + "\n" + safe_text(observed["text"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print("已保存设备检查输出：" + str(args.output))


if __name__ == "__main__":
    main()
