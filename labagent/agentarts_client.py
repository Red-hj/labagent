"""HTTP client for local SDK and deployed AgentArts runtimes."""
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

SESSION_HEADER = "x-hw-agentarts-session-id"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("调用地址发生重定向，请使用运行时详情页的完整调用地址")


class AgentArtsClient:
    def __init__(self, url, api_key="", platform_session_id=None, timeout=300, *,
                 access_key="", secret_key="", security_token="", region="cn-southwest-2"):
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("请填写 HTTPS 云端调用地址，或本机 HTTP SDK 地址")
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("请使用运行时详情页的完整调用地址")
        self.url, self.api_key, self.timeout = url, api_key, timeout
        if bool(access_key) != bool(secret_key):
            raise ValueError("IAM调用需要同时配置华为云AK和SK")
        if api_key and access_key:
            raise ValueError("请选择IAM或API Key中的一种认证方式")
        self.iam_signer = None
        self.security_token = security_token
        if access_key:
            sdk_path = Path(__file__).resolve().parents[1] / "runtime/agentarts-signing-sdk"
            if str(sdk_path) not in sys.path:
                sys.path.insert(0, str(sdk_path))
            try:
                from apig_sdk import signer
            except ImportError:
                raise ValueError("请先运行 python scripts/setup_agentarts_iam.py 安装华为官方签名SDK") from None
            self.iam_module = signer
            self.iam_signer = signer.Signer(algorithm="V11-HMAC-SHA256", region_id=region)
            self.iam_signer.Key, self.iam_signer.Secret = access_key, secret_key
        self.platform_session_id = platform_session_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.platform_session_id):
            raise ValueError("平台会话编号须为1～64位字母、数字、短横线或下划线")

    def invoke(self, payload):
        headers = {"Content-Type": "application/json", SESSION_HEADER: self.platform_session_id}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        if self.iam_signer:
            headers["X-Sdk-Content-Sha256"] = "UNSIGNED-PAYLOAD"
            if self.security_token:
                headers["X-Security-Token"] = self.security_token
            signed = self.iam_module.HttpRequest("POST", self.url, headers, body.decode("utf-8"))
            # AgentArts data-plane SDK signs an empty query line; the gateway
            # may rewrite queries. Keep the original URL on the HTTP request.
            signed.query = {}
            self.iam_signer.Sign(signed)
            headers = signed.headers
        request = Request(self.url, data=body, headers=headers, method="POST")
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
