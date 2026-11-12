"""HTTP client for local SDK and deployed AgentArts runtimes."""
import json
import re
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

SESSION_HEADER = "x-hw-agentarts-session-id"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("调用地址发生重定向，请使用运行时详情页的完整调用地址")


class AgentArtsClient:
    def __init__(self, url, api_key="", platform_session_id=None, timeout=300):
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("请填写 HTTPS 云端调用地址，或本机 HTTP SDK 地址")
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("请使用运行时详情页的完整调用地址")
        self.url, self.api_key, self.timeout = url, api_key, timeout
        self.platform_session_id = platform_session_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.platform_session_id):
            raise ValueError("平台会话编号须为1～64位字母、数字、短横线或下划线")

    def invoke(self, payload):
        headers = {"Content-Type": "application/json", SESSION_HEADER: self.platform_session_id}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = Request(self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with build_opener(NoRedirect()).open(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            raise ValueError(f"运行时返回 HTTP {error.code}，请检查调用地址、入站认证和请求材料") from None
        except (URLError, TimeoutError):
            raise ValueError("运行时连接失败或响应超时，请检查服务状态和网络") from None
        if not isinstance(result, dict) or not result.get("session_id") or not result.get("status"):
            raise ValueError("运行时未返回 LabAgent 诊断结果，请检查所调用的镜像版本")
        return result
