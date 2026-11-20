"""Account-scoped, temporary outbound connections to a student's MCP client.

Only observation requests and their results cross this channel. Pairing grants no
account/session access. Connections and unfinished requests expire on restart.
"""
import copy
from contextlib import contextmanager
import re
import secrets
import threading
import time

from .mcp_client import MCPError


class LabBridge:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.condition = threading.Condition()
        self.connections = {}
        self.runtime_grants = {}

    def _drop(self, bridge_id):
        bridge = self.connections.pop(bridge_id)
        for token, grant in list(self.runtime_grants.items()):
            if grant["bridge_id"] == bridge_id:
                self.runtime_grants.pop(token)
        for job in bridge["jobs"].values():
            job["error"] = "实验连接已断开，请重新连接后手动采集"
            job["event"].set()
        self.condition.notify_all()

    def _expire(self):
        for bridge_id, bridge in list(self.connections.items()):
            if bridge["expires"] <= self.clock():
                self._drop(bridge_id)

    def create(self, owner, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise MCPError("请填写实验电脑名称（最多60字）")
        with self.condition:
            self._expire()
            # One active workstation per account keeps the destination unambiguous.
            for bridge_id, bridge in list(self.connections.items()):
                if bridge["owner"] == owner:
                    self._drop(bridge_id)
            bridge_id, code = secrets.token_hex(16), secrets.token_hex(8).upper()
            self.connections[bridge_id] = {"owner": owner, "name": name.strip(),
                "code": code, "expires": self.clock() + 300, "token": None,
                "last_seen": 0, "tools": [], "jobs": {}}
            return {"id": bridge_id, "code": code, "expires_in": 300}

    @staticmethod
    def _catalog(tools):
        if not isinstance(tools, list) or not 1 <= len(tools) <= 30:
            raise MCPError("请在本地配置1～30个可用的设备检查")
        result, names = [], set()
        for item in tools:
            fn = item.get("function", {}) if isinstance(item, dict) else {}
            if not isinstance(fn, dict):
                raise MCPError("本地检查工具格式无效")
            name, description, schema = fn.get("name"), fn.get("description"), fn.get("parameters", {})
            if not isinstance(name, str) or not re.fullmatch(r"lab_[a-z0-9_]{1,60}", name) or name in names:
                raise MCPError("本地检查工具名称无效或重复")
            if not isinstance(description, str) or not 1 <= len(description) <= 600 or not isinstance(schema, dict):
                raise MCPError("本地检查工具说明无效")
            properties = schema.get("properties")
            if schema.get("type") != "object" or schema.get("additionalProperties") is not False or not isinstance(properties, dict) or len(properties) > 5:
                raise MCPError("本地检查须使用固定选项")
            if not isinstance(schema.get("required"), list) or not all(isinstance(k, str) for k in schema["required"]) or set(schema["required"]) != set(properties):
                raise MCPError("本地检查参数须明确列出")
            choices = {}
            for key, value in properties.items():
                values = value.get("enum") if isinstance(value, dict) else None
                if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,39}", key) or value.get("type") != "string" or not isinstance(values, list) or not 1 <= len(values) <= 30 or not all(isinstance(v, str) and len(v) <= 500 for v in values):
                    raise MCPError("本地检查只接受明确的文本选项")
                choices[key] = {"type": "string", "enum": list(values)}
            result.append({"type": "function", "function": {"name": name, "description": description,
                "parameters": {"type": "object", "properties": choices, "required": list(choices), "additionalProperties": False}}})
            names.add(name)
        return result

    def claim(self, code, tools):
        if not isinstance(code, str) or not re.fullmatch(r"[A-Fa-f0-9]{16}", code):
            raise MCPError("连接码格式不正确，请从网站复制完整连接码")
        catalog = self._catalog(tools)
        with self.condition:
            self._expire()
            bridge = next((b for b in self.connections.values() if b["code"] and secrets.compare_digest(b["code"], code.upper())), None)
            if not bridge:
                raise MCPError("连接码无效、已使用或已过期，请在网站重新生成")
            bridge.update(code=None, tools=catalog, token=secrets.token_urlsafe(32),
                          expires=self.clock() + 8 * 3600, last_seen=self.clock())
            return {"token": bridge["token"], "expires_in": 8 * 3600}

    def _agent(self, token):
        self._expire()
        if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{40,60}", token):
            for bridge in self.connections.values():
                if bridge["token"] and secrets.compare_digest(bridge["token"], token):
                    return bridge
        raise PermissionError("本地连接已失效，请在网站重新配对")

    def status(self, owner):
        with self.condition:
            self._expire()
            for bridge_id, bridge in self.connections.items():
                if bridge["owner"] == owner:
                    return {"id": bridge_id, "name": bridge["name"],
                        "state": "waiting" if bridge["code"] else "online" if self.clock() - bridge["last_seen"] < 35 else "offline",
                        "tools": copy.deepcopy(bridge["tools"])}
            return None

    @contextmanager
    def runtime_connection(self, owner, base_url):
        """Issue one diagnosis a temporary view of its account's workstation."""
        token, connection = None, None
        with self.condition:
            bridge = self.status(owner) if owner and base_url else None
            if bridge and bridge["state"] == "online":
                token = secrets.token_urlsafe(32)
                self.runtime_grants[token] = {"owner": owner, "bridge_id": bridge["id"],
                    "expires": self.clock() + 600}
                connection = {"url": base_url.rstrip("/") + "/api/lab/runtime/collect",
                    "token": token, "tools": bridge["tools"]}
        try:
            yield connection
        finally:
            if token:
                with self.condition:
                    self.runtime_grants.pop(token, None)

    def runtime_collect(self, token, tool, arguments):
        with self.condition:
            self._expire()
            grant = self.runtime_grants.get(token) if isinstance(token, str) else None
            if not grant or grant["expires"] <= self.clock():
                raise PermissionError("本轮云端实验连接已结束，请在网站重新发起请求")
            owner, bridge_id = grant["owner"], grant["bridge_id"]
        return self.collect(owner, bridge_id, tool, arguments)

    def disconnect(self, owner, bridge_id):
        with self.condition:
            if not isinstance(bridge_id, str):
                raise MCPError("实验电脑编号无效")
            bridge = self.connections.get(bridge_id)
            if not bridge or bridge["owner"] != owner:
                raise MCPError("这台实验电脑未连接到当前账号")
            self._drop(bridge_id)

    def poll(self, token, *, wait=15):
        with self.condition:
            bridge = self._agent(token)
            bridge["last_seen"] = self.clock()
            self.condition.wait_for(lambda: any(not j["claimed"] for j in bridge["jobs"].values()) or bridge not in self.connections.values(), timeout=wait)
            self._agent(token)  # Recheck disconnect/expiry while the poll was waiting.
            bridge["last_seen"] = self.clock()
            for job_id, job in bridge["jobs"].items():
                if not job["claimed"]:
                    job["claimed"] = True
                    return {"id": job_id, "tool": job["tool"], "arguments": copy.deepcopy(job["arguments"])}
            return None

    def finish(self, token, job_id, result=None, error=None):
        with self.condition:
            bridge = self._agent(token)
            if not isinstance(job_id, str):
                raise MCPError("采集编号无效")
            job = bridge["jobs"].get(job_id)
            if not job or not job["claimed"] or job["event"].is_set():
                raise MCPError("本次采集已结束或超时，未将迟到结果加入材料")
            if error is not None:
                # Local exceptions/credentials are never returned to the browser.
                job["error"] = "本地设备检查失败，请查看连接程序提示，再手动重试"
            else:
                if not isinstance(result, dict) or not all(isinstance(result.get(k), str) and 0 < len(result[k]) <= limit for k, limit in (("device", 120), ("command", 500), ("text", 100_000))):
                    raise MCPError("本地输出格式无效或过大")
                job["result"] = {k: result[k] for k in ("device", "command", "text")}
            bridge["last_seen"] = self.clock()
            job["event"].set()

    def collect(self, owner, bridge_id, tool, arguments, *, timeout=30):
        with self.condition:
            self._expire()
            if not isinstance(bridge_id, str):
                raise MCPError("实验电脑编号无效")
            bridge = self.connections.get(bridge_id)
            if not bridge or bridge["owner"] != owner:
                raise MCPError("这台实验电脑未连接到当前账号")
            if not bridge["token"] or self.clock() - bridge["last_seen"] >= 35:
                raise MCPError("实验电脑未连接，请先启动本地连接程序")
            fn = next((t["function"] for t in bridge["tools"] if t["function"]["name"] == tool), None)
            choices = fn["parameters"]["properties"] if fn else None
            if choices is None or not isinstance(arguments, dict) or set(arguments) != set(choices) or any(not isinstance(v, str) or v not in choices[k]["enum"] for k, v in arguments.items()):
                raise MCPError("请选择这台电脑已开放的检查选项")
            if bridge["jobs"]:
                raise MCPError("这台实验电脑正在采集，请等本次完成")
            job_id = secrets.token_hex(16)
            job = {"tool": tool, "arguments": copy.deepcopy(arguments), "claimed": False, "event": threading.Event()}
            bridge["jobs"][job_id] = job
            self.condition.notify_all()
        try:
            if not job["event"].wait(timeout):
                raise MCPError("等待实验电脑超时，未自动重复检查；请确认连接后手动重试")
            if job.get("error"):
                raise MCPError(job["error"])
            return job["result"]
        finally:
            with self.condition:
                bridge["jobs"].pop(job_id, None)
