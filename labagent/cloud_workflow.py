"""Forward web material sessions to the AgentArts runtime."""
from copy import deepcopy
import time
import uuid

from .agentarts_client import AgentArtsClient, AgentArtsError
from .knowledge import export_course_documents
from .progress import emit_progress


class CloudWorkflow:
    def __init__(self, store, client_factory=None, *, idle_timeout=300, max_age=3600, clock=time.time):
        self.store = store
        self.idle_timeout, self.max_age, self.clock = idle_timeout, max_age, clock
        self.client_factory = client_factory or (
            lambda session: AgentArtsClient.from_environment(platform_session_id=session))

    def run(self, payload, *, lab_connection=None, task_connection=None):
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
        request = {"materials": deepcopy(payload["configs"]), "material_mode": "replace"}
        request["course_documents"] = export_course_documents(payload.get("knowledge_scope", "default"))
        request["personal_memory"] = deepcopy(payload.get("personal_memory", []))
        if lab_connection:
            request["lab_connection"] = deepcopy(lab_connection)
        if task_connection:
            request["task_connection"] = deepcopy(task_connection)
        for key in ("question", "use_llm", "evidence_kind", "knowledge_scope", "recordings", "requirements", "topology", "command_outputs", "hint_level", "learning"):
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
        if lab_connection and response.get("lab_connection_applied") is not True:
            raise ValueError("云端镜像尚不支持实验电脑工具回传，请更新AgentArts运行时后继续。")
        if task_connection and response.get("task_control_applied") is not True:
            raise ValueError("请更新云端运行时以支持任务停止和恢复。")
        expected = sorted(document["id"] for document in request["course_documents"])
        if request["personal_memory"] and response.get("personal_memory_applied") is not True:
            raise ValueError("云端尚不支持个人记忆，请更新AgentArts镜像后继续；此前的网页记录已保留。")
        if expected and response.get("course_document_ids") != expected:
            raise ValueError("云端尚未接收本轮课程资料，请更新AgentArts镜像后重试；此前的网页记录已保留。")
        if response.get("materials_replaced") is not True:
            raise ValueError("云端尚未按当前材料列表同步，请更新AgentArts运行时后继续；当前材料和对话已保留。")
        result = response.get("result")
        if not isinstance(result, dict) or not all(key in result for key in
                ("created_at", "config_fingerprint", "hint_level", "findings", "status")):
            raise ValueError("云端未返回完整诊断结果，请检查运行时镜像版本。")
        result = deepcopy(result)
        if payload.get("learning") and result.get("llm", {}).get("status") != "cancelled" and not result.get("investigation", {}).get("learning"):
            model_status = result.get("llm", {}).get("status")
            if model_status and model_status != "ok":
                message = {
                    "step_limit": "本轮检查尚未收敛。请先聚焦一台设备或一项输出，再继续核对。",
                    "no_evidence": "本轮未生成有效的下一步引导，请重试本次请求。",
                    "not_configured": "云端模型尚未配置，请联系维护人员检查模型设置。",
                }.get(model_status, "模型暂时未能完成本轮引导，请重试本次请求。")
                raise ValueError(message + " 当前材料、实验目标和已保存的对话均保留。")
            raise ValueError("云端未返回约定的逐步引导结果，请联系维护人员检查运行时；当前材料和对话已保留。")
        result["agentarts"] = {"platform_session_id": platform_id, "session_id": response["session_id"],
                              "started_at": platform_started_at, "last_activity_at": self.clock(),
                              "session_recovered": recovering}
        emit_progress("cloud_received", "已收到华为云诊断结果，正在整理本轮记录…")
        return result
