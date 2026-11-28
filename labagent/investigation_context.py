"""Prepare bounded, cited investigation inputs without executing tools or models."""
import json
import re

from .knowledge import validate_scope, validate_course_documents
from .personal import validate_memory_context
from .learning import policy as learning_policy, STEP_TOOL
from .experiment_state import experiment_context
from .experiment_goals import evidence_blocks, submitted_pings, is_config_block, complete_config, ROUTE_EXCERPT
from .investigation_prompts import system_instructions, GOAL_TOOL_NAMES
from .operation_history import inspect_history, prompt_info
from .connectivity import host_literals
from .diagnostic_planner import normalize_check_command

SENSITIVE = re.compile(r"authentication|password|passwd|cipher|community|secret|pre-shared|api[_-]?key|authorization|bearer|sk-[a-z0-9]{12,}|BEGIN.*PRIVATE KEY", re.I)


def safe_line(text):
    return "[敏感内容已省略]" if SENSITIVE.search(text) else text


def safe_text(text):
    return "\n".join(safe_line(line) for line in text.splitlines())


def safe_value(value):
    if isinstance(value, dict):
        return {key: safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_value(item) for item in value]
    return safe_text(value) if isinstance(value, str) else value


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("字段不符合工具定义")


def short_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("文本为空或过长")
    return safe_text(value)


def validate_open(payload):
    if "personal_memory" in payload:
        validate_memory_context(payload["personal_memory"])
    validate_scope(payload.get("knowledge_scope", "default"))
    if "course_documents" in payload:
        validate_course_documents(payload["course_documents"], payload.get("knowledge_scope", "default"))
    learning = learning_policy(payload)
    configs = payload.get("configs")
    if not isinstance(configs, dict) or not (0 if learning else 1) <= len(configs) <= 20 or not all(isinstance(key, str) and 0 < len(key) <= 120 and isinstance(value, str) and 0 < len(value) <= 200_000 for key, value in configs.items()):
        raise ValueError("开放排查需要1～20份设备材料，名称不超过120字，正文不超过200KB")
    if not isinstance(payload.get("question"), str) or not payload["question"].strip() or len(payload["question"]) > 10000:
        raise ValueError("请描述实际问题，长度不超过10000字")
    if type(payload.get("use_llm", False)) is not bool:
        raise ValueError("use_llm 必须是布尔值")
    if type(payload.get("hint_level", 2)) is not int or payload.get("hint_level", 2) not in (1, 2, 3):
        raise ValueError("hint_level 必须为1、2或3")
    if not isinstance(payload.get("requirements", {}), dict) or not isinstance(payload.get("topology", {}), dict):
        raise ValueError("topology 和 requirements 如有填写须为对象；开放排查可使用空对象")
    outputs = payload.get("command_outputs", [])
    if not isinstance(outputs, list) or len(outputs) > 60:
        raise ValueError("command_outputs 最多60份")
    for output in outputs:
        if not isinstance(output, dict) or not all(isinstance(output.get(key), str) for key in ("device", "command", "text")) or len(output["text"]) > 100000:
            raise ValueError("运行输出须包含 device、command、text，正文不超过100KB")
    if payload.get("evidence_kind", "user_submitted") not in ("demo", "user_submitted"):
        raise ValueError("evidence_kind 无效")
    recordings = payload.get("recordings", [])
    if not isinstance(recordings, list) or len(recordings) > 15 or not all(isinstance(item, str) and re.fullmatch(r"[a-f0-9]{32}", item) for item in recordings):
        raise ValueError("recordings须为最多15个截图或录屏编号")
    interaction = payload.get("interaction")
    if interaction is not None:
        exact_keys(interaction, ("type", "message"))
        if interaction["type"] not in ("ask", "repair", "verify", "update"):
            raise ValueError("不支持的交互类型")
        short_text(interaction["message"], 4000)


def source_bundle(payload, conversation=None, *, previous_materials=None):
    from .engine import fingerprint
    sources, warnings = {}, []
    for index, (device, text) in enumerate(payload["configs"].items(), 1):
        sources[f"config:{index}"] = {"device": device, "kind": "screenshot_ocr" if "-截图识别.log" in device else "submitted_material", "text": text}
        if text.lstrip().startswith("{"):
            try:
                value = json.loads(text)
                if isinstance(value, dict) and value.get("kind") == "labagent_repair_run":
                    sources[f"config:{index}"]["kind"] = "repair_run"
            except ValueError:
                pass
    if previous_materials:
        # Keep the previous order for carried files, then apply this turn's uploads.
        unchanged = {name: index for index, (name, text) in enumerate(previous_materials.items())
                     if payload["configs"].get(name) == text}
        sources = dict(sorted(sources.items(), key=lambda item: unchanged.get(item[1]["device"], len(previous_materials))))
    if payload.get("question"):
        sources["question:initial"] = {"device": "实验问题与要求", "kind": "user_question", "text": payload["question"]}
    for index, output in enumerate(payload.get("command_outputs", []), 1):
        if output.get("config_fingerprint") != fingerprint(payload):
            warnings.append(f"运行输出{index}未绑定当前输入，未提供给模型；请采集或确认新输出并绑定后继续。")
            continue
        sources[f"output:{index}"] = {"device": output["device"], "kind": "submitted_command_output", "command": output["command"], "text": output["text"]}
    for index, turn in enumerate(conversation or [], 1):
        if turn.get("intent") != "analyze" and turn.get("user"):
            sources[f"message:{index}"] = {"device": f"第{index}轮追问", "kind": "user_message", "text": turn["user"]}
    if payload.get("interaction"):
        sources["message:current"] = {"device": "本轮追问", "kind": "user_message", "text": payload["interaction"]["message"]}
    for source_id, source in sources.items():
        source["source_id"] = source_id
        source["lines"] = [safe_line(line) for line in source["text"].splitlines()]
    return sources, warnings


def manifest(sources):
    return [{key: source[key] for key in ("source_id", "device", "kind")} |
            {"line_count": len(source["lines"]), "command": safe_text(source.get("command", ""))} for source in sources.values()]


def submitted_host_addresses(payload, sources):
    addresses = set()
    for source in sources.values():
        addresses.update(host_literals(safe_text(source.get("command", ""))))
        for line in source["lines"]:
            addresses.update(host_literals(line))
    for field in ("topology", "requirements"):
        addresses.update(host_literals(safe_text(json.dumps(payload.get(field, {}), ensure_ascii=False))))
    return addresses


def prepare_investigation(payload, sources, previous=None, *, conversation=None, previous_materials=None, connected=None, goal_checks=True, registry):
    active_learning = learning_policy(payload)
    messages = [
        {"role": "system", "content": system_instructions(active_learning, previous, connected=bool(connected), goal_checks=goal_checks)},
        {"role": "user", "content": json.dumps({"question": safe_text(payload["question"]), "materials": manifest(sources),
            "knowledge_scope": payload.get("knowledge_scope", "default"),
            "personal_memory": safe_value(payload.get("personal_memory", [])),
            "requirements": safe_text(json.dumps(payload.get("requirements", {}), ensure_ascii=False)),
            "topology": safe_text(json.dumps(payload.get("topology", {}), ensure_ascii=False)), "evidence_kind": payload.get("evidence_kind", "user_submitted"),
            "previous_hypotheses": [{"title": item["title"], "state": "previous_unverified"} for item in (previous or {}).get("hypotheses", [])],
            "previous_summary": safe_text((previous or {}).get("summary", "")), "previous_requests": safe_value((previous or {}).get("pending_requests", (previous or {}).get("evidence_requests", []))), "interaction": safe_value(payload.get("interaction", {})),
            "experiment_goal": safe_text((previous or {}).get("experiment_goal", "")), "previous_outcome": (previous or {}).get("outcome"),
            "verification_targets": (previous or {}).get("verification_targets", []),
            "saved_goals": (previous or {}).get("goal_assessment", {}).get("criteria", []),
            "previous_plan": safe_value((previous or {}).get("diagnostic_plan", {})),
            "experiment_state": safe_value(experiment_context(previous, payload, previous_materials)),
            "operation_history": {"event_count": inspect_history(sources)["event_count"], "tool": "inspect_operation_timeline"},
            "conversation": safe_value((conversation or [])[-6:])}, ensure_ascii=False)}]
    if payload.get("interaction"):
        messages.append({"role": "user", "content": "本次请求：" + safe_text(payload["interaction"]["message"]) + "\n以上JSON中的question是最初实验背景，conversation是历史记录。请直接回答本次请求，不重新回答最初问题。"})
    available_tools = [tool for tool in registry.definitions() if (not active_learning or tool["function"]["name"] != "submit_hypotheses")
                       and (goal_checks or tool["function"]["name"] not in GOAL_TOOL_NAMES)]
    if active_learning:
        available_tools.append(STEP_TOOL)
    if connected:
        available_tools.extend(connected.tools)
    return active_learning, messages, available_tools


def observed_device_commands(sources):
    """Only current output blocks count; a partial configuration is not a full read."""
    commands = set()
    for block in evidence_blocks(sources):
        command = normalize_check_command(block['command'])
        output = [line for line in block['lines'] if line.strip() and not prompt_info(line)]
        if not command or not output or ROUTE_EXCERPT.search('\n'.join(output)):
            continue
        if is_config_block(block) and not complete_config(block):
            continue
        commands.add((block['device'], command))
    for ping in submitted_pings(sources)[0]:
        commands.add((ping['test_device'], 'ping ' + ping['target']))
    return commands
