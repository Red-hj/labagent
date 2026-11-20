"""Independent MCP peer for synthetic replies and recorded lab output."""
import json
from pathlib import Path
import sys
import time

ready = False


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
        command = message["params"]["arguments"].get("command")
        if command == "fail":
            send({"id": request_id, "result": {"isError": True, "content": [{"type": "text", "text": "Device offline"}]}})
        elif command == "slow":
            time.sleep(2)
            send({"id": request_id, "result": {"content": [{"type": "text", "text": "late"}]}})
        elif command == "ip route show table main":
            # Replay the body returned by a collector that strips the CLI prompt.
            capture = Path(__file__).resolve().parents[2] / "data/team_cases/lab_02/map_materials/repaired/R1-run.log"
            text = capture.read_text(encoding="utf-8").split("<R1>ip route show table main\n", 1)[1].split("\n<R1>", 1)[0]
            send({"id": request_id, "result": {"content": [{"type": "text", "text": text}]}})
        else:
            send({"method": "notifications/message", "params": {"level": "info", "data": "Reading synthetic device"}})
            send({"id": "peer-ping", "method": "ping"})
            reply = json.loads(sys.stdin.readline())
            assert reply == {"jsonrpc": "2.0", "id": "peer-ping", "result": {}}
            send({"id": request_id, "result": {"content": [{"type": "text", "text": "<R1>display ip interface brief\nGE0/0/0 10.12.0.1/24 up up"}]}})
    else:
        send({"id": request_id, "error": {"code": -32601, "message": "Unknown method"}})
