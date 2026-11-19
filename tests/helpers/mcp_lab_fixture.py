"""Independent synthetic MCP peer; no simulator or real device is contacted."""
import json
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
        else:
            send({"method": "notifications/message", "params": {"level": "info", "data": "Reading synthetic device"}})
            send({"id": "peer-ping", "method": "ping"})
            reply = json.loads(sys.stdin.readline())
            assert reply == {"jsonrpc": "2.0", "id": "peer-ping", "result": {}}
            send({"id": request_id, "result": {"content": [{"type": "text", "text": "<R1>display ip interface brief\nGE0/0/0 10.12.0.1/24 up up"}]}})
    else:
        send({"id": request_id, "error": {"code": -32601, "message": "Unknown method"}})
