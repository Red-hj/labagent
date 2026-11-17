"""Material-oriented session API shared by platform entrypoints."""
from copy import deepcopy
from pathlib import Path
import re
import threading

from .engine import validate
from .experiment_goals import validate_goal_fields
from .session_state import RESOLVED_OUTCOMES
from .store import Store
from .storage import data_directory
from .knowledge import validate_course_documents
from .workflow import run_open_workflow

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUESTION = "请分析上传的实验材料，说明问题原因与下一步操作。"


class SessionNotFound(ValueError):
    pass


def validate_resume(attempt):
    """Check the portable context sent by the web server before starting a graph."""
    if not isinstance(attempt, dict) or not isinstance(attempt.get("input"), dict) or not isinstance(attempt.get("result"), dict):
        raise ValueError("resume_from须包含上一轮input和result")
    validate(attempt["input"])
    result = attempt["result"]
    if attempt["input"].get("diagnostic_mode") != "open_network" or not isinstance(result.get("investigation"), dict):
        raise ValueError("只能恢复开放排查的上下文")
    analysis = result["investigation"]
    if any(not isinstance(analysis.get(key, ""), str) for key in
           ("summary", "experiment_goal", "outcome", "resolution_summary")):
        raise ValueError("恢复诊断的摘要、目标和状态须为文本")
    hypotheses = analysis.get("hypotheses", [])
    if not isinstance(hypotheses, list) or not all(isinstance(item, dict) and isinstance(item.get("title"), str) for item in hypotheses):
        raise ValueError("恢复诊断的假设须包含标题")
    for key in ("pending_requests", "evidence_requests"):
        requests = analysis.get(key, [])
        if not isinstance(requests, list) or not all(isinstance(item, dict) and all(
                isinstance(item.get(field), str) for field in ("device", "information", "why")) for item in requests):
            raise ValueError("恢复诊断的材料请求格式无效")
    targets = analysis.get("verification_targets", [])
    if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
        raise ValueError("恢复诊断的复测目标须为文本列表")
    if analysis.get("outcome") in RESOLVED_OUTCOMES and not isinstance(analysis.get("verification"), dict):
        raise ValueError("恢复已完成诊断时须保留恢复依据")
    assessment = analysis.get("goal_assessment", {})
    if not isinstance(assessment, dict) or not isinstance(assessment.get("criteria", []), list) or len(assessment.get("criteria", [])) > 8:
        raise ValueError("恢复的实验目标须为最多8项有效目标")
    for goal in assessment.get("criteria", []):
        validate_goal_fields(goal)
    conversation = result.get("conversation", [])
    if not isinstance(conversation, list) or len(conversation) > 8 or not all(
            isinstance(turn, dict) and all(isinstance(turn.get(key), str) for key in ("intent", "user", "assistant", "created_at"))
            for turn in conversation):
        raise ValueError("恢复交流记录须为最近8轮有效对话")


class MaterialService:
    def __init__(self, database=None, completion=None):
        self.store = Store(database or data_directory() / "agentarts-sessions.sqlite3")
        self.workflow_database = self.store.path.with_name("workflow-" + self.store.path.name)
        self.completion = completion
        self.locks = [threading.Lock() for _ in range(32)]

    def invoke(self, request):
        if not isinstance(request, dict):
            raise ValueError("请求须为JSON对象")
        session_id = request.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id)):
            raise ValueError("请使用返回的32位session_id继续排查")
        restored = request.get("resume_from")
        if "resume_from" in request:
            if session_id:
                raise ValueError("恢复上下文时请省略旧的session_id")
            validate_resume(restored)
        with self.locks[hash(session_id or "new") % len(self.locks)]:
            history = self.store.history(session_id) if session_id else None
            if session_id and not history:
                raise SessionNotFound("会话不存在，请提交材料或恢复上下文后继续排查")
            previous = history["attempts"][-1] if history else restored
            payload = deepcopy(previous["input"]) if previous else {
                "diagnostic_mode": "open_network", "question": DEFAULT_QUESTION,
                "use_llm": True, "configs": {}, "evidence_kind": "user_submitted"}
            payload.pop("session_id", None)
            payload.pop("interaction", None)
            if request.get("knowledge_scope", payload.get("knowledge_scope", "default")) != payload.get("knowledge_scope", "default"):
                payload.pop("course_documents", None)
            materials = request.get("materials", {})
            if not isinstance(materials, dict):
                raise ValueError("materials须为材料名称与正文组成的对象")
            payload["configs"].update(materials)
            for key in ("question", "use_llm", "evidence_kind", "knowledge_scope", "recordings", "requirements", "topology", "command_outputs", "hint_level"):
                if key in request:
                    payload[key] = request[key]
            if "course_documents" in request:
                payload["course_documents"] = validate_course_documents(
                    request["course_documents"], payload.get("knowledge_scope", "default"))
            if "personal_memory" in request:
                from .personal import validate_memory_context
                payload["personal_memory"] = validate_memory_context(request["personal_memory"])
            action, message = request.get("action"), request.get("message")
            if action is not None or message is not None:
                if message is None:
                    message = {"ask": "解释当前问题的原因。", "repair": "给出具体修改步骤。", "update": "分析本轮补充的材料。", "verify": "比较材料改动并核对本轮测试结果。"}.get(action, "")
                payload["interaction"] = {"type": action or "ask", "message": message}
            if session_id:
                payload["session_id"] = session_id
            validate(payload)
            result = self.store.save(payload, run_open_workflow(payload, self.workflow_database, self.completion,
                                                               previous_attempt=previous))
            model = result["llm"]
            return {"status": "success" if model["status"] == "ok" else model["status"],
                    "session_id": result["session_id"],
                    "session_recovered": restored is not None,
                    "personal_memory_applied": True,
                    "course_document_ids": sorted(document["id"] for document in payload.get("course_documents", [])),
                    "reply": result["investigation"].get("reply") or model.get("message") or result["hint"],
                    "result": result}
