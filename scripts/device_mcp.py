"""Expose configured simulator consoles as an observation-only stdio MCP server."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.device_console import ConsoleError, load_devices, read_command


def serve(devices, incoming, outgoing):
    def send(request_id, **response):
        outgoing.write((json.dumps({"jsonrpc": "2.0", "id": request_id, **response}, ensure_ascii=False) + "\n").encode("utf-8"))
        outgoing.flush()

    ready = False
    for raw in incoming:
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError):
            send(None, error={"code": -32700, "message": "Invalid JSON"})
            continue
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            send(None, error={"code": -32600, "message": "Invalid request"})
            continue
        method, request_id = request["method"], request.get("id")
        if method == "notifications/initialized":
            ready = True
        elif request_id is None:
            continue
        elif method == "initialize":
            send(request_id, result={"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "labagent-device-console", "version": "1.0"}})
        elif method == "ping":
            send(request_id, result={})
        elif not ready:
            send(request_id, error={"code": -32000, "message": "Initialize first"})
        elif method == "tools/list":
            commands = list(dict.fromkeys(cmd for spec in devices.values() for cmd in spec["commands"]))
            send(request_id, result={"tools": [{"name": "device_exec",
                "description": "读取已配置的 Huawei VRP 实验设备控制台输出",
                "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
                "inputSchema": {"type": "object", "properties": {
                    "target": {"type": "string", "enum": list(devices)},
                    "command": {"type": "string", "enum": commands}},
                    "required": ["target", "command"], "additionalProperties": False}}]})
        elif method == "tools/call":
            params = request.get("params")
            args = params.get("arguments") if isinstance(params, dict) else None
            if not isinstance(params, dict) or params.get("name") != "device_exec" or not isinstance(args, dict) or set(args) != {"target", "command"} or not isinstance(args["target"], str) or args["target"] not in devices or not isinstance(args["command"], str):
                send(request_id, error={"code": -32602, "message": "Choose a configured device and command"})
                continue
            try:
                text = read_command(devices[args["target"]], args["command"])
                send(request_id, result={"content": [{"type": "text", "text": text}]})
            except ConsoleError as error:
                send(request_id, result={"isError": True, "content": [{"type": "text", "text": str(error)}]})
        else:
            send(request_id, error={"code": -32601, "message": "Method not found"})


def main():
    parser = argparse.ArgumentParser(description="LabAgent built-in Huawei VRP console MCP server")
    parser.add_argument("devices", type=Path)
    args = parser.parse_args()
    try:
        serve(load_devices(args.devices), sys.stdin.buffer, sys.stdout.buffer)
    except ConsoleError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
