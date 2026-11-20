"""AgentArts tool adapter for a diagnosis-scoped workstation connection."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, ProxyHandler

from .lab_bridge import LabBridge
from .mcp_client import MCPError, NoRedirect


class RelayLabTools:
    def __init__(self, connection):
        trusted = os.environ.get("LABAGENT_LAB_RELAY_URL", "")
        if not isinstance(connection, dict) or not trusted or connection.get("url") != trusted:
            raise MCPError("请在AgentArts运行时配置网站的LABAGENT_LAB_RELAY_URL")
        url = urlparse(trusted)
        if (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"})) or url.username or url.password or url.query or url.fragment:
            raise MCPError("实验回传地址须为网站的HTTPS接口，本机联调可用localhost")
        token = connection.get("token")
        if not isinstance(token, str) or not 40 <= len(token) <= 60 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in token):
            raise MCPError("云端实验连接凭据无效")
        self.url, self.token = trusted, token
        self.tools = LabBridge._catalog(connection.get("tools"))
        self.bindings = {tool["function"]["name"]: tool["function"]["parameters"]["properties"] for tool in self.tools}
        self.warnings = []

    def execute(self, name, arguments):
        choices = self.bindings.get(name)
        if choices is None or not isinstance(arguments, dict) or set(arguments) != set(choices) or any(
                not isinstance(value, str) or value not in choices[key]["enum"] for key, value in arguments.items()):
            raise MCPError("请选择这台电脑已开放的检查选项")
        request = Request(self.url, data=json.dumps({"tool": name, "arguments": arguments}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        try:
            # A device observation is sent once; redirects and HTTP failures do not replay it.
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=40) as response:
                data = response.read(500_001)
            if len(data) > 500_000:
                raise MCPError("实验电脑返回的输出过大")
            observed = json.loads(data)
            if not isinstance(observed, dict) or not all(isinstance(observed.get(key), str) and observed[key] for key in ("device", "command", "text")):
                raise MCPError("实验电脑返回的输出格式无效")
            return {key: observed[key] for key in ("device", "command", "text")}
        except (HTTPError, URLError, TimeoutError, OSError):
            raise MCPError("云端读取实验电脑失败，请检查连接后重新发起请求") from None
        except (json.JSONDecodeError, UnicodeError):
            raise MCPError("实验电脑返回的输出格式无效") from None
