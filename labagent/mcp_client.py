"""MCP clients for operator-configured network-lab servers.

Only the operator's configuration can start a program or choose its arguments.
Models and HTTP requests cannot supply an executable, URL, or device credentials.
"""
from contextlib import AbstractContextManager
import json
import os
from pathlib import Path
from queue import Queue, Empty
import re
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


class MCPError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise MCPError("MCP 地址发生跳转，请配置最终服务地址")


class StdioMCPClient(AbstractContextManager):
    def __init__(self, settings, *, timeout=20):
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise MCPError("MCP 超时须在1～120秒以内")
        self.settings, self.timeout = settings, timeout
        self.process = None
        self.messages = Queue(maxsize=128)
        self.sequence = 0
        self.lock = threading.Lock()

    def __enter__(self):
        spec = self.settings
        if not isinstance(spec.get("command"), str) or not spec["command"]:
            raise MCPError("MCP 服务缺少启动命令")
        args = spec.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise MCPError("MCP args 必须是字符串数组")
        # Do not pass model/cloud credentials from the parent process to a server.
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"}}
        temporary = Path(__file__).resolve().parents[1] / "runtime" / "mcp"
        temporary.mkdir(parents=True, exist_ok=True)
        env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary), PYTHONDONTWRITEBYTECODE="1")
        for key, value in spec.get("env", {}).items():
            if not isinstance(value, str):
                raise MCPError("MCP 环境配置必须是文本")
            match = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*)\}", value)
            if match and match[1] not in os.environ:
                raise MCPError("MCP 所需环境配置尚未设置")
            env[key] = os.environ[match[1]] if match else value
        try:
            self.process = subprocess.Popen([spec["command"], *args], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=spec.get("cwd"), env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            result = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "LabAgent", "version": "1.0"}})
            if result.get("protocolVersion") not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
                raise MCPError("MCP 服务协议版本暂不兼容")
            self.notify("notifications/initialized")
            return self
        except Exception as error:
            self.close()
            if isinstance(error, MCPError):
                raise
            raise MCPError("MCP 服务未能启动，请检查本地安装和配置") from None

    def _read(self):
        try:
            while True:
                raw = self.process.stdout.readline(1_000_001)
                if not raw:
                    break
                if len(raw) > 1_000_000:
                    break
                self.messages.put_nowait(json.loads(raw))
        except Exception:
            pass
        finally:
            try:
                self.messages.put_nowait(None)
            except Exception:
                pass

    def _send(self, data):
        try:
            self.process.stdin.write((json.dumps(data, ensure_ascii=False) + "\n").encode())
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise MCPError("MCP 连接已关闭") from None

    def notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def request(self, method, params=None):
        with self.lock:
            self.sequence += 1
            request_id = self.sequence
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            deadline = time.monotonic() + self.timeout
            while True:
                if time.monotonic() >= deadline:
                    self.close()
                    raise MCPError("MCP 检查超时，未自动重复执行")
                try:
                    message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
                except Empty:
                    self.close()
                    raise MCPError("MCP 检查超时，未自动重复执行") from None
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise MCPError("MCP 服务未返回有效协议消息")
                if "method" in message:
                    if "id" in message:
                        response = {"result": {}} if message["method"] == "ping" else {
                            "error": {"code": -32601, "message": "Client capability not supported"}}
                        self._send({"jsonrpc": "2.0", "id": message["id"], **response})
                    continue
                if message.get("id") != request_id:
                    raise MCPError("MCP 响应与当前检查不匹配")
                if "error" in message:
                    raise MCPError("MCP 服务拒绝了本次检查，请核对工具及参数")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise MCPError("MCP 结果格式无效")
                return result

    def list_tools(self):
        tools, cursor, cursors = [], None, set()
        for _ in range(20):
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(page.get("tools"), list):
                raise MCPError("MCP 未返回工具清单")
            tools.extend(page["tools"])
            cursor = page.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in cursors:
                break
            cursors.add(cursor)
        raise MCPError("MCP 工具清单分页无效")

    def call_tool(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("resultType", "complete") != "complete":
            raise MCPError("MCP工具尚需额外输入，未将其视为完成的设备观测")
        if result.get("isError"):
            raise MCPError("实验工具执行失败，请检查设备是否启动及连接状态")
        content = result.get("content", [])
        if not isinstance(content, list):
            raise MCPError("MCP 工具正文格式无效")
        text = "\n".join(item["text"] for item in content if isinstance(item, dict)
                         and item.get("type") == "text" and isinstance(item.get("text"), str))
        if not text and isinstance(result.get("structuredContent"), dict):
            text = json.dumps(result["structuredContent"], ensure_ascii=False)
        if not text or len(text) > 100_000:
            raise MCPError("MCP 未返回可引用的文本输出，或输出超过100KB")
        return text

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()

    def __exit__(self, *args):
        self.close()


class HTTPMCPClient(StdioMCPClient):
    """Streamable HTTP JSON/SSE responses; credentials never follow redirects."""
    def __enter__(self):
        parsed = urlparse(self.settings.get("url", ""))
        if parsed.username or parsed.password or parsed.fragment or not parsed.netloc or not (
                parsed.scheme == "https" or parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise MCPError("远程MCP须使用HTTPS，本机服务可使用HTTP")
        self.url, self.session_id, self.protocol = self.settings["url"], None, "2025-06-18"
        self.headers = {}
        for key, value in self.settings.get("headers", {}).items():
            if not isinstance(value, str):
                raise MCPError("MCP请求头须为文本")
            variable = re.fullmatch(r"\$\{([A-Z_][A-Z0-9_]*)\}", value)
            if variable and variable[1] not in os.environ:
                raise MCPError("MCP认证环境配置尚未设置")
            self.headers[key] = os.environ[variable[1]] if variable else value
        # A workstation's system proxy must not receive local lab traffic.
        self.opener = build_opener(ProxyHandler({}), NoRedirect()) if parsed.hostname in {"localhost", "127.0.0.1", "::1"} else build_opener(NoRedirect())
        result = self.request("initialize", {"protocolVersion": self.protocol, "capabilities": {},
            "clientInfo": {"name": "LabAgent", "version": "1.0"}})
        self.protocol = result.get("protocolVersion")
        if self.protocol not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
            self.close()
            raise MCPError("MCP 服务协议版本暂不兼容")
        self.notify("notifications/initialized")
        return self

    def _exchange(self, value, *, notification=False):
        headers = {**self.headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.protocol}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = Request(self.url, data=json.dumps(value).encode(), headers=headers, method="POST")
        deadline = time.monotonic() + self.timeout
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.headers.get("Mcp-Session-Id"):
                    self.session_id = response.headers["Mcp-Session-Id"]
                if notification:
                    return {}
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raw = response.read(1_000_001)
                    if len(raw) > 1_000_000:
                        raise MCPError("MCP响应过大")
                    messages = [json.loads(raw)]
                else:
                    data, size, messages = [], 0, []
                    for raw in response:
                        size += len(raw)
                        if size > 1_000_000 or time.monotonic() > deadline:
                            raise MCPError("MCP响应超时或过大，未自动重复执行")
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                        elif not line and data:
                            message = json.loads("\n".join(data)); data = []
                            if "method" in message and "id" in message:
                                response_value = {"result": {}} if message["method"] == "ping" else {
                                    "error": {"code": -32601, "message": "Client capability not supported"}}
                                self._exchange({"jsonrpc": "2.0", "id": message["id"], **response_value}, notification=True)
                                continue
                            if message.get("id") == value["id"] and "method" not in message:
                                messages = [message]
                                break
                for message in messages:
                    if isinstance(message, dict) and message.get("jsonrpc") == "2.0" and message.get("id") == value["id"]:
                        if "error" in message:
                            raise MCPError("MCP服务拒绝了本次检查")
                        if isinstance(message.get("result"), dict):
                            return message["result"]
        except (HTTPError, URLError, TimeoutError, OSError):
            raise MCPError("未能完成MCP请求，请检查地址、认证和服务状态；未自动重放") from None
        except (UnicodeError, json.JSONDecodeError):
            raise MCPError("MCP服务未返回有效协议消息") from None
        raise MCPError("MCP响应与当前检查不匹配")

    def request(self, method, params=None):
        with self.lock:
            self.sequence += 1
            return self._exchange({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params or {}})

    def notify(self, method):
        self._exchange({"jsonrpc": "2.0", "method": method}, notification=True)

    def close(self):
        if getattr(self, "session_id", None):
            headers = {**self.headers, "Mcp-Session-Id": self.session_id, "MCP-Protocol-Version": self.protocol}
            self.session_id = None
            try:
                with self.opener.open(Request(self.url, headers=headers, method="DELETE"), timeout=min(self.timeout, 2)):
                    pass
            except (URLError, OSError, MCPError):
                pass


class LabMCPTools(AbstractContextManager):
    """Expose only selected observation tools with operator-bound device targets."""
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = Path(__file__).resolve().parents[1] / self.path
        self.clients, self.bindings, self.tools, self.warnings = {}, {}, [], []

    def __enter__(self):
        settings = json.loads(self.path.read_text(encoding="utf-8-sig"))
        servers = settings.get("servers", {})
        if not isinstance(servers, dict) or len(servers) > 5:
            raise MCPError("最多配置5个本地 MCP 服务")
        try:
            for server_id, spec in servers.items():
                spec = dict(spec)
                root = Path(__file__).resolve().parents[1]
                working = Path(spec.get("cwd") or root)
                spec["cwd"] = str(working if working.is_absolute() else root / working)
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,19}", server_id):
                    raise MCPError("MCP 服务编号格式无效")
                # An unavailable lab should not prevent analysis of uploaded materials.
                client = None
                try:
                    transport = spec.get("transport", "stdio")
                    if transport not in {"stdio", "streamable-http"}:
                        raise MCPError("请选择stdio或streamable-http传输")
                    client = (HTTPMCPClient if transport == "streamable-http" else StdioMCPClient)(spec, timeout=spec.get("timeout", 20))
                    client.__enter__()
                    available = {tool["name"] for tool in client.list_tools()}
                except MCPError:
                    if client:
                        client.close()
                    self.warnings.append(server_id + "：服务未连接，可继续上传材料排查")
                    continue
                self.clients[server_id] = client
                for alias, binding in spec.get("tools", {}).items():
                    if not re.fullmatch(r"[a-z][a-z0-9_]{0,24}", alias) or binding.get("effect") != "observe":
                        raise MCPError("本轮仅允许配置明确标为observe的检查工具")
                    if binding.get("name") not in available:
                        self.warnings.append(server_id + "：所选工具不存在，已跳过")
                        continue
                    name = "lab_" + server_id + "_" + alias
                    choices = binding.get("choices", {})
                    if not isinstance(choices, dict) or not all(isinstance(v, list) and v and
                            all(isinstance(item, str) and len(item) <= 500 for item in v) for v in choices.values()):
                        raise MCPError("MCP 可选参数须配置明确的文本选项")
                    if set(choices) & set(binding.get("arguments", {})):
                        raise MCPError("MCP 固定参数不能被模型覆盖")
                    self.bindings[name] = (server_id, binding)
                    self.tools.append({"type": "function", "function": {"name": name,
                        "description": binding.get("description", "读取已连接实验设备的实际输出")[:600],
                        "parameters": {"type": "object", "properties": {key: {"type": "string", "enum": values}
                            for key, values in choices.items()}, "required": list(choices), "additionalProperties": False}}})
            return self
        except Exception:
            self.__exit__()
            raise

    def execute(self, name, arguments):
        server_id, binding = self.bindings[name]
        choices = binding.get("choices", {})
        if not isinstance(arguments, dict) or set(arguments) != set(choices) or any(
                value not in choices[key] for key, value in arguments.items()):
            raise MCPError("请使用本实验已配置的检查选项")
        text = self.clients[server_id].call_tool(binding["name"], {**binding.get("arguments", {}), **arguments})
        return {"server": server_id, "device": binding.get("device", server_id),
                "command": arguments.get("command", binding.get("command", binding["name"])), "text": text}

    def __exit__(self, *args):
        for client in self.clients.values():
            client.close()
