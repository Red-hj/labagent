"""Forward web material sessions to the AgentArts runtime."""
from copy import deepcopy
import time
import uuid

from .agentarts_client import AgentArtsClient, AgentArtsError
from .progress import emit_progress


class CloudWorkflow:
    def __init__(self, store, client_factory=None, *, idle_timeout=300, max_age=3600, clock=time.time):
        self.store = store
        self.idle_timeout, self.max_age, self.clock = idle_timeout, max_age, clock
        self.client_factory = client_factory or (
            lambda session: AgentArtsClient.from_environment(platform_session_id=session))

    def run(self, payload):
        history = self.store.history(payload["session_id"]) if payload.get("session_id") else None
        previous = history["attempts"][-1]["result"].get("agentarts") if history else None
        if history and not previous:
            raise ValueError("这个会话使用本地引擎，请新建排查后使用华为云。")
        platform_id = previous["platform_session_id"] if previous else uuid.uuid4().hex
        started_at = self.clock()
        platform_started_at = previous.get("started_at", 0) if previous else started_at
        recovering = bool(previous) and (
            not platform_started_at or
            started_at - previous.get("last_activity_at", 0) >= max(1, self.idle_timeout - 30) or
            started_at - platform_started_at >= max(1, self.max_age - 300))
        request = {"materials": deepcopy(payload["configs"])}
        for key in ("question", "use_llm", "evidence_kind", "knowledge_scope", "recordings", "requirements", "topology", "command_outputs", "hint_level"):
            if key in payload:
                request[key] = deepcopy(payload[key])
        if previous:
            request["session_id"] = previous["session_id"]
        if interaction := payload.get("interaction"):
            request["message"] = interaction["message"]
            request["action"] = interaction["type"]
        emit_progress("cloud_connecting", "正在连接华为云诊断服务…")

        def recover():
            nonlocal platform_id, platform_started_at, recovering
            recovering = True
            platform_id, platform_started_at = uuid.uuid4().hex, self.clock()
            last = history["attempts"][-1]
            request.pop("session_id", None)
            request["resume_from"] = {"input": deepcopy(last["input"]), "result": {
                "investigation": deepcopy(last["result"].get("investigation", {})),
                "conversation": deepcopy(last["result"].get("conversation", []))}}
            emit_progress("cloud_recovering", "正在恢复此前的材料、实验目标和交流记录…")

        if recovering:
            recover()
        try:
            response = self.client_factory(platform_id).invoke(request)
        except AgentArtsError as error:
            # Only an explicit missing-session response proves no diagnosis ran.
            # Generic 500s and timeouts must not replay a possibly completed call.
            if not previous or recovering or error.http_status != 400 or error.code != "session_not_found":
                raise
            recover()
            response = self.client_factory(platform_id).invoke(request)
        if recovering and response.get("session_recovered") is not True:
            raise ValueError("云端镜像尚不支持会话恢复，请更新AgentArts镜像后重试；此前的网页记录已保留。")
        result = response.get("result")
        if not isinstance(result, dict) or not all(key in result for key in
                ("created_at", "config_fingerprint", "hint_level", "findings", "status")):
            raise ValueError("云端未返回完整诊断结果，请检查运行时镜像版本。")
        result = deepcopy(result)
        result["agentarts"] = {"platform_session_id": platform_id, "session_id": response["session_id"],
                              "started_at": platform_started_at, "last_activity_at": self.clock(),
                              "session_recovered": recovering}
        emit_progress("cloud_received", "已收到华为云诊断结果，正在整理本轮记录…")
        return result
