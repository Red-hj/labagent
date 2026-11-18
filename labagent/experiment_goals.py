"""Evaluate cited lab requirements against submitted configuration and output.

Criteria describe successful experiments, rather than enumerating fault types.
Device prompts delimit mixed files; source lines are preserved in every check.
"""
import ipaddress
from pathlib import Path
import re

from .connectivity import parse_ping_records
from .runtime_parser import FRR_ROUTE_LEGEND, LINUX_ROUTE_COMMAND, LINUX_ROUTE_ROW, parse_frr_routes, parse_linux_routes, parse_routes
from .operation_history import TIME, prompt_info


KINDS = ("reachability", "route_protocol", "config_contains", "config_absent")
LABELS = {"passed": "已满足", "failed": "未满足", "needs_evidence": "待补充输出"}
SECTION_NUMBER = r"(?:(?:[一二三四五六七八九十百]+[、．.]|\d+[、．.]\s+)\s*)?"
DEVICE_SECTION = re.compile(r"^\s*" + SECTION_NUMBER + r"([A-Za-z][A-Za-z0-9_.-]*)\s*(?:的)?(?:相关|当前|最新)?\s*"
                            r"(配置(?:节选|摘录|片段)?|完整配置|地址信息|运行记录|运行结果|输出)\s*[:：]?\s*$")
DOCUMENT_SECTION = re.compile(r"^\s*(?:[一二三四五六七八九十百]+[、．.]|\d+[、．.]\s+)\s*\S.{0,65}$")

GOAL_TOOL = {"type": "function", "function": {
    "name": "check_experiment_goals",
    "description": "从实验说明建立有原文依据的实验目标，并用实际材料检查。首次传goals；后续传空对象沿用全部目标。返回Ping、VRP/FRRouting/Linux路由来源、配置命令的检查状态。实验要求与恢复连通性分别核对。",
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

ROUTE_TOOL = {"type": "function", "function": {
    "name": "check_route_selection",
    "description": "核对设备当前IPv4路由表中到指定IP或网段的路由来源和下一跳。仅使用实际运行输出；配置命令无法证明路由已选中。询问实际走哪个协议或下一跳时使用。",
    "parameters": {"type": "object", "properties": {"device": {"type": "string"}, "target": {"type": "string"}},
                   "required": ["device", "target"], "additionalProperties": False}}}


def evidence_blocks(sources, *, include_history=False):
    blocks = []
    for source_id, source in sources.items():
        if source["kind"] in {"user_question", "repair_run"}:
            continue
        if source["kind"] == "user_message" and source_id != "message:current" and not include_history:
            continue
        lines = [TIME.sub("", line) for line in source["lines"]]
        # A filename is only a fallback. Explicit device prompts take precedence.
        device = re.split(r"[-_\s]", Path(source["device"]).stem)[0]
        if source["kind"] == "screenshot_ocr" and not re.fullmatch(r"(?:R|SW|PC|AR|LSW|FW|SRV)\d+", device, re.I):
            device = source["device"]
        sysname = next((match for line in lines if (match := re.fullmatch(r"\s*sysname\s+(\S+)\s*", line, re.I))), None)
        if sysname:
            device = sysname[1]
        heading = next((match for line in lines[:3] if (match := re.match(r"\s*([A-Za-z][\w.-]*)\s*(?:的)?(?:最新)?(?:输出|运行结果)[：:]", line))), None)
        if heading:
            device = heading[1]
        default_device = device
        start, command, section = 0, source.get("command", ""), ""
        for index, line in enumerate(lines):
            prompt = prompt_info(line)
            heading = DEVICE_SECTION.fullmatch(line)
            boundary = DOCUMENT_SECTION.fullmatch(line)
            if prompt or heading or boundary:
                if index > start:
                    blocks.append(_block(source, device, command, start, index, section))
                start = index
                if prompt:
                    device, command, section = prompt[0], prompt[3], ""
                elif heading:
                    device, command = heading[1], ""
                    section = "configuration" if "配置" in heading[2] else ""
                else:
                    device, command, section = default_device, "", ""
        if len(lines) > start:
            blocks.append(_block(source, device, command, start, len(lines), section))
    return blocks


def _block(source, device, command, start, end, section=""):
    return {"source_id": source["source_id"], "device": device, "material": source["device"],
            "kind": source["kind"], "command": command, "section": section, "start": start,
            "lines": [TIME.sub("", line) for line in source["lines"][start:end]],
            "raw_lines": source["lines"][start:end]}


def citation(block, number):
    return {"source_id": block["source_id"], "device": block["material"],
            "line": block["start"] + number, "quote": block["raw_lines"][number - 1]}


def ping_device(block, row):
    named = re.match(r"\s*([A-Za-z][\w.-]*)\s*(?:执行)?\s+ping\b|\s*([A-Za-z][\w.-]*)执行ping\b", row["quote"], re.I)
    return (named[1] or named[2]) if named else block["device"]


def submitted_pings(sources):
    """Newest device/target block is current; older captures remain traceable."""
    current, reported, observations = {}, [], []
    for block in evidence_blocks(sources):
        grouped = {}
        for row in parse_ping_records(block["lines"]):
            device = ping_device(block, row)
            evidence = [citation(block, item["line"]) for item in row["evidence"]]
            item = {**row, "source_id":block["source_id"], "device":block["material"], "test_device":device,
                    "line":evidence[0]["line"], "line_end":evidence[-1]["line"], "quote":evidence[0]["quote"], "evidence":evidence}
            if block["kind"] == "user_message" and row["line_end"] == row["line"]:
                reported.append(item)
            else:
                observations.append(item)
                grouped.setdefault((device.casefold(), row["target"]), []).append(item)
        current.update(grouped)
    active = [item for items in current.values() for item in items]
    # Current raw output pasted in chat replaces the same target's older upload.
    targets = {item["target"] for item in active if item["source_id"] == "message:current"}
    active = [item for item in active if item["source_id"] == "message:current" or item["target"] not in targets]
    return active, reported, [item for item in observations if item not in active]


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
            device = ping_device(block, row)
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


def check_route_selection(device, target, blocks):
    network = ipaddress.IPv4Network(target, strict=False) if "/" in target else None
    address = ipaddress.IPv4Address(target) if network is None else None
    request = {"device": device, "information": "完整当前 IPv4 路由表（display ip routing-table、show ip route 或 ip -4 route show table main）",
               "why": f"核对到 {target} 的路由来源和下一跳"}
    result = {"device": device, "target": target, "status": "needs_evidence", "routes": [], "evidence": [],
              "detail": f"待补 {device} 当前路由表，以确认到 {target} 的实际路由来源和下一跳。", "request": request}

    for block in reversed(blocks):
        if block["device"].casefold() != device.casefold():
            continue
        command = " ".join(block["command"].lower().split())
        linux_command = bool(LINUX_ROUTE_COMMAND.fullmatch(command))
        if re.match(r"ip(?: -\d)? route\b", command) and not linux_command:
            continue  # Filtered, non-main and route-changing commands are not a main-table observation.
        if re.fullmatch(r"(?:display ip routing-table|show ip route)\s+(?!\d)[a-z].*", command):
            continue  # A protocol/VRF-filtered output cannot establish the forwarding choice.
        text = "\n".join(block["lines"])
        frr = FRR_ROUTE_LEGEND.search(text)
        vrp = re.search(r"Destination/Mask\s+Proto", text)
        linux = not frr and not vrp and (linux_command or not command and any(LINUX_ROUTE_ROW.fullmatch(line) for line in block["lines"]))
        if frr:
            header = next(i for i, line in enumerate(block["lines"], 1) if FRR_ROUTE_LEGEND.search(line))
        elif vrp:
            header = next(i for i, line in enumerate(block["lines"], 1) if "Destination/Mask" in line)
        elif linux:
            header = next((i for i, line in enumerate(block["lines"], 1) if LINUX_ROUTE_ROW.fullmatch(line)), 1)
        else:
            if command in {"display ip routing-table", "show ip route"}:
                result["evidence"] = [citation(block, 1)]
                break  # Never fall back to an older table after a new unsupported capture.
            continue
        try:
            rows = parse_frr_routes(text) if frr else parse_linux_routes(text) if linux else parse_routes(text, installed_only=True)
        except ValueError:
            rows = None
        matches = []
        for row in rows or []:
            prefix = ipaddress.IPv4Network(row["network"])
            if (network == prefix if network else address in prefix):
                matches.append((prefix.prefixlen, row))
        if not matches:
            result["evidence"] = [citation(block, header)]
            break
        longest = max(length for length, _ in matches)
        chosen = [row for length, row in matches if length == longest]
        if linux:
            metric = min(row["metric"] for row in chosen)
            chosen = [row for row in chosen if row["metric"] == metric]
        routes = [{key: row[key] for key in ("network", "protocol", "next_hop", "interface")} for row in chosen]
        summary = "、".join(dict.fromkeys(f"{route['network']} {route['protocol'].upper()}"
                           + (f" 经 {route['next_hop']}" if route["next_hop"] else "")
                           + (f"，出接口 {route['interface']}" if route["interface"] else "") for route in routes))
        return {**result, "status": "observed", "routes": routes, "evidence": [citation(block, row["line"]) for row in chosen],
                "detail": f"{device} 到 {target} 的路由表匹配项为 {summary}。", "request": None}
    return result


def _route(goal, blocks):
    result = check_route_selection(goal["device"], goal["target"], blocks)
    if result["status"] != "observed":
        return "needs_evidence", result["detail"], result["evidence"]
    actual = {route["protocol"].upper() for route in result["routes"]}
    if any(protocol == "UNKNOWN" or protocol.isdecimal() for protocol in actual):
        return "needs_evidence", result["detail"] + " 当前输出未提供可核对的协议名称，请补充带协议来源的路由表。", result["evidence"]
    expected = goal["expected"].upper()
    accepted = {"OSPF", "OSPF_ASE", "OSPF_NSSA"} if expected == "OSPF" else {expected}
    return ("passed" if actual <= accepted else "failed"), f"{result['detail']} 实验要求{expected}。", result["evidence"]


def _config(goal, blocks):
    candidates = [block for block in blocks if block.get("section") == "configuration" or "current-configuration" in block["command"].lower()
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
