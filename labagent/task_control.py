"""Cooperative cancellation between model requests and device/tool calls."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler

from .mcp_client import NoRedirect

_control = ContextVar("labagent_task_control", default=None)


class DiagnosisCancelled(Exception):
    pass


@contextmanager
def task_control(check=None):
    token = _control.set(check)
    try:
        yield
    finally:
        _control.reset(token)


def check_cancelled():
    check = _control.get()
    if check and check():
        raise DiagnosisCancelled("已停止本轮检查，已取得的材料和记录会保留。")


class RuntimeTaskControl:
    def __init__(self, connection):
        relay = os.environ.get("LABAGENT_LAB_RELAY_URL", "").strip()
        parsed = urlparse(relay)
        expected = parsed._replace(path="/api/tasks/runtime/status", query="", fragment="").geturl()
        if not relay or not isinstance(connection, dict) or connection.get("url") != expected:
            raise ValueError("云端任务控制地址与现有网站回传配置不匹配")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
            raise ValueError("任务控制地址须为HTTPS或本机联调地址")
        token = connection.get("token")
        if not isinstance(token, str) or not 40 <= len(token) <= 60:
            raise ValueError("任务控制凭据无效")
        self.url, self.token = expected, token

    def __call__(self):
        request = Request(self.url, data=b'{}', headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
                state = json.loads(response.read(4096))
            if type(state.get("cancelled")) is not bool:
                raise ValueError("任务控制响应无效")
            return state["cancelled"]
        except (OSError, ValueError):
            # A disconnected controller must not permit additional device actions.
            raise DiagnosisCancelled("任务连接已中断，已停止后续检查。请回到网站查看记录。") from None
