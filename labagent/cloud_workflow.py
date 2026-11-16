"""Forward web material sessions to the AgentArts runtime."""
from copy import deepcopy
import uuid

from .agentarts_client import AgentArtsClient
from .progress import emit_progress


class CloudWorkflow:
    def __init__(self, store, client_factory=None):
        self.store = store
        self.client_factory = client_factory or (
            lambda session: AgentArtsClient.from_environment(platform_session_id=session))

    def run(self, payload):
        history = self.store.history(payload["session_id"]) if payload.get("session_id") else None
        previous = history["attempts"][-1]["result"].get("agentarts") if history else None
        if history and not previous:
            raise ValueError("这个会话使用本地引擎，请新建排查后使用华为云。")
        platform_id = previous["platform_session_id"] if previous else uuid.uuid4().hex
        request = {"materials": deepcopy(payload["configs"])}
        for key in ("question", "use_llm", "evidence_kind", "knowledge_scope", "recordings"):
            if key in payload:
                request[key] = deepcopy(payload[key])
        if previous:
            request["session_id"] = previous["session_id"]
        if interaction := payload.get("interaction"):
            request["message"] = interaction["message"]
            request["action"] = interaction["type"]
        emit_progress("cloud_connecting", "正在连接华为云诊断服务…")
        response = self.client_factory(platform_id).invoke(request)
        result = response.get("result")
        if not isinstance(result, dict) or not all(key in result for key in
                ("created_at", "config_fingerprint", "hint_level", "findings", "status")):
            raise ValueError("云端未返回完整诊断结果，请检查运行时镜像版本。")
        result = deepcopy(result)
        result["agentarts"] = {"platform_session_id": platform_id, "session_id": response["session_id"]}
        emit_progress("cloud_received", "已收到华为云诊断结果，正在整理本轮记录…")
        return result
