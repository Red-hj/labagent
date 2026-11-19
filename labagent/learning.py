"""Progressive experiment support and short-lived access to worked solutions."""
import base64
import hashlib
import hmac
import json
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

COOKIE = "labagent_learning"
_solution_access = ContextVar("labagent_solution_access", default=False)


@contextmanager
def learning_access(payload):
    active = policy(payload)
    token = _solution_access.set(bool(active and active["level"] == 3))
    # Authorization lives in this execution context, never in graph checkpoints.
    if active:
        payload["learning"] = {"level": active["level"]}
    try:
        yield
    finally:
        _solution_access.reset(token)


def grant_solution(code, subject, *, now=None):
    expected = os.environ.get("LABAGENT_SOLUTION_CODE", "")
    key = os.environ.get("LABAGENT_LEARNING_KEY", "")
    if not expected or not key:
        raise ValueError("老师尚未开放完整解答，可继续使用逐步提示")
    if not isinstance(code, str) or not hmac.compare_digest(code.encode(), expected.encode()):
        raise ValueError("解答授权码不正确")
    body = base64.urlsafe_b64encode(json.dumps({"subject": subject,
        "expires": int(now if now is not None else time.time()) + 7200}, separators=(",", ":")).encode()).decode().rstrip("=")
    return body + "." + hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()


def solution_allowed(token, *, subject=None, now=None):
    key = os.environ.get("LABAGENT_LEARNING_KEY", "")
    if not key or not isinstance(token, str) or len(token) > 1000:
        return False
    try:
        body, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()):
            return False
        value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        remaining = value["expires"] - (now if now is not None else time.time())
        return 0 < remaining <= 7200 and (subject is None or value["subject"] == subject)
    except (ValueError, KeyError, TypeError):
        return False


def policy(payload):
    learning = payload.get("learning")
    if learning is None:
        return None
    if not isinstance(learning, dict) or set(learning) - {"level", "access_token"}:
        raise ValueError("learning只接受提示级别和解答授权")
    level = learning.get("level", 1)
    if type(level) is not int or level not in (1, 2, 3):
        raise ValueError("提示级别须为1、2或3")
    if level == 3 and not (_solution_access.get() or solution_allowed(learning.get("access_token"))):
        raise ValueError("完整解答需要老师授权，请先使用逐步提示")
    return {"level": level, "mode": "worked_solution" if level == 3 else "guided"}


STEP_TOOL = {"type": "function", "function": {"name": "submit_learning_step",
    "description": "提交本轮面向学生的一小步引导。实验可尚未完成或没有故障。只推进当前卡点，不倾倒整份答案。",
    "parameters": {"type": "object", "properties": {
        "stage": {"type": "string", "enum": ["orient", "try", "observe", "reflect", "completed"]},
        "current_step": {"type": "string"}, "explanation": {"type": "string"},
        "action": {"type": "string"}, "expected_observation": {"type": "string"},
        "question": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {
            "source_id": {"type": "string"}, "line": {"type": "integer"}, "quote": {"type": "string"}},
            "required": ["source_id", "line", "quote"], "additionalProperties": False}}},
        "required": ["stage", "current_step", "explanation", "action", "expected_observation", "question", "evidence"],
        "additionalProperties": False}}}

WRITE_COMMAND = re.compile(r"(?im)\b(?:undo\s+(?:shutdown|network|ip|ospf)\b|"
    r"ip\s+(?:address|route(?:-static)?)\s+\d|router\s+ospf\s+\d|ospf\s+\d|network\s+\d|no\s+shutdown\b)")


def instructions(active, previous=None):
    level = active["level"]
    return ("\n本轮为持续实验引导。学生可能刚开始、做到一半不会、命令报错或结果不符；不要要求先做完整个实验。"
        "用初学者能理解的说法，先解释术语再使用缩写。先结合本次提问和历史判断卡点；已有信息足够就给一步，不重复索要已有材料。没有设备材料时也可解释操作入口并询问当前画面。"
        "内部检查结论用于选提示，面向学生必须只调用submit_learning_step，禁止submit_hypotheses。"
        "先简短接住当前问题，再给一个小动作、需要观察什么，最后用一个具体问题收回学生结果。"
        "学生仍不懂时换一种解释或把动作拆小，不能原样重复；学生操作有进展时明确说明这一步有什么变化。"
        "不要根据‘做了’就判通过，也不要把一次Ping成功当全部实验完成；completed仅在原实验目标工具全部通过时使用。"
        + {1: " 当前为第1级：给检查方向、概念解释或一条只读观察命令；不直接点明最终故障答案、目标配置值或修复命令。",
           2: " 当前为第2级：详细解释当前一步的操作入口、命令视图及只读检查方法；可用留空模板让学生填写关键值，不直接给完整修复配置或替学生完成推理。",
           3: " 当前已有服务端解答授权：可以给当前卡点的明确答案和具体命令，同时解释依据和验证方法；仍按当前一步推进，不编造操作结果。"}[level]
        + " 上一轮学习进度：" + json.dumps((previous or {}).get("learning", {}), ensure_ascii=False))


def validate_step(args, active, cite, context):
    keys = {"stage", "current_step", "explanation", "action", "expected_observation", "question", "evidence"}
    if not isinstance(args, dict) or set(args) != keys:
        raise ValueError("请提交当前步骤、一个动作、预期观察和一个追问")
    if args["stage"] not in {"orient", "try", "observe", "reflect", "completed"}:
        raise ValueError("学习阶段无效")
    for key in keys - {"evidence", "stage"}:
        limits = {"current_step": 120, "explanation": 500, "action": 500, "expected_observation": 300, "question": 200}
        if not isinstance(args[key], str) or not args[key].strip() or len(args[key]) > limits[key]:
            raise ValueError("引导内容须简短且完整")
    if not isinstance(args["evidence"], list) or len(args["evidence"]) > 4:
        raise ValueError("每步最多4条材料依据")
    evidence = [cite(item) for item in args["evidence"]]
    if active["level"] < 3 and WRITE_COMMAND.search("\n".join(args[key] for key in keys - {"evidence"})):
        raise ValueError("当前为逐步提示，请改为只读检查或留空练习，不下发修复命令")
    assessment = context.get("goal_assessment", {})
    if args["stage"] == "completed" and assessment.get("status") != "passed":
        raise ValueError("还没有本轮实验目标全部通过的证据，请继续引导当前步骤")
    step = {**args, "evidence": evidence, **active}
    reply = "\n\n".join(args[key] for key in ("explanation", "action", "expected_observation", "question"))
    result = {"summary": args["current_step"], "reply": reply, "hypotheses": [], "evidence_requests": [],
              "steps": [args["action"]], "outcome": "investigating", "learning": step}
    if args["stage"] == "completed":
        result["outcome"] = "observations_passed"
        result["verification"] = {"source": "experiment_goal_checks", "targets": [], "pings": [], "evidence": evidence}
    return result
