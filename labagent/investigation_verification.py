"""Verify evidence and merge diagnostic states; never call models or devices."""
import ipaddress
import re

from .investigation_context import exact_keys, short_text, safe_text, submitted_host_addresses
from .session_state import RESOLVED_OUTCOMES, experiment_goal
from .experiment_goals import check_goals, explicit_area_goals, evidence_blocks, check_route_selection, is_config_block, complete_config
from .connectivity import address_literal, ground_ping_text
from .parser import parse_config, interface_id
from .operation_history import prompt_info

def ground_investigation_targets(analysis, known):
    closed = analysis.get("outcome") in RESOLVED_OUTCOMES
    targets = analysis.get("verification_targets", [])
    allowed = known | {address_literal(target) for target in analysis.get("verification", {}).get("targets", [])} if closed else known
    grounded = [target for target in targets if address_literal(target) in allowed - {None}]
    unverified = {address_literal(target) for target in targets if target not in grounded} - {None}
    changed = targets != grounded
    analysis["verification_targets"] = grounded

    def clean(text):
        nonlocal changed
        revised = ground_ping_text(text, allowed, unverified)
        changed |= revised != text
        return revised

    for field in ("summary", "reply", "experiment_goal"):
        if analysis.get(field):
            analysis[field] = clean(analysis[field])
    if "steps" in analysis:
        analysis["steps"] = [clean(step) for step in analysis["steps"]]
    if "hypotheses" in analysis:
        analysis["hypotheses"] = [{**item, "title": clean(item["title"]), "reasoning": clean(item["reasoning"])}
                                  for item in analysis["hypotheses"]]
    for field in ("evidence_requests", "pending_requests"):
        if field in analysis:
            analysis[field] = [{**item, "information": clean(item["information"]), "why": clean(item["why"])}
                               for item in analysis[field]]
    if changed and not closed:
        request = {"device": "目标端点", "information": "请提供目标端点的实际 IP 地址。",
                   "why": "确定 Ping 的准确目的地址"}
        for field in ("evidence_requests", "pending_requests"):
            pending = analysis.get(field, [])
            analysis[field] = [request, *[item for item in pending if item != request]][:6]


def checked_citation(citation, sources, seen, *, allow_line_reference=False):
    location = {"source_id", "line"}
    if not (allow_line_reference and isinstance(citation, dict) and citation.keys() == location):
        exact_keys(citation, location | {"quote"})
    source_id, line = citation["source_id"], citation["line"]
    if not isinstance(source_id, str) or type(line) is not int or (source_id, line) not in seen:
        raise ValueError("引用须指向本轮实际读取过的原文行")
    source = sources[source_id]
    quote = source["lines"][line - 1]
    if citation.get("quote", quote) != quote or not quote.strip() or quote == "[敏感内容已省略]":
        raise ValueError("quote必须逐字匹配可引用的完整原文行，保留缩进")
    return {"source_id": source_id, "line": line, "quote": quote, "device": source["device"]}


def validate_submission(args, sources, seen, *, conversational=False, checked_pings=None, verifying=False, required_targets=None, comparing=False, route_checked=False):
    required = {"summary", "hypotheses", "evidence_requests"}
    if not isinstance(args, dict) or not required <= set(args) or set(args) - required - {"reply", "steps", "outcome", "verification_targets", "experiment_goal", "resolution_evidence", "operation_focus"}:
        raise ValueError("字段不符合工具定义")
    summary = short_text(args["summary"], 1200)
    hypotheses, requests = args["hypotheses"], args["evidence_requests"]
    if not isinstance(hypotheses, list) or len(hypotheses) > 3:
        raise ValueError("最多3个假设")
    steps = args.get("steps", [])
    if not isinstance(steps, list) or len(steps) > 4:
        raise ValueError("修改或验证步骤最多4步")
    steps = [short_text(step, 800) for step in steps]
    ready_for_retest = args.get("outcome") == "ready_for_retest" and bool(steps)
    if not isinstance(requests, list) or not (0 if conversational or comparing or route_checked or ready_for_retest or args.get("outcome") == "observations_passed" else 1) <= len(requests) <= 6:
        raise ValueError("请提供明确的补充证据请求；追问交流时可为空")
    checked, ruled_out = [], []
    for hypothesis in hypotheses:
        if not isinstance(hypothesis, dict) or set(hypothesis) - {"state"} != {"title", "reasoning", "evidence"}:
            raise ValueError("字段不符合工具定义")
        state = hypothesis.get("state", "unverified")
        if state not in ("unverified", "ruled_out"):
            raise ValueError("仅接受待验证或已排除的假设状态")
        evidence = hypothesis["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 6:
            raise ValueError("每个假设需要1～6条原文引用")
        citations = []
        for citation in evidence:
            citations.append(checked_citation(citation, sources, seen))
        title = short_text(hypothesis["title"], 160)
        reasoning = short_text(hypothesis["reasoning"], 1000)
        (checked if state == "unverified" else ruled_out).append({
            "title": title, "reasoning": reasoning, "state": state,
            "evidence": citations, "citation_check": "exact_seen_lines"})
    checked_requests = []
    for request in requests:
        exact_keys(request, ("device", "information", "why"))
        checked_requests.append({key: short_text(request[key], 500) for key in ("device", "information", "why")})
    reply = short_text(args["reply"], 1600) if args.get("reply") else summary
    outcome = args.get("outcome", "investigating")
    if outcome not in ("investigating", "ready_for_retest", "user_reported_resolved", "observations_passed"):
        raise ValueError("不支持的排查阶段")
    targets = args.get("verification_targets", [])
    if not isinstance(targets, list) or len(targets) > 10 or not all(isinstance(target, str) and 0 < len(target) < 100 for target in targets):
        raise ValueError("请提供最多10个实验目标地址")
    verification = {}
    if outcome == "observations_passed":
        rows = (checked_pings or {}).get("pings", [])
        if not verifying or not targets:
            raise ValueError("请在复测时调用Ping核对工具，并列出实验目标地址")
        if not set(required_targets or []) <= set(targets):
            raise ValueError("请核对原实验目标地址，包含此前确定的全部目标")
        if not all(any(row["target"] == target and row["passed"] for row in rows) and not any(row["target"] == target and not row["passed"] for row in rows) for target in targets):
            raise ValueError("目标地址缺少通过的提交观测，或仍有失败记录")
        verification = {"targets": targets, "pings": rows, "source": "uploaded_text"}
        checked, checked_requests = [], []
    if outcome == "user_reported_resolved":
        evidence = args.get("resolution_evidence", [])
        if not conversational or not isinstance(evidence, list) or not 1 <= len(evidence) <= 3:
            raise ValueError("请引用本轮用户反馈恢复的原文")
        citations = [checked_citation(item, sources, seen) for item in evidence]
        if any(item["source_id"] != "message:current" for item in citations):
            raise ValueError("恢复反馈须来自本轮追问")
        verification = {"targets": targets or required_targets or [], "source": "user_report", "evidence": citations}
        checked, checked_requests = [], []
    return {"summary": summary, "reply": reply, "steps": [short_text(step, 800) for step in steps], "hypotheses": checked, "ruled_out": ruled_out, "evidence_requests": checked_requests,
            "outcome": outcome, "verification": verification, "verification_targets": targets,
            "experiment_goal": short_text(args["experiment_goal"], 500) if args.get("experiment_goal") else "",
            "boundary": "依据本次提交的材料与工具记录生成。"}


def covered_current_route_request(request, missing_routes):
    """Drop redundant requests for the current table; retain retests and combined requests."""
    text = " ".join(request["information"].split())
    if re.search(r"修改后|修复后|复测|改后|更新后|恢复后|after\b|post.repair", text + " " + request["why"], re.I):
        return False
    # Parenthesized equivalents explain how to collect one table, not an extra request.
    text = re.sub(r"[（(]([^（）()]*)[）)]", lambda match: "" if all(
        re.fullmatch(r"(?:display ip routing-table|show ip route|ip -4 route show table main)", part.strip(), re.I)
        for part in re.split(r"[、，,]|\s+或\s+", match[1])) else match[0], text).strip()
    match = re.fullmatch(r"(?:完整\s*)?(?:当前\s*)?(?:完整\s*)?(?:(?:IPv4\s*)?路由表|display ip routing-table)"
                         r"\s*(\d+(?:\.\d+){3}(?:/\d+)?)?\s*(?:的?\s*(?:完整\s*)?(?:输出|原始输出|表项))?", text, re.I)
    if not match:
        return False
    for lookup in missing_routes:
        if lookup["status"] not in {"observed", "no_match"} or lookup["device"].casefold() != request["device"].casefold():
            continue
        if not match[1] and "/" in lookup["target"]:
            continue  # An exact-prefix lookup does not establish the host's forwarding choice.
        if not match[1] or match[1] == lookup["target"] or match[1] == lookup["target"] + "/32":
            return True
    return False


def assess_goals(context, sources, seen, payload, previous_materials):
    if context.get("goal_checks_disabled") or not (context.get("criteria") or explicit_area_goals(sources)):
        return {}
    same_materials = payload["configs"] == previous_materials
    assessment = check_goals(context.get("criteria", []), sources, include_history=same_materials)
    for check in assessment["checks"]:
        seen.update((item["source_id"], item["line"]) for item in check["evidence"])
    return assessment


def merge_investigation_state(analysis, model, payload, sources, previous, previous_materials, engine_state, *, goal_checks=True, comparison_reply=False):
    same_materials = list(payload["configs"].items()) == list((previous_materials or {}).items())
    previous_closed = model.get("status") != "cancelled" and (previous or {}).get("outcome") in RESOLVED_OUTCOMES
    if not analysis and previous_closed and same_materials and not payload.get("learning"):
        analysis = dict(previous)
    saved_criteria = (previous or {}).get("goal_assessment", {}).get("criteria", [])
    saved_assessment = engine_state.get("goal_assessment") or (check_goals(saved_criteria, sources, include_history=same_materials)
                                                              if goal_checks and saved_criteria else {})
    if not analysis and saved_assessment:
        analysis = {"goal_assessment": saved_assessment, "experiment_goal": (previous or {}).get("experiment_goal", ""),
                    "verification_targets": (previous or {}).get("verification_targets", []),
                    "summary": model.get("message", "本轮分析尚未完成。"), "outcome": "investigating"}
    if analysis:
        current_failure = any(citation["source_id"] == "message:current" for item in analysis.get("hypotheses", []) for citation in item["evidence"])
        current_failure |= any(row["source_id"] == "message:current" and not row["passed"] for tool in model["tool_trace"]
                               if tool["tool"] == "check_submitted_pings" and tool["status"] == "ok" for row in tool["result"]["pings"])
        goal_pending = analysis.get("goal_assessment", {}).get("checks", [])
        goal_failure = any(item["status"] == "failed" for item in goal_pending)
        if not comparison_reply and not payload.get("learning") and previous_closed and same_materials and not current_failure and not goal_failure and analysis.get("outcome") not in RESOLVED_OUTCOMES and analysis.get("outcome") != "goals_pending":
            analysis["outcome"] = previous["outcome"]
            analysis["verification"] = previous["verification"]
        closed = analysis.get("outcome") in RESOLVED_OUTCOMES
        if closed:
            analysis["hypotheses"], analysis["evidence_requests"] = [], []
        pending = analysis.get("evidence_requests", [])
        if not comparison_reply and not closed and analysis.get("outcome") != "ready_for_retest" and not pending and payload.get("interaction", {}).get("type") in ("ask", "repair"):
            pending = (previous or {}).get("pending_requests", (previous or {}).get("evidence_requests", []))
        analysis["pending_requests"] = pending
        analysis["experiment_goal"] = ((previous or {}).get("experiment_goal") or
                                       (previous or {}).get("experiment_state", {}).get("goal") or
                                       analysis.get("experiment_goal") or experiment_goal(payload))
        if not analysis.get("verification_targets"):
            analysis["verification_targets"] = (previous or {}).get("verification_targets", [])
        ground_investigation_targets(analysis, submitted_host_addresses(payload, sources))
        if closed:
            analysis["resolution_summary"] = ((previous.get("resolution_summary") or previous.get("summary"))
                                              if previous_closed and previous["outcome"] == analysis["outcome"] and same_materials and not current_failure else analysis["summary"])
    return analysis


def finalize_submission(result, context, assessment, sources, payload, previous_materials, *, route_table_answer=False, grounded=False, pending_config=False):
    route_selection = list(context.get("route_selection", {}).values())
    if assessment:
        result["goal_assessment"] = assessment
        result["verification_targets"] = list(dict.fromkeys(result["verification_targets"] + [goal["target"] for goal in context["criteria"] if goal["kind"] == "reachability"]))
        pending = [item for item in assessment["checks"] if item["kind"] != "reachability" and item["status"] != "passed"]
        if result["outcome"] == "user_reported_resolved" and pending:
            result["outcome"] = "goals_pending"
            result["evidence_requests"] = [item["request"] for item in pending]
            result["summary"] = result["reply"] = "访问恢复反馈已记录。实验目标还需完成：" + "；".join(item["detail"] for item in pending)
    if context.get("plan") and not route_table_answer:
        result["diagnostic_plan"] = context["plan"]
        selected = context["plan"]["selected"]
        if selected and not grounded and result["outcome"] == "investigating":
            result["evidence_requests"] = [{"device": selected["device"], "information": selected["command"],
                                            "why": selected["purpose"] + "。" + context["plan"]["reason"]}]
    if route_selection and result["outcome"] == "investigating" and not pending_config:
        requests = [lookup["request"] for lookup in route_selection if lookup["request"]]
        for request in result["evidence_requests"]:
            if request not in requests:
                requests.append(request)
        result["evidence_requests"] = requests[:6]
    if not pending_config and result["outcome"] in {"investigating", "ready_for_retest"}:
        missing_routes = [lookup for lookup in route_selection if lookup["status"] in {"no_match", "observed"}]
        for check in assessment.get("checks", []):
            if check["kind"] == "route_protocol":
                lookup = check_route_selection(check["device"], check["target"],
                                               evidence_blocks(sources, include_history=payload["configs"] == previous_materials))
                if lookup["status"] in {"no_match", "observed"}:
                    missing_routes.append(lookup)
        result["evidence_requests"] = [request for request in result["evidence_requests"]
                                       if not covered_current_route_request(request, missing_routes)]
    return result


def _submission_texts(result):
    yield from (result.get("summary", ""), result.get("reply", ""), *result.get("steps", []))
    for item in result.get("hypotheses", []):
        yield item["title"]
        yield item["reasoning"]


def _mentions(text, device):
    return bool(re.search(r"(?<![\w.-])" + re.escape(device) + r"(?![\w.-])", text, re.I))


def _scoped_elsewhere(text, device, devices):
    named = list(devices) + re.findall(r"\b(?:R|SW|AR|LSW|FW|PC|SRV)\d+\b", text, re.I)
    return not _mentions(text, device) and any(_mentions(text, name) for name in named)


def _clauses(text):
    return re.split(r"[。；;\n]", text)


def _uncertain(text):
    return bool(re.search(r"可能|尚不能|无法(?:确定|判断|确认)|不能(?:确定|判断|确认|认定)|是否|待(?:确认|核对)|不代表|不等于|并非|没有证据", text))


def verify_excerpt_claims(result, sources, seen):
    """Missing commands in an unread/partial snapshot cannot establish an absence."""
    blocks = evidence_blocks(sources)
    latest = {block["device"].casefold(): block for block in blocks if is_config_block(block)}
    missing = re.compile(r"漏配|未(?:配置|宣告|启用|加入)|没有.{0,12}(?:配置|宣告|加入)|缺少.{0,12}(?:配置|network|OSPF)|未[将把].{0,60}加入", re.I)
    all_devices = [block["device"] for block in latest.values()]
    for block in latest.values():
        if complete_config(block) and all((block["source_id"], n) in seen for n in range(block["start"] + 1, block["start"] + len(block["lines"]) + 1)):
            continue
        for text in _submission_texts(result):
            for clause in _clauses(text):
                additions = re.findall(r"\bnetwork\s+\d+(?:\.\d+){3}\s+\d+(?:\.\d+){3}", clause, re.I)
                new_statement = any(not any(command.casefold() == line.strip().casefold() for line in block["lines"])
                                    for command in additions)
                unproven_step = text in result.get("steps", []) and (new_statement or re.search(
                    r"(?:补配|补上|添加|新增|启用).{0,60}(?:OSPF|network)|(?:OSPF|network).{0,60}(?:补配|补上|添加|新增|启用)", clause, re.I))
                if not (missing.search(clause) or unproven_step) or _uncertain(clause):
                    continue
                if not _mentions(clause, block["device"]):
                    # A hypothesis can establish its device via its cited source.
                    cited = any(text in {item["title"], item["reasoning"]} and any(
                        ref["source_id"] == block["source_id"] for ref in item["evidence"])
                        for item in result.get("hypotheses", []))
                    if (not cited and len(all_devices) != 1) or _scoped_elsewhere(clause, block["device"], all_devices):
                        continue
                # A matching operational interface read can establish membership independently.
                operational = [row for row in blocks if row["device"].casefold() == block["device"].casefold()
                               and re.match(r"\s*display ospf(?:\s+\d+)?\s+interface\b", row["command"], re.I)]
                targets = re.findall(r"(?:LoopBack|GigabitEthernet|GE|Ethernet)\s*[\d/]+|\d+(?:\.\d+){3}", clause, re.I)
                if targets and any(any(re.search(r"(?<![\w./])" + re.escape(interface_id(target)) + r"(?![\w./])",
                                       re.sub(r"(?:GigabitEthernet|GE)\s*(?=\d)", "GE", line, flags=re.I), re.I)
                                       for target in targets)
                                   for row in operational for line in row["lines"] if not prompt_info(line)
                                   and all((row["source_id"], n) in seen for n in range(row["start"] + 1, row["start"] + len(row["lines"]) + 1))):
                    continue
                raise ValueError(f"{block['device']} 的配置为摘录或尚未读全，不能断言漏配/未宣告；请先读取完整配置或相关接口运行输出。")


def verify_minimal_ospf_wildcard_advice(result, sources):
    """Do not widen an existing exact match that already selects the primary IP."""
    latest = {block["device"].casefold(): block for block in evidence_blocks(sources) if is_config_block(block)}
    all_devices = [block["device"] for block in latest.values()]
    for block in latest.values():
        parsed = parse_config(block["device"], "\n".join(block["lines"]))
        addresses = {port.address for port in parsed.interfaces.values() if port.address}
        exact = {str(ipaddress.IPv4Address(base)) for process in parsed.processes.values()
                 for base, wildcard, _, _ in process.networks if wildcard == 0
                 and str(ipaddress.IPv4Address(base)) in addresses}
        for text in _submission_texts(result):
            for clause in _clauses(text):
                if re.search(r"无需|不必|不需要|不要|不能|并非|不是", clause):
                    continue
                if _scoped_elsewhere(clause, block["device"], all_devices):
                    continue
                for address in exact:
                    if (re.search(r"\bnetwork\s+" + re.escape(address) + r"\s+0\.0\.0\.255\b", clause, re.I)
                            or address in clause and "反掩码" in clause and "0.0.0.255" in clause):
                        raise ValueError(f"{block['device']} 的 network {address} 0.0.0.0 已精确匹配接口主IP，无需扩大反掩码；保留该语句，只修改有证据的问题。")
