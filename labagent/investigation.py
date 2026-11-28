"""Open network investigation: model-selected source reads, cited hypotheses.

No fault catalogue is used here. Checked citations establish provenance, not
causality. Model hypotheses never become deterministic faults or verification.
"""
from datetime import datetime, timezone
import difflib
import json
import os
from contextlib import nullcontext
import re
import time

from .knowledge import search, validate_scope, validate_course_documents
from .personal import validate_memory_context
from .model import configured, chat_completion, ModelError, public_config, bounded_setting
from .session_state import RESOLVED_OUTCOMES, experiment_goal, ordered_materials
from .experiment_state import experiment_context, summarize_experiment
from .progress import emit_progress, ToolProgress
from .experiment_goals import (GOAL_TOOL, ROUTE_TOOL, area_instruction_supports_goal, validate_goal_fields,
                               check_goals, check_route_selection, checked_frr_area_repair, complete_config, evidence_blocks,
                               explicit_area_goals, is_config_block, neighbor_instruction_supports_goal,
                               pending_ospf_loopback_evidence, submitted_pings)
from .diagnostic_planner import PLAN_TOOL, validate_plan
from .operation_history import HISTORY_TOOL, inspect_history, model_history
from .repair_lab import REPAIR_TOOL, compare_run, discover_runs
from .network_map import PATH_TOOL, build_map, explain_path
from .learning import policy as learning_policy, STEP_TOOL, validate_step
from .investigation_prompts import system_instructions
from .tool_workflow import run_tool_workflow
from .task_control import DiagnosisCancelled, check_cancelled
from .tool_registry import ToolRegistry
from .connectivity import address_literal, ground_ping_text, host_literals
from .investigation_context import (SENSITIVE, safe_line, safe_text, safe_value, exact_keys, short_text,
                                    validate_open, source_bundle, manifest, submitted_host_addresses,
                                    prepare_investigation, observed_device_commands)
from .investigation_verification import (ground_investigation_targets, checked_citation, validate_submission,
                                         covered_current_route_request, assess_goals, finalize_submission,
                                         merge_investigation_state, verify_excerpt_claims,
                                         verify_minimal_ospf_wildcard_advice)


READ_SOURCE_TOOL = {"type": "function", "function": {"name": "read_source", "description": "按原文行号读取一份已上传材料，每次最多60行。", "parameters": {
    "type": "object", "properties": {"source_id": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
    "required": ["source_id", "start_line", "end_line"], "additionalProperties": False}}}

RETRIEVE_REFERENCE_TOOL = {"type": "function", "function": {"name": "retrieve_reference", "description": "检索所选课程资料，仅作为背景参考，不是当前设备运行证据。", "parameters": {
    "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}}

COMPARE_MATERIALS_TOOL = {"type": "function", "function": {"name": "compare_materials", "description": "实际比较同一会话上次材料与本次材料，返回新增、删除和修改行。复测时先调用它，未更新材料不能假定已经修改。", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}

CHECK_SUBMITTED_PINGS_TOOL = {"type": "function", "function": {"name": "check_submitted_pings", "description": "核对上传材料中的Ping目标和收发包数。支持华为VRP、Windows中英文、Linux多行输出及单行摘要；返回目标、通过状态和逐行原文依据。", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}

SUBMIT_HYPOTHESES_TOOL = {"type": "function", "function": {"name": "submit_hypotheses", "description": "提交诊断说明、原文依据和下一步操作。每个假设至少引用一行实际读过的材料，quote须逐字匹配。观测通过使用Ping工具结果填写outcome。", "parameters": {
    "type": "object", "properties": {
        "summary": {"type": "string"}, "reply": {"type": "string", "description": "直接回答本次追问，最多250字。"},
        "answer_mode": {"type": "string", "enum": ["route_table"], "description": "用户只查询当前路由时必填route_table，由程序用已完成的check_route_selection结果生成回复。需要解释故障、给修改步骤或核对Ping时省略。"},
        "steps": {"type": "array", "items": {"type": "string"}, "description": "用户请求修改或复测时的具体步骤，最多4步。"},
        "outcome": {"type": "string", "enum": ["investigating", "ready_for_retest", "user_reported_resolved", "observations_passed"]},
        "experiment_goal": {"type": "string", "description": "从材料提取的具体实验目标，保留设备和目的地址，后续沿用。"},
        "operation_focus": {"type": "string", "description": "直接复制inspect_operation_timeline返回的events[].id字符串，例如op004。普通材料可省略。"},
        "verification_targets": {"type": "array", "items": {"type": "string"}, "description": "实验目标对应的Ping目的地址；首次分析时提取，后续核对同一组目标。"},
        "resolution_evidence": {"type": "array", "items": {"type": "object", "properties": {"source_id": {"type": "string"}, "line": {"type": "integer"}, "quote": {"type": "string"}}, "required": ["source_id", "line", "quote"], "additionalProperties": False}, "description": "用户明确反馈目标已恢复时，引用本轮追问的原文。"},
        "hypotheses": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "reasoning": {"type": "string"},
            "state": {"type": "string", "enum": ["unverified", "ruled_out"], "description": "现有证据已排除时填写ruled_out，该项不会进入待验证假设；仍待排查时填写unverified。"},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {"source_id": {"type": "string"}, "line": {"type": "integer"}, "quote": {"type": "string"}}, "required": ["source_id", "line", "quote"], "additionalProperties": False}}},
            "required": ["title", "reasoning", "evidence"], "additionalProperties": False}},
        "evidence_requests": {"type": "array", "items": {"type": "object", "properties": {"device": {"type": "string"}, "information": {"type": "string"}, "why": {"type": "string"}}, "required": ["device", "information", "why"], "additionalProperties": False}}},
    "required": ["summary", "hypotheses", "evidence_requests"], "additionalProperties": False}}}


tool_registry = ToolRegistry()


@tool_registry.register(GOAL_TOOL, "正在核对实验目标与协议要求")
def _check_experiment_goals(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    context = diagnostic_context if diagnostic_context is not None else {}
    if context.get("goal_checks_disabled"):
        raise ValueError("本轮未启用实验目标检查")
    if not isinstance(args, dict) or set(args) - {"goals"}:
        raise ValueError("请提供goals或空对象")
    goals = args.get("goals", [])
    if not isinstance(goals, list) or len(goals) > 8:
        raise ValueError("最多8项实验目标")
    saved = list(context.get("criteria", []))
    for goal in goals:
        validate_goal_fields(goal)
        existing = next((item for item in saved if item["id"] == goal["id"]), None)
        if existing:
            if any(goal[key] != existing[key] for key in ("title", "kind", "device", "target", "expected")):
                raise ValueError("已保存的目标继续沿用；新要求使用新编号并引用原文")
            continue
        instruction = checked_citation(goal["instruction"], sources, seen, allow_line_reference=True)
        if goal["kind"] == "ospf_interface_area" and not area_instruction_supports_goal(
                instruction["quote"], goal["device"], goal["target"], goal["expected"]):
            raise ValueError("区域目标的原文须同时写出设备、接口以及该接口唯一的目标区域；不要添加推测的接口或区号")
        if goal["kind"] == "ospf_neighbor" and not neighbor_instruction_supports_goal(
                instruction["quote"], goal["device"], goal["target"], goal["expected"]):
            raise ValueError("邻居目标的原文须明确设备、对端 Router ID 和目标状态")
        if goal["kind"] == "reachability" and address_literal(goal["target"]) not in submitted_host_addresses(payload, sources):
            raise ValueError("材料未提供该 Ping 目的地址。请先索要目标端点实际 IP，暂不建立该目标。")
        saved.append({**goal, "instruction": instruction})
    if not saved and not explicit_area_goals(sources):
        raise ValueError("请先从已读取的实验说明提取具体目标")
    if len(saved) > 8:
        raise ValueError("最多8项实验目标")
    context["criteria"] = saved
    assessment = assess_goals(context, sources, seen, payload, previous_materials)
    return assessment, False


@tool_registry.register(ROUTE_TOOL, "正在核对实际路由来源和下一跳")
def _check_route_selection(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    context = diagnostic_context if diagnostic_context is not None else {}
    exact_keys(args, ("device", "target"))
    device, target = short_text(args["device"], 120), short_text(args["target"], 64)
    result = check_route_selection(device, target, evidence_blocks(sources, include_history=payload["configs"] == previous_materials))
    seen.update((ref["source_id"], ref["line"]) for ref in result["evidence"])
    context.setdefault("route_selection", {})[(device.casefold(), target)] = result
    return result, False


@tool_registry.register(PLAN_TOOL, "正在比较候选检查并选择下一步")
def _plan_diagnostic_checks(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    context = diagnostic_context if diagnostic_context is not None else {}
    if context.get("goal_checks_disabled"):
        raise ValueError("本轮未启用主动检查选择")
    plan = validate_plan(args, lambda value: checked_citation(value, sources, seen),
                         observed_commands=observed_device_commands(sources, previous_materials=previous_materials))
    known = submitted_host_addresses(payload, sources)
    if any(ground_ping_text(test["command"], known) != test["command"] for test in plan["tests"]):
        raise ValueError("Ping 检查需要材料中的实际目标 IP。缺少地址时，请在补充证据请求里索要地址。")
    context["plan"] = plan
    return plan, False


@tool_registry.register(HISTORY_TOOL, "正在回溯设备、配置视图与关键报错")
def _inspect_operation_timeline(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    context = diagnostic_context if diagnostic_context is not None else {}
    exact_keys(args, ())
    history = inspect_history(sources)
    context["operation_history"] = history
    for event in history["events"]:
        seen.update((item["source_id"], item["line"]) for item in event["evidence"] + event["response_evidence"])
    return model_history(history), False


@tool_registry.register(REPAIR_TOOL, "正在核对候选修复的实际运行结果")
def _compare_repair_candidates(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    context = diagnostic_context if diagnostic_context is not None else {}
    exact_keys(args, ("source_id",))
    source = sources.get(args["source_id"])
    if not source:
        raise ValueError("请使用本轮材料中的source_id")
    result = compare_run(json.loads(source["text"]))
    context["repair_compared"] = True
    phase_ids = {item["id"] for item in result["phases"]}
    excerpts = []
    for number,line in enumerate(source["lines"],1):
        phase_id = re.fullmatch(r'\s*"id":\s*"([^"]+)",?\s*',line)
        if (phase_id and phase_id[1] in phase_ids) or ('"pings"' not in line and '"PING ' in line and len(line)<1400):
            excerpts.append({"source_id":args["source_id"],"line":number,"quote":line})
    result["source_evidence"] = excerpts[:12]
    result["submission_hint"] = "若用户问各阶段的结果，检查已完成。将phases[].checks中的detail用于summary/reply，hypotheses和evidence_requests填空数组。"
    seen.update((args["source_id"], number) for number in range(1, len(source["lines"]) + 1))
    return safe_value(result), False


@tool_registry.register(PATH_TOOL, "正在沿接口、网关和路由解释通信路径")
def _explain_network_path(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    exact_keys(args, ("source", "target"))
    result = explain_path(build_map(sources), args["source"], args["target"])
    for step in result["steps"]:
        seen.update((ref["source_id"], ref["line"]) for ref in step["evidence"])
    return result, False


@tool_registry.register(READ_SOURCE_TOOL, "正在读取材料")
def _read_source(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    exact_keys(args, ("source_id", "start_line", "end_line"))
    source_id, first, last = args["source_id"], args["start_line"], args["end_line"]
    if not isinstance(source_id, str) or source_id not in sources or type(first) is not int or type(last) is not int:
        raise ValueError("请使用材料清单中的source_id和整数行号")
    source = sources[source_id]
    if not 1 <= first <= last <= len(source["lines"]) or last - first >= 60:
        raise ValueError("行号越界或超过60行，请按材料行数选择更小范围")
    rows, budget = [], 0
    for number in range(first, last + 1):
        text = source["lines"][number - 1]
        if budget + len(text) > 12000:
            break
        rows.append({"line": number, "text": text})
        seen.add((source_id, number))
        budget += len(text)
    result = {"source_id": source_id, "device": source["device"], "lines": rows,
              "range_complete": bool(rows) and rows[-1]["line"] == last}
    if rows:
        coverage = []
        for block in evidence_blocks({source_id: source}, include_history=True):
            start, end = block["start"] + 1, block["start"] + len(block["lines"])
            if end >= first and start <= rows[-1]["line"] and is_config_block(block):
                complete = complete_config(block)
                coverage.append({"device": block["device"], "start_line": start, "end_line": end,
                                 "scope": "complete_snapshot" if complete else "partial_or_unconfirmed",
                                 "all_lines_read": all((source_id, n) in seen for n in range(start, end + 1))})
        if coverage:
            result["configuration_coverage"] = coverage
            if any(item["scope"] != "complete_snapshot" or not item["all_lines_read"] for item in coverage):
                result["configuration_boundary"] = "部分配置只能证明看到的条目。不能认定未见的命令未配置，也不能把补写这条命令作为本轮改法；先补齐相应设备配置。"
    return result, False


@tool_registry.register(RETRIEVE_REFERENCE_TOOL, "正在检索参考资料")
def _retrieve_reference(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    exact_keys(args, ("query",))
    query = short_text(args["query"], 2000)
    found = search(query, scope=payload.get("knowledge_scope", "default"), limit=3,
                   course_documents=payload.get("course_documents"))
    return {"role": "教学参考，不是设备观测", "match_summary": found.get("match_summary", {
                "status": "reference_candidates" if found["results"] else "no_match", "answerability": "not_assessed"}),
            "results": [{**doc, "content": safe_text(doc["content"])} for doc in found["results"]], "warnings": found["warnings"]}, False


@tool_registry.register(COMPARE_MATERIALS_TOOL, "正在比较前后材料")
def _compare_materials(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    exact_keys(args, ())
    old = previous_materials or {}
    current = payload["configs"]
    changes = []
    for material in sorted(set(old) | set(current)):
        if old.get(material) != current.get(material):
            diff = list(difflib.unified_diff(safe_text(old.get(material, "")).splitlines(), safe_text(current.get(material, "")).splitlines(), n=2, lineterm=""))
            changes.append({"material": material, "change": "added" if material not in old else "removed" if material not in current else "updated", "diff": "\n".join(diff[:70])[:8000]})
    return {"has_previous": bool(old), "changed": bool(changes), "changes": changes}, False


@tool_registry.register(CHECK_SUBMITTED_PINGS_TOOL, "正在核对Ping统计")
def _check_submitted_pings(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    exact_keys(args, ())
    observations, reported, superseded = submitted_pings(sources)
    for row in observations + reported + superseded:
        seen.update((row["source_id"], item["line"]) for item in row["evidence"])
    return {"source": "uploaded_text", "pings": observations[:100], "reported_pings": reported,
            "superseded_pings": superseded[:100],
            "note": "pings为同设备同目标的最新输出，superseded_pings为已被新输出替代的历史记录；reported_pings为用户口述统计。"}, False


@tool_registry.register(SUBMIT_HYPOTHESES_TOOL, "正在整理结论与下一步")
def _submit_hypotheses(args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    context = diagnostic_context if diagnostic_context is not None else {}
    if not isinstance(args, dict):
        raise ValueError("字段不符合工具定义")
    route_selection = list(context.get("route_selection", {}).values())
    if not seen and not route_selection:
        raise ValueError("请先读取当前材料，再提交假设或补充请求")
    route_table_answer = "answer_mode" in args
    if route_table_answer:
        if args["answer_mode"] != "route_table" or not route_selection or args.get("outcome", "investigating") != "investigating":
            raise ValueError("仅查询路由时，请先调用check_route_selection，再用route_table模式提交。")
        detail = "\n".join(lookup["detail"] for lookup in route_selection)
        if len(detail) > 1200:
            detail = "已核对各目标的路由，请在本轮诊断详情中查看结果和原文。"
        # The tool has already checked the uploaded rows. Free-form model text cannot add route claims here.
        args = {"summary": detail, "hypotheses": [], "evidence_requests": []}
    assessment = assess_goals(context, sources, seen, payload, previous_materials)
    if args.get("outcome") == "observations_passed" and assessment and assessment["status"] != "passed":
        missing = "；".join(item["detail"] for item in assessment["checks"] if item["status"] != "passed")
        raise ValueError("实验目标还未全部满足，请据此继续排查：" + missing)
    interaction = payload.get("interaction")
    asks_for_repair = (interaction["type"] == "repair" if interaction else
                       bool(re.search(r"修复|修改|改法|修正|若要改|怎么改|如何改|怎样改", payload.get("question", ""))))
    asks_for_diagnosis = (interaction["type"] in {"repair", "update", "verify"} if interaction else
                          asks_for_repair or bool(re.search(r"排查|诊断|故障|达标|不通|漏配|未宣告", payload.get("question", ""))))
    repair = (checked_frr_area_repair(assessment, sources)
              if not route_table_answer and args.get("outcome", "investigating") in {"investigating", "ready_for_retest"}
              and asks_for_repair else None)
    pending_config = (pending_ospf_loopback_evidence(sources, assessment)
                      if not repair and not route_table_answer and asks_for_diagnosis
                      and args.get("outcome", "investigating") in {"investigating", "ready_for_retest"} else None)
    if grounded := repair or pending_config:
        args = {**grounded, "experiment_goal": args.get("experiment_goal", "")}
    result = validate_submission(args, sources, seen, conversational=bool(interaction) or bool(grounded), checked_pings=checked_pings,
                                 verifying=checked_pings is not None, required_targets=required_targets,
                                 comparing=context.get("repair_compared", False), route_checked=bool(route_selection))
    if args.get("operation_focus"):
        history = context.get("operation_history", {})
        event = next((item for item in history.get("events", []) if item["id"] == args["operation_focus"]), None)
        if not event:
            choices = ", ".join(item["id"] for item in history.get("events", [])[:12])
            raise ValueError("operation_focus直接填写事件id字符串，可选：" + (choices or "先调用inspect_operation_timeline"))
        result["operation_focus"] = event["id"]
    if not route_table_answer and not grounded:
        verify_excerpt_claims(result, sources, seen)
        verify_minimal_ospf_wildcard_advice(result, sources)
    result = finalize_submission(result, context, assessment, sources, payload, previous_materials,
                                 route_table_answer=route_table_answer, grounded=grounded, pending_config=pending_config)
    return result, True


TOOLS = tool_registry.definitions()


def execute_tool(name, args, payload, sources, seen, *, previous_materials=None, checked_pings=None, required_targets=None, diagnostic_context=None):
    return tool_registry.execute(name, args, payload, sources, seen, previous_materials=previous_materials,
                                 checked_pings=checked_pings, required_targets=required_targets,
                                 diagnostic_context=diagnostic_context)


def run_investigator(payload, sources, previous=None, completion=None, *, conversation=None, previous_materials=None, goal_checks=True, lab_tools=None):
    if not payload.get("use_llm") or not configured():
        return _run_investigator(payload, sources, previous, completion, conversation=conversation, previous_materials=previous_materials, goal_checks=goal_checks)
    from .mcp_client import LabMCPTools, MCPError
    path = os.environ.get("LABAGENT_MCP_CONFIG")
    context = nullcontext(lab_tools) if lab_tools is not None else LabMCPTools(path) if path else nullcontext(None)
    try:
        connected = context.__enter__()
    except (MCPError, OSError, json.JSONDecodeError):
        result = _run_investigator(payload, sources, previous, completion, conversation=conversation, previous_materials=previous_materials, goal_checks=goal_checks)
        result["mcp_warnings"] = ["实验平台连接配置尚未就绪，本轮继续使用已提交材料"]
        return result
    try:
        return _run_investigator(payload, sources, previous, completion, conversation=conversation,
                                 previous_materials=previous_materials, connected=connected, goal_checks=goal_checks)
    finally:
        context.__exit__(None, None, None)


def _run_investigator(payload, sources, previous=None, completion=None, *, conversation=None, previous_materials=None, connected=None, goal_checks=True):
    if not payload.get("use_llm") or not configured():
        return {"status": "not_configured", "message": "开放排查需要启用并配置模型；材料已保留，尚未生成假设。", "tool_trace": []}
    active_learning, messages, available_tools = prepare_investigation(
        payload, sources, previous, conversation=conversation, previous_materials=previous_materials,
        connected=connected, goal_checks=goal_checks, registry=tool_registry)
    trace, metadata = [], []
    # Current follow-up lines were already supplied to the model with their source ID.
    current_lines = sources.get("message:current", {}).get("lines", [])
    seen = {("message:current", number) for number in range(1, len(current_lines) + 1)}
    diagnostic_context = {"criteria": (previous or {}).get("goal_assessment", {}).get("criteria", []),
                          "goal_checks_disabled": not goal_checks}
    start = time.monotonic()
    def info():
        return {"tool_trace": trace, "calls": metadata, "provider": public_config()["provider"], "model": public_config()["model"],
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "engine_state": {"goal_assessment": assess_goals(diagnostic_context, sources, seen, payload, previous_materials)}}
    round_limit = 6
    guided_followup = active_learning and payload.get("interaction", {}).get("type") == "ask"
    submission_only = False
    calls, outcome, pending_learning, pending_item = [], None, None, None
    approved_tools = {tool["function"]["name"] for tool in available_tools}
    def select_check(turn):
        nonlocal calls, outcome, submission_only
        submission_only = guided_followup and turn == round_limit - 1
        emit_progress("analyzing", "正在分析本轮材料与问题…")
        if completion is not None:
            message = completion(messages)
        else:
            choice = {"tool_choice": "required"} if submission_only else {}
            message = chat_completion(messages, tools=[STEP_TOOL] if submission_only else available_tools,
                                      max_tokens=bounded_setting("LABAGENT_OPEN_MAX_TOKENS", 1600, 256, 2048), **choice)
        if not isinstance(message, dict):
            raise ValueError("invalid response")
        if isinstance(message.get("_meta"), dict):
            metadata.append({"round": turn + 1, **message["_meta"]})
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not 1 <= len(calls) <= 8:
            outcome = {"status": "no_evidence", "message": "模型未按工具协议提交有出处的结果，未采纳其回答。", **info()}
            return True
        messages.append({"role": "assistant", "content": None, "tool_calls": calls})
        return False

    def execute_check(turn):
        nonlocal outcome, pending_learning, pending_item
        ids = set()
        for call in calls:
            check_cancelled()
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in ids or not isinstance(call.get("function"), dict):
                raise ValueError("invalid tool call")
            ids.add(call["id"])
            name = call["function"].get("name")
            if not isinstance(name, str):
                raise ValueError("invalid tool name")
            args, terminal = None, False
            label = tool_registry.label(name)
            if name == "submit_learning_step":
                label = "正在整理当前一步的提示"
            if connected and name in connected.bindings:
                label = "正在读取已连接实验设备的输出"
            item = ToolProgress(name if name in approved_tools else "unapproved_tool", label, turn + 1)
            try:
                if submission_only and name != "submit_learning_step":
                    raise ValueError("检查轮次已用完，请提交当前一步引导")
                raw = call["function"].get("arguments", "")
                if not isinstance(raw, str) or len(raw) > 20000:
                    raise ValueError("工具参数无效或过长")
                args = json.loads(raw)
                if name in {"submit_hypotheses", "submit_learning_step"} and len(calls) != 1:
                    raise ValueError("提交假设时必须单独调用此工具")
                if name == "read_source" and isinstance(args, dict):
                    item.label += "：" + sources.get(args.get("source_id"), {}).get("device", "")
                    item.publish("running", "tool", item.label, "item.updated")
                latest_pings = next((item["result"] for item in reversed(trace) if item["tool"] == "check_submitted_pings" and item["status"] == "ok"), None)
                if active_learning and name == "submit_hypotheses":
                    raise ValueError("当前是逐步引导，请使用submit_learning_step")
                if name == "submit_learning_step" and active_learning:
                    pending_learning = args
                    pending_item = item
                    observation = {"status": "awaiting_learning_review"}
                    terminal = True
                elif connected and name in connected.bindings:
                    if len(payload["configs"]) >= 20:
                        raise ValueError("当前材料已满20份，请先精简材料")
                    observed = connected.execute(name, args)
                    source_id = "mcp:" + str(len([key for key in sources if key.startswith("mcp:")]) + 1)
                    slug = re.sub(r"[^\w-]+", "_", observed["command"])[:35]
                    material = ("MCP-" + observed["device"] + "-" + name + "-" + slug + ".log")[:120]
                    existing = next((key for key, value in sources.items() if value["device"] == material), None)
                    if existing:
                        source_id = existing
                        seen.difference_update({ref for ref in seen if ref[0] == source_id})
                    text = safe_text(observed["text"])
                    lines = text.splitlines()
                    sources[source_id] = {"source_id": source_id, "device": material, "kind": "command_output",
                        "command": observed["command"], "text": text, "lines": lines, "collected": True}
                    payload["configs"][material] = "设备：" + observed["device"] + "\n命令：" + observed["command"] + "\n" + text
                    # Citation lines must match the stored material exactly.
                    sources[source_id]["text"] = payload["configs"][material]
                    sources[source_id]["lines"] = payload["configs"][material].splitlines()
                    seen.update((source_id, line) for line in range(1, len(sources[source_id]["lines"]) + 1))
                    observation = {"source_id": source_id, "device": observed["device"], "material": material,
                        "command": observed["command"], "lines": [{"line": i, "text": row} for i, row in enumerate(sources[source_id]["lines"], 1)],
                        "source": "mcp_device_observation", "observed_at": datetime.now(timezone.utc).isoformat()}
                else:
                    observation, terminal = execute_tool(name, args, payload, sources, seen, previous_materials=previous_materials, checked_pings=latest_pings,
                                                         required_targets=(previous or {}).get("verification_targets"), diagnostic_context=diagnostic_context)
                state = "ok"
                if name == "submit_learning_step" and active_learning:
                    item.review()
                else:
                    item.complete()
            except (ValueError, TypeError, KeyError) as error:
                observation, state = {"error": str(error)[:200]}, "rejected"
                item.fail()
            except Exception:
                item.fail()
                raise
            trace.append({"item_id": item.id, "tool": name if name in approved_tools else "unapproved_tool", "status": state,
                          "round": turn + 1, "arguments": safe_value(args) if state == "ok" else None, "result": observation})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(observation, ensure_ascii=False)})
            if terminal:
                if active_learning:
                    return True
                outcome = {"status": "ok", "investigation": observation, "evidence_tools_used": True, **info()}
                return True
        return False

    def review_learning_step():
        nonlocal outcome, pending_learning, pending_item
        try:
            emit_progress("learning_review", "正在核对提示级别、原文依据和当前步骤…")
            diagnostic_context["goal_assessment"] = assess_goals(diagnostic_context, sources, seen, payload, previous_materials)
            observation = validate_step(pending_learning, active_learning, lambda ref: checked_citation(ref, sources, seen), diagnostic_context)
            if ground_ping_text(observation["learning"]["action"], submitted_host_addresses(payload, sources)) != observation["learning"]["action"]:
                raise ValueError("先索要目标端点的实际IP，不用示例地址代替学生当前实验地址")
            if diagnostic_context["goal_assessment"]:
                observation["goal_assessment"] = diagnostic_context["goal_assessment"]
            trace[-1]["result"] = observation
            messages[-1]["content"] = json.dumps(observation, ensure_ascii=False)
            outcome = {"status": "ok", "investigation": observation, "evidence_tools_used": True, **info()}
            pending_item.complete()
            return True
        except (ValueError, TypeError, KeyError) as error:
            trace[-1].update(status="rejected", arguments=None, result={"error": str(error)[:200]})
            messages[-1]["content"] = json.dumps(trace[-1]["result"], ensure_ascii=False)
            pending_item.fail()
            pending_learning = None
            pending_item = None
            outcome = None
            return False
        except Exception:
            pending_item.fail()
            raise

    try:
        workflow = run_tool_workflow(select_check, execute_check, review=review_learning_step if active_learning else None,
                                     limit=round_limit)
        check_cancelled()
        if outcome is not None:
            outcome["tool_workflow"] = workflow
            if connected:
                outcome["mcp_warnings"] = connected.warnings
            return outcome
        return {"status": "step_limit", "message": "达到六轮排查上限，请缩小问题或补充材料后继续。", "tool_workflow": workflow, **info()}
    except DiagnosisCancelled as error:
        if pending_item is not None:
            pending_item.publish("cancelled", "tool_cancelled", "已停止本轮提示核对。", "item.completed")
            trace[-1].update(status="cancelled", arguments=None, result={"status": "cancelled"})
        return {"status": "cancelled", "message": str(error), **info()}
    except ModelError as error:
        if error.call is not None:
            metadata.append({"round": len(metadata) + 1, **error.call})
        return {"status": "unavailable", **error.as_dict(), **info()}
    except Exception:
        return {"status": "unavailable", "error_code": "invalid_response", "message": "开放排查未完成，材料和已有工具记录已保留。", **info()}


def _repair_comparison_reply(comparisons):
    sections = []
    for comparison in comparisons:
        stages = []
        for phase in comparison["phases"]:
            text = f'「{safe_line(str(phase["title"]))}」通过 {phase["passed_count"]}/{phase["total"]} 项。'
            if 0 < phase["passed_count"] < phase["total"]:
                if phase["passed_count"] <= phase["total"] - phase["passed_count"]:
                    passed = "、".join(safe_line(str(check["title"])) for check in phase["checks"] if check["passed"])
                    text += f"通过项目为 {passed}。"
                else:
                    pending = "、".join(f'{safe_line(str(check["title"]))}（{safe_line(check["detail"]).rstrip("。")}）'
                                       for check in phase["checks"] if not check["passed"])
                    text += f"待达标项目为 {pending}。"
            stages.append(text)
        recommended = next((phase for phase in comparison["phases"] if phase["id"] == comparison["recommended"]), None)
        conclusion = (f'「{safe_line(str(recommended["title"]))}」满足全部实验目标。'
                      if recommended else "本次提交的阶段尚未满足全部实验目标。")
        sections.append("".join(stages) + conclusion)
    return "\n\n".join(sections)


def investigate(payload, previous=None, completion=None, *, conversation=None, previous_materials=None, goal_checks=True, lab_tools=None):
    from .engine import fingerprint
    validate_open(payload)
    sources, warnings = source_bundle(payload, conversation, previous_materials=previous_materials)
    comparisons = discover_runs(sources)
    valid_run_comparison = (bool(comparisons) and all(not item.get("error") for item in comparisons)
                            and all(source["kind"] in {"repair_run", "user_question", "user_message"} for source in sources.values()))
    operations = inspect_history(sources)
    if operations["event_count"]:
        emit_progress("timeline", f"已还原{operations['event_count']}步终端操作，正在核对关键时间点。")
    emit_progress("materials", f"已接收{len(payload['configs'])}份材料，正在结合本轮追问分析。")
    model = run_investigator(payload, sources, previous, completion, conversation=conversation,
                             previous_materials=previous_materials, goal_checks=goal_checks, lab_tools=lab_tools)
    analysis = model.pop("investigation", {})
    comparison_reply = (valid_run_comparison and not payload.get("learning")
                        and model.get("status") not in {"ok", "cancelled"})
    if comparison_reply:
        reply = _repair_comparison_reply(comparisons)
        analysis = {"summary": reply, "reply": reply, "hypotheses": [], "evidence_requests": [],
                    "outcome": "investigating"}
    if model.get("status") == "cancelled":
        analysis = {"summary": "已停止本轮检查。", "reply": model["message"], "hypotheses": [],
                    "evidence_requests": [], "outcome": "investigating"}
    engine_state = model.pop("engine_state", {})
    source_manifest = manifest(sources)
    payload["configs"] = ordered_materials(payload["configs"], source_manifest)
    analysis = merge_investigation_state(analysis, model, payload, sources, previous, previous_materials, engine_state,
                                         goal_checks=goal_checks, comparison_reply=comparison_reply)
    knowledge, reference_searches = {}, []
    for item in model["tool_trace"]:
        if item["tool"] == "retrieve_reference" and item["status"] == "ok":
            reference_searches.append({"query": item["arguments"]["query"], **item["result"].get("match_summary", {})})
            for document in item["result"]["results"]:
                knowledge[document.get("chunk_id", document.get("id"))] = document
            warnings.extend(item["result"].get("warnings", []))
    requests = [f"{item['device']}：{item['information']}（{item['why']}）" for item in analysis.get("pending_requests", [])]
    if (not requests and not comparison_reply and model.get("status") not in ("ok", "cancelled")
            and analysis.get("outcome") not in RESOLVED_OUTCOMES):
        requests = ["请启用可用模型或补充当前材料后继续开放排查。"]
    if model.get("status") == "ok":
        model["text"] = analysis["summary"]
    if analysis or (previous or {}).get("experiment_state"):
        analysis["experiment_state"] = summarize_experiment(previous, payload, sources, analysis, previous_materials,
                                                           succeeded=model.get("status") == "ok" or bool(comparison_reply))
    interaction_checks = {item["tool"]: item["result"] for item in model["tool_trace"] if item["status"] == "ok" and item["tool"] in ("compare_materials", "check_submitted_pings")}
    route_lookups = {(item["result"]["device"].casefold(), item["result"]["target"]): item["result"] for item in model["tool_trace"]
                     if item["status"] == "ok" and item["tool"] == "check_route_selection"}
    if route_lookups:
        interaction_checks["check_route_selection"] = list(route_lookups.values())
    passed = analysis.get("outcome") == "observations_passed"
    reported = analysis.get("outcome") == "user_reported_resolved"
    if passed:
        interaction_checks["check_submitted_pings"] = analysis["verification"]
    if reported:
        interaction_checks.pop("check_submitted_pings", None)
        interaction_checks["user_report"] = analysis["verification"]
    status = "cancelled" if model.get("status") == "cancelled" else "verified_from_observations" if passed else "resolved_by_user_report" if reported else "goals_pending" if analysis.get("outcome") == "goals_pending" else "needs_information"
    if valid_run_comparison and (comparison_reply or model.get("status") == "ok" and not requests):
        status = "comparison_complete"
    return {"schema_version": "1.0", "mode": "open_network", "experiment": "开放网络排查", "created_at": datetime.now(timezone.utc).isoformat(),
            "collected_materials": {source["device"]: source["text"] for source in sources.values() if source.get("collected")},
            "config_fingerprint": fingerprint(payload), "status": status, "findings": [], "unknowns": [], "checks": [],
            "runtime_checks": [], "runtime_missing": requests, "hint_level": analysis.get("learning", {}).get("level", payload.get("hint_level", 2)),
            "hint": analysis.get("summary") or "提交材料后开始分析。", "knowledge": list(knowledge.values()), "knowledge_scope": payload.get("knowledge_scope", "default"),
            "knowledge_retrieval": {"warnings": warnings, "searches": reference_searches}, "planning_knowledge": [], "command_output_parsing": {},
            "next_steps": requests, "summary": {"faults": 0, "unknowns": 0, "static_checks_passed": 0}, "investigation": analysis,
            "source_manifest": source_manifest, "llm": model, "interaction_checks": interaction_checks,
            "operation_history": {**operations, "focus_id": analysis.get("operation_focus") or operations["first_issue_id"]},
            "repair_comparisons": comparisons,
            "recordings": payload.get("recordings", []),
            "trace": [{"step": i, "component": "开放排查工具", "action": item["tool"], **item} for i, item in enumerate(model["tool_trace"], 1)]}
