"""Material-oriented session API shared by platform entrypoints."""
from copy import deepcopy
from pathlib import Path
import re
import threading

from .engine import validate
from .store import Store
from .workflow import run_open_workflow

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUESTION = "请分析上传的实验材料，说明问题原因与下一步操作。"


class MaterialService:
    def __init__(self, database=None, completion=None):
        self.store = Store(database or ROOT / "runtime/agentarts-sessions.sqlite3")
        self.workflow_database = self.store.path.with_name("workflow-" + self.store.path.name)
        self.completion = completion
        self.locks = [threading.Lock() for _ in range(32)]

    def invoke(self, request):
        if not isinstance(request, dict):
            raise ValueError("请求须为JSON对象")
        session_id = request.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id)):
            raise ValueError("请使用返回的32位session_id继续排查")
        with self.locks[hash(session_id or "new") % len(self.locks)]:
            history = self.store.history(session_id) if session_id else None
            if session_id and not history:
                raise ValueError("会话不存在，请提交材料开始排查")
            payload = deepcopy(history["attempts"][-1]["input"]) if history else {
                "diagnostic_mode": "open_network", "question": DEFAULT_QUESTION,
                "use_llm": True, "configs": {}, "evidence_kind": "user_submitted"}
            payload.pop("interaction", None)
            materials = request.get("materials", {})
            if not isinstance(materials, dict):
                raise ValueError("materials须为材料名称与正文组成的对象")
            payload["configs"].update(materials)
            for key in ("question", "use_llm", "evidence_kind", "knowledge_scope", "recordings"):
                if key in request:
                    payload[key] = request[key]
            action, message = request.get("action"), request.get("message")
            if action is not None or message is not None:
                if message is None:
                    message = {"ask": "解释当前问题的原因。", "repair": "给出具体修改步骤。", "update": "分析本轮补充的材料。", "verify": "比较材料改动并核对本轮测试结果。"}.get(action, "")
                payload["interaction"] = {"type": action or "ask", "message": message}
            if session_id:
                payload["session_id"] = session_id
            validate(payload)
            result = self.store.save(payload, run_open_workflow(payload, self.workflow_database, self.completion))
            model = result["llm"]
            return {"status": "success" if model["status"] == "ok" else model["status"],
                    "session_id": result["session_id"],
                    "reply": result["investigation"].get("reply") or model.get("message") or result["hint"],
                    "result": result}
