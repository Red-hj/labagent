"""Evaluate cited lab requirements against submitted configuration and output.

Criteria describe successful experiments, rather than enumerating fault types.
Device prompts delimit mixed files; source lines are preserved in every check.
"""
import ipaddress
from pathlib import Path
import re

from .connectivity import parse_ping_records
from .runtime_parser import parse_routes
from .operation_history import TIME


KINDS = ("reachability", "route_protocol", "config_contains", "config_absent")
LABELS = {"passed": "已满足", "failed": "未满足", "needs_evidence": "待补充输出"}

GOAL_TOOL = {"type": "function", "function": {
    "name": "check_experiment_goals",
    "description": "从实验说明建立有原文依据的实验目标，并用实际材料检查。首次传goals；后续传空对象沿用全部目标。返回Ping、VRP路由来源、配置命令的检查状态。实验要求与恢复连通性分别核对。",
    "parameters": {"type": "object", "properties": {"goals": {"type": "array", "maxItems": 8, "items": {
        "type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
            "device": {"type": "string", "description": "从原文提取设备名；reachability可为空，其他类型必填。"},
            "target": {"type": "string", "description": "reachability填目的IP；route_protocol填目的IP或网段；config类型填命令前缀。"},
            "expected": {"type": "string", "description": "route_protocol填要求的协议，如OSPF；其他类型填空字符串。"},
            "instruction": {"type": "object", "properties": {"source_id": {"type": "string"}, "line": {"type": "integer"}, "quote": {"type": "string"}},
                            "required": ["source_id", "line", "quote"], "additionalProperties": False}},
        "required": ["id", "title", "kind", "device", "target", "expected", "instruction"], "additionalProperties": False}}},
        "additionalProperties": False}}}


def evidence_blocks(sources, *, include_history=False):
    blocks = []
    for source_id, source in sources.items():
        if source["kind"] == "user_question":
            continue
        if source["kind"] == "user_message" and source_id != "message:current" and not include_history:
            continue
        lines = [TIME.sub("", line) for line in source["lines"]]
        # A filename is only a fallback. Explicit device prompts take precedence.
        device = re.split(r"[-_\s]", Path(source["device"]).stem)[0]
        sysname = next((match for line in lines if (match := re.fullmatch(r"\s*sysname\s+(\S+)\s*", line, re.I))), None)
        if sysname:
            device = sysname[1]
        heading = next((match for line in lines[:3] if (match := re.match(r"\s*([A-Za-z][\w.-]*)\s*(?:的)?(?:最新)?(?:输出|运行结果)[：:]", line))), None)
        if heading:
            device = heading[1]
        start, command = 0, source.get("command", "")
        for index, line in enumerate(lines):
            prompt = re.match(r"^\s*<([\w.-]+)>\s*(.*)$", line)
            if prompt:
                if index > start:
                    blocks.append(_block(source, device, command, start, index))
                start, device, command = index, prompt[1], prompt[2]
        if len(lines) > start:
            blocks.append(_block(source, device, command, start, len(lines)))
    return blocks


def _block(source, device, command, start, end):
    return {"source_id": source["source_id"], "device": device, "material": source["device"],
            "kind": source["kind"], "command": command, "start": start,
            "lines": [TIME.sub("", line) for line in source["lines"][start:end]],
            "raw_lines": source["lines"][start:end]}


def citation(block, number):
    return {"source_id": block["source_id"], "device": block["material"],
            "line": block["start"] + number, "quote": block["raw_lines"][number - 1]}


def check_goals(criteria, sources, *, include_history=False):
    blocks = evidence_blocks(sources, include_history=include_history)
    checks = []
    for goal in criteria:
        matching = [block for block in blocks if not goal["device"] or block["device"].casefold() == goal["device"].casefold()]
        if goal["kind"] == "reachability":
            status, detail, evidence = _ping(goal, blocks)
        elif goal["kind"] == "route_protocol":
            status, detail, evidence = _route(goal, matching)
        else:
            status, detail, evidence = _config(goal, matching)
        checks.append({"id": goal["id"], "title": goal["title"], "kind": goal["kind"],
                       "device": goal["device"], "target": goal["target"], "expected": goal["expected"],
                       "status": status, "detail": detail, "evidence": evidence,
                       "instruction": goal["instruction"], "request": goal_request(goal)})
    return {"criteria": criteria, "checks": checks,
            "status": "passed" if checks and all(item["status"] == "passed" for item in checks) else "in_progress"}


def _ping(goal, blocks):
    records = []
    for block in blocks:
        rows = [row for row in parse_ping_records(block["lines"]) if row["target"] == goal["target"]]
        # A sentence in chat is a report; a multi-line output is an observation.
        rows = [row for row in rows if block["kind"] != "user_message" or row["line_end"] > row["line"]]
        grouped = {}
        for row in rows:
            named = re.match(r"\s*([A-Za-z][\w.-]*)\s*(?:执行)?\s+ping\b|\s*([A-Za-z][\w.-]*)执行ping\b", row["quote"], re.I)
            device = (named[1] or named[2]) if named else block["device"]
            if not goal["device"] or goal["device"].casefold() == device.casefold():
                grouped.setdefault(device.casefold(), []).append(row)
        records.extend((device, block, items) for device, items in grouped.items())
    if not records:
        return "needs_evidence", f"补充{goal['device'] or '源设备'}到{goal['target']}的Ping收发统计。", []
    # Newer blocks replace the same device's older test, not other devices' tests.
    latest = {device: (block, rows) for device, block, rows in records}
    evidence, passed = [], True
    for block, rows in latest.values():
        passed &= all(row["passed"] for row in rows)
        evidence += [citation(block, item["line"]) for row in rows for item in row["evidence"]]
    return ("passed" if passed else "failed"), f"到{goal['target']}的Ping{'全部通过' if passed else '仍有丢包'}。", evidence


def _route(goal, blocks):
    tables = []
    for block in blocks:
        try:
            rows = parse_routes("\n".join(block["lines"]))
        except ValueError:
            rows = None
        if rows is not None:
            tables.append((block, rows))
    if not tables:
        return "needs_evidence", f"补充{goal['device']}的VRP路由表，核对到{goal['target']}的路由来源。", []
    # A route is meaningful for a specific device; require it in the criterion.
    block, rows = tables[-1]
    if "/" in goal["target"]:
        network = ipaddress.ip_network(goal["target"], strict=False)
        matches = [row for row in rows if ipaddress.ip_network(row["network"]) == network]
    else:
        target = ipaddress.ip_address(goal["target"])
        matches = [row for row in rows if target in ipaddress.ip_network(row["network"])]
    if not matches:
        header = next(index for index, line in enumerate(block["lines"], 1) if "Destination/Mask" in line)
        return "needs_evidence", f"当前表中未找到{goal['target']}；补充包含该目标的完整路由输出。", [citation(block, header)]
    route = max(matches, key=lambda row: ipaddress.ip_network(row["network"]).prefixlen)
    actual, expected = route["protocol"].upper(), goal["expected"].upper()
    passed = actual == expected or (expected == "OSPF" and actual in {"OSPF_ASE", "OSPF_NSSA"})
    return ("passed" if passed else "failed"), f"{goal['device']}到{goal['target']}使用{actual}，实验要求{expected}。", [citation(block, route["line"])]


def _config(goal, blocks):
    candidates = [block for block in blocks if "current-configuration" in block["command"].lower()
                  or any(re.match(r"\s*sysname\s+", line, re.I) for line in block["lines"])
                  or Path(block["material"]).suffix.lower() in {".cfg", ".conf"} and not block["command"]]
    if not candidates:
        return "needs_evidence", f"补充{goal['device']}的当前配置。", []
    block = candidates[-1]
    prefix = " ".join(goal["target"].lower().split())
    found = [index for index, line in enumerate(block["lines"], 1)
             if (normalized := " ".join(line.lower().split())) == prefix or normalized.startswith(prefix + " ")]
    present = bool(found)
    complete = any(line.strip().lower() == "return" for line in block["lines"])
    if not present and not complete:
        return "needs_evidence", f"补充{goal['device']}完整当前配置，以核对命令“{goal['target']}”。", []
    passed = present if goal["kind"] == "config_contains" else not present
    evidence = [citation(block, index) for index in found[:3]]
    if not evidence:
        evidence = [citation(block, index) for index, line in enumerate(block["lines"], 1) if line.strip().lower() == "return"]
    return ("passed" if passed else "failed"), f"{goal['device']}当前配置{'包含' if present else '没有'}命令“{goal['target']}”。", evidence


def goal_request(goal):
    if goal["kind"] == "reachability":
        information = f"ping {goal['target']} 的完整收发统计"
    elif goal["kind"] == "route_protocol":
        information = f"display ip routing-table {goal['target'].split('/')[0]}"
    else:
        information = "display current-configuration"
    return {"device": goal["device"] or "源设备", "information": information,
            "why": f"核对实验要求：{goal['title']}"}


def validate_goal_fields(goal):
    keys = {"id", "title", "kind", "device", "target", "expected", "instruction"}
    if not isinstance(goal, dict) or set(goal) != keys:
        raise ValueError("实验目标须包含id、title、kind、device、target、expected、instruction")
    if any(not isinstance(goal[key], str) or len(goal[key]) > 300 for key in keys - {"instruction"}):
        raise ValueError("实验目标字段须为300字以内文本")
    if not all(goal[key].strip() for key in ("id", "title", "kind", "target")) or goal["kind"] not in KINDS:
        raise ValueError("请使用支持的实验目标类型和具体参数")
    if goal["kind"] in {"reachability", "route_protocol"}:
        ipaddress.IPv4Network(goal["target"] if "/" in goal["target"] else goal["target"] + "/32", strict=False)
        if goal["kind"] == "reachability" and "/" in goal["target"]:
            raise ValueError("Ping目标须为单个地址")
    if goal["kind"] != "reachability" and not goal["device"].strip():
        raise ValueError("路由和配置目标须指定设备")
    if goal["kind"] == "route_protocol" and not goal["expected"].strip():
        raise ValueError("请填写实验要求的路由协议")
