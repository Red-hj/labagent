"""Independent MCP peer for synthetic replies and recorded lab output."""
import json
from pathlib import Path
import sys
import time

ready = False
phase = sys.argv[1] if len(sys.argv) > 1 else None
captures = Path(__file__).resolve().parents[2] / "data/team_cases/lab_02/map_materials"


def replay(device, command, capture_phase):
    raw = (captures / capture_phase / f"{device}-run.log").read_text(encoding="utf-8")
    if command.startswith("ping "):
        header = "PING " + command.rsplit(" ", 1)[-1]
        return header + raw.split("\n" + header, 1)[1]
    return raw.split(f"<{device}>{command}\n", 1)[1].split(f"\n<{device}>", 1)[0]


def send(value):
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method, request_id = message.get("method"), message.get("id")
    if method == "notifications/initialized":
        ready = True
        continue
    if method == "initialize":
        send({"id": request_id, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "synthetic-lab", "version": "1"}}})
    elif not ready:
        send({"id": request_id, "error": {"code": -32000, "message": "Initialize first"}})
    elif method == "tools/list":
        if message["params"].get("cursor"):
            send({"id": request_id, "result": {"tools": [{"name": "other_check", "inputSchema": {"type": "object"}}]}})
        else:
            send({"id": request_id, "result": {"tools": [{"name": "device_exec", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}, "command": {"type": "string"}}, "required": ["target", "command"]}}], "nextCursor": "second"}})
    elif method == "tools/call":
        arguments = message["params"]["arguments"]
        device, command = arguments.get("target"), arguments.get("command")
        if command == "fail":
            send({"id": request_id, "result": {"isError": True, "content": [{"type": "text", "text": "Device offline"}]}})
        elif command == "slow":
            time.sleep(2)
            send({"id": request_id, "result": {"content": [{"type": "text", "text": "late"}]}})
        elif phase in {"fault", "repaired"}:
            commands = {"R1": "ip route show table main", "R2": "ip route show table main",
                        "PC1": "ping -n -c 5 -i 0.2 -W 1 -w 8 192.168.20.10",
                        "PC2": "ping -n -c 5 -i 0.2 -W 1 -w 8 192.168.10.10"}
            if device not in commands or commands[device] != command:
                send({"id": request_id, "result": {"isError": True, "content": [{"type": "text", "text": "Capture unavailable"}]}})
                continue
            send({"id": request_id, "result": {"content": [{"type": "text", "text": replay(device, command, phase)}]}})
        elif command == "ip route show table main":
            # Replay the body returned by a collector that strips the CLI prompt.
            text = replay("R1", command, "repaired")
            send({"id": request_id, "result": {"content": [{"type": "text", "text": text}]}})
        else:
            send({"method": "notifications/message", "params": {"level": "info", "data": "Reading synthetic device"}})
            send({"id": "peer-ping", "method": "ping"})
            reply = json.loads(sys.stdin.readline())
            assert reply == {"jsonrpc": "2.0", "id": "peer-ping", "result": {}}
            send({"id": request_id, "result": {"content": [{"type": "text", "text": "<R1>display ip interface brief\nGE0/0/0 10.12.0.1/24 up up"}]}})
    else:
        send({"id": request_id, "error": {"code": -32601, "message": "Unknown method"}})
