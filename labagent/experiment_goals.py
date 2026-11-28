"""Evaluate cited lab requirements against submitted configuration and output.

Criteria describe successful experiments, rather than enumerating fault types.
Device prompts delimit mixed files; source lines are preserved in every check.
"""
from collections import Counter
import ipaddress
from pathlib import Path
import re

from .connectivity import IP, parse_ping_records
from .runtime_parser import (FRR_NEIGHBOR_COMMAND, FRR_NEIGHBOR_HEADER, FRR_ROUTE_LEGEND, LINUX_ROUTE_COMMAND, LINUX_ROUTE_ROW,
                             OSPF_NEIGHBOR_STATES,
                             parse_frr_neighbors, parse_frr_routes, parse_linux_routes, parse_neighbors,
                             parse_routes, select_route_records)
from .operation_history import TIME, prompt_info
from .parser import area_id, interface_id, ospf_bindings, parse_config


KINDS = ("reachability", "route_protocol", "config_contains", "config_absent", "ospf_interface_area", "ospf_neighbor")
LABELS = {"passed": "已满足", "failed": "未满足", "needs_evidence": "待补充输出"}
SECTION_NUMBER = r"(?:(?:[一二三四五六七八九十百]+[、．.]|\d+[、．.]\s+)\s*)?"
DEVICE_SECTION = re.compile(r"^\s*" + SECTION_NUMBER + r"([A-Za-z][A-Za-z0-9_.-]*)\s*(?:的)?(?:相关|当前|最新)?\s*"
                            r"(配置(?:节选|摘录|片段)?|完整配置|地址信息|运行记录|运行结果|输出)\s*[:：]?\s*$")
DOCUMENT_SECTION = re.compile(r"^\s*(?:[一二三四五六七八九十百]+[、．.]|\d+[、．.]\s+)\s*\S.{0,65}$")
OBSERVATION_DEVICE = re.compile(r"设备[：:]\s*([\w][\w.-]{0,119})\s*")
OBSERVATION_COMMAND = re.compile(r"命令[：:]\s*(\S.{0,499}?)\s*")
PING_ORIGIN = re.compile(
    r"\s*([A-Za-z][\w.-]*)(?:\s*[（(]\s*" + IP.pattern + r"\s*[）)])?(?:\s*执行\s*|\s+)ping\b", re.I
)
PARTIAL_CONFIG = re.compile(r"节选|摘录|片段|配置未提供|配置待补充|尚未提供|\b(?:excerpt|truncated|partial)\b|^\s*(?:\.{3}|…+)(?:\s|$)", re.I | re.M)
AREA_REQUIREMENT = re.compile(
    r"(?P<devices>[A-Z][\w.-]*(?:\s*[、,，]\s*[A-Z][\w.-]*)*)\s*的\s*"
    r"(?P<interface>[\w./-]+?)\s*(?:互联)?接口\s*(?:统一使用|必须(?:使用|位于|属于|在)|应(?:使用|位于|属于|在))\s*"
    r"Area\s+(?P<area>\d+(?:\.\d+){0,3})(?=\s|[。；，、,.!?！？;]|$)", re.I)
INSTRUCTION_FILE = re.compile(r"(?:实验|课程)(?:说明|要求|指导)|\b(?:instructions?|requirements?)\b", re.I)
VRP_TABLE_SCOPE = re.compile(r"\s*Routing Tables?\s*:\s*(.+?)\s*", re.I)
VRP_TABLE_COUNTS = re.compile(r"\s*Destinations\s*:\s*(\d{1,10})\s+Routes\s*:\s*(\d{1,10})\s*", re.I)
OUTPUT_EXCERPT = re.compile(r"节选|摘录|片段|截断|未显示|\b(?:excerpt|truncated|partial)\b|^\s*(?:\.{3,}|…+)|-{2,}\s*More\s*-{2,}", re.I | re.M)


OSPF_NEIGHBOR_COMMAND = re.compile(rf"(?:{FRR_NEIGHBOR_COMMAND}|display ospf(?:\s+\d+)? peer)", re.I)
OSPF_NEIGHBOR_WORDS = r"邻居|邻接|\b(?:neighbou?r|peer|adjacency)\b"
OSPF_NEIGHBOR_CLAUSE_BOUNDARY = re.compile(
    rf"[;；。!?！？]|\.(?=\s|$)|[,，](?=[^,，;；。!?！？]*(?:{OSPF_NEIGHBOR_WORDS}))", re.I)
OSPF_STATE_MENTION = re.compile(
    r"(?<![A-Za-z0-9_-])(?:" + "|".join(sorted(OSPF_NEIGHBOR_STATES)) + r")(?![A-Za-z0-9_-])", re.I)


def neighbor_instruction_supports_goal(instruction, device, router_id, expected):
    target_tokens = [re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(value.strip()) + r"(?![A-Za-z0-9_.-])", re.I)
                     for value in (device, router_id)]
    expected_states = {expected.strip().casefold()}
    for clause in OSPF_NEIGHBOR_CLAUSE_BOUNDARY.split(instruction):
        if (re.search(OSPF_NEIGHBOR_WORDS, clause, re.I)
                and all(token.search(clause) for token in target_tokens)
                and {state.casefold() for state in OSPF_STATE_MENTION.findall(clause)} == expected_states):
            return True
    return False


def area_instruction_supports_goal(instruction, device, interface, expected):
    compact = re.sub(r"(?i)GigabitEthernet(?=\d)", "GE", re.sub(r"(?i)(GigabitEthernet|GE)\s+(?=\d)", r"\1", instruction))
    def names(value):
        return re.search(r"(?<![A-Za-z0-9_./-])" + re.escape(value) + r"(?![A-Za-z0-9_./-])", compact, re.I)

    try:
        areas = {area_id(value) for value in re.findall(r"(?:\bArea|区域)\s*(\d+(?:\.\d+){0,3})(?![\d.])", instruction, re.I)}
        return bool(names(device) and names(interface_id(interface)) and areas == {area_id(expected.strip())})
    except ValueError:
        return False

GOAL_TOOL = {"type": "function", "function": {
    "name": "check_experiment_goals",
    "description": "从实验说明建立有原文依据的实验目标，并用实际材料检查。首次传goals；后续传空对象沿用全部目标。返回Ping、VRP/FRRouting/Linux路由来源、配置命令的检查状态。实验要求与恢复连通性分别核对。",
    "parameters": {"type": "object", "properties": {"goals": {"type": "array", "maxItems": 8, "items": {
        "type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS),
                     "description": "ospf_neighbor 核对运行邻居状态。device 为本机，target 为对端 Router ID，expected 为要求的状态，如 Full。"},
            "device": {"type": "string", "description": "从原文提取设备名；reachability可为空，其他类型必填。"},
            "target": {"type": "string", "description": "reachability填目的IP；route_protocol填目的IP或网段；config类型填命令前缀，ospf_interface_area填接口名，ospf_neighbor填对端Router ID。"},
            "expected": {"type": "string", "description": "route_protocol填协议，ospf_interface_area填区域号，ospf_neighbor填目标状态。其他类型填空字符串。"},
            "instruction": {"type": "object", "description": "填本轮已读要求的材料编号和行号，工具会保存完整原文。同一行有多个目标时可共用该行。",
                            "properties": {"source_id": {"type": "string"}, "line": {"type": "integer"},
                                           "quote": {"type": "string", "description": "可省略。提交时填写完整原文行。"}},
                            "required": ["source_id", "line"], "additionalProperties": False}},
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
        # Only infer a device from filenames; structured output already names its device.
        device = source["device"]
        if source["kind"] != "submitted_command_output":
            device = re.split(r"[-_\s]", Path(device).stem)[0]
            if source["kind"] == "screenshot_ocr" and not re.fullmatch(r"(?:R|SW|PC|AR|LSW|FW|SRV)\d+", device, re.I):
                device = source["device"]
        sysname = next((match for line in lines if (match := re.fullmatch(r"\s*sysname\s+(\S+)\s*", line, re.I))), None)
        if sysname:
            device = sysname[1]
        heading = next((match for line in lines[:3] if (match := re.match(r"\s*([A-Za-z][\w.-]*)\s*(?:的)?(?:最新)?(?:输出|运行结果)[：:]", line))), None)
        if heading:
            device = heading[1]
        start, command, section = 0, source.get("command", ""), ""
        if (len(lines) >= 3 and source["kind"] in {"submitted_material", "command_output"}
                and (named := OBSERVATION_DEVICE.fullmatch(lines[0]))
                and (invocation := OBSERVATION_COMMAND.fullmatch(lines[1]))):
            # Browser collection, internal MCP, and CLI upload all store the same two metadata lines.
            # They describe the first output block; leave them out of the device parser, retaining offsets.
            device, command, start = named[1], invocation[1], 2
        default_device = device
        for index in range(start, len(lines)):
            line = lines[index]
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
    complete = source.get("complete", False) and start == 0 and end == len(source["lines"])
    return {"source_id": source["source_id"], "device": device, "material": source["device"],
            "kind": source["kind"], "command": command, "section": section, "start": start, "complete": complete,
            "lines": [TIME.sub("", line) for line in source["lines"][start:end]],
            "raw_lines": source["lines"][start:end]}


def citation(block, number):
    return {"source_id": block["source_id"], "device": block["material"],
            "line": block["start"] + number, "quote": block["raw_lines"][number - 1]}


def ping_device(block, row):
    named = PING_ORIGIN.match(row["quote"])
    return named[1] if named else block["device"]


def _ping_observations(blocks):
    """Use the latest complete observation for each device, source address and destination."""
    current, reported, observations = {}, [], []
    for block in blocks:
        for row in parse_ping_records(block["lines"], command=block["command"]):
            device = ping_device(block, row)
            evidence = [citation(block, item["line"]) for item in row["evidence"]]
            item = {**row, "source_id": block["source_id"], "device": block["material"], "test_device": device,
                    "line": evidence[0]["line"], "line_end": evidence[-1]["line"], "quote": evidence[0]["quote"],
                    "evidence": evidence}
            if block["kind"] == "user_message" and row["line_end"] == row["line"]:
                reported.append(item)
            else:
                observations.append(item)
                current[(device.casefold(), (row["target"], row.get("source_address")))] = item
    from_message = [(path, item) for (_, path), item in current.items() if item["source_id"] == "message:current"]
    unnamed_paths = {path for path, item in from_message if item["test_device"] == item["device"]}
    named_paths = {path for path, item in from_message if item["test_device"] != item["device"]}
    earlier_counts = Counter(path for (_, path), item in current.items() if item["source_id"] != "message:current")
    # Preserve an unlabeled retest only when its source address and target have a single earlier origin.
    replaced_paths = {path for path in unnamed_paths - named_paths if earlier_counts[path] == 1}
    active = [item for (_, path), item in current.items()
              if item["source_id"] == "message:current" or path not in replaced_paths]
    active_refs = {(item["source_id"], item["line"]) for item in active}
    superseded = [item for item in observations if (item["source_id"], item["line"]) not in active_refs]
    return active, reported, superseded


def submitted_pings(sources):
    """Return current, reported and superseded Ping results with their evidence."""
    return _ping_observations(evidence_blocks(sources))


def check_goals(criteria, sources, *, include_history=False):
    # A different upload order can renumber a source while its quoted instruction stays the same.
    by_name = {source["device"]: source for source in sources.values()}
    rebound = []
    for goal in criteria:
        ref = goal["instruction"]
        source = by_name.get(ref.get("device"))
        if (source and source["source_id"] != ref["source_id"] and 0 < ref["line"] <= len(source["lines"])
                and source["lines"][ref["line"] - 1] == ref["quote"]):
            goal = {**goal, "instruction": {**ref, "source_id": source["source_id"]}}
        rebound.append(goal)
    criteria = rebound
    blocks = evidence_blocks(sources, include_history=include_history)
    explicit = explicit_area_goals(sources)
    area_lines = {(g["instruction"]["source_id"], g["instruction"]["line"]) for g in explicit}
    area_keys = {(g["device"].casefold(), interface_id(g["target"]), g["expected"]) for g in explicit}
    active = [g for g in criteria if not (
        (g["kind"] == "ospf_interface_area" and (g["device"].casefold(), interface_id(g["target"]), area_id(g["expected"].strip())) in area_keys)
        or (g["kind"] == "config_contains" and g["target"].strip().lower().startswith("interface ")
            and (g["instruction"]["source_id"], g["instruction"]["line"]) in area_lines))] + explicit
    pings = _ping_observations(blocks)[0] if any(goal["kind"] == "reachability" for goal in active) else []
    checks = []
    for goal in active:
        matching = [block for block in blocks if not goal["device"] or block["device"].casefold() == goal["device"].casefold()]
        if goal["kind"] == "reachability":
            status, detail, evidence = _ping(goal, pings)
        elif goal["kind"] == "route_protocol":
            status, detail, evidence = _route(goal, matching)
        elif goal["kind"] == "ospf_interface_area":
            status, detail, evidence = _ospf_interface_area(goal, matching)
        elif goal["kind"] == "ospf_neighbor":
            status, detail, evidence = _ospf_neighbor(goal, matching)
        else:
            status, detail, evidence = _config(goal, matching)
        checks.append({"id": goal["id"], "title": goal["title"], "kind": goal["kind"],
                       "device": goal["device"], "target": goal["target"], "expected": goal["expected"],
                       "status": status, "detail": detail, "evidence": evidence,
                       "instruction": goal["instruction"], "request": goal_request(goal)})
    return {"criteria": criteria, "checks": checks,
            "status": "passed" if checks and all(item["status"] == "passed" for item in checks) else "in_progress"}


def explicit_area_goals(sources):
    """Keep each endpoint of a common, explicitly worded lab requirement."""
    goals = {}
    for source_id, source in sources.items():
        if source_id != "question:initial" and not (source["kind"] == "submitted_material" and INSTRUCTION_FILE.search(source["device"])):
            continue
        for number, line in enumerate(source["lines"], 1):
            for match in AREA_REQUIREMENT.finditer(line):
                try:
                    area = area_id(match["area"])
                except ValueError:
                    continue
                interface = match["interface"]
                for device in re.split(r"\s*[、,，]\s*", match["devices"]):
                    key = device.casefold(), interface_id(interface), area
                    goals[key] = {"id": f"area:{device}:{interface}:{area}", "kind": "ospf_interface_area", "device": device,
                                  "title": f"{device} 的 {interface} 属于 OSPF Area {area}", "target": interface, "expected": area,
                                  "instruction": {"source_id": source_id, "device": source["device"], "line": number, "quote": line}}
                    if len(goals) == 8:
                        return list(goals.values())
    return list(goals.values())


def _ping(goal, pings):
    device = goal["device"].casefold()
    matching = [row for row in pings if row["target"] == goal["target"]
                and (not device or row["test_device"].casefold() == device)]
    if not matching:
        return "needs_evidence", f"补充{goal['device'] or '源设备'}到{goal['target']}的Ping收发统计。", []
    passed = all(row["passed"] for row in matching)
    evidence = [ref for row in matching for ref in row["evidence"]]
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
        public_table = [(i, match[1].casefold()) for i, line in enumerate(block["lines"], 1)
                        if (match := VRP_TABLE_SCOPE.fullmatch(line))]
        if vrp and public_table and (len(public_table) != 1 or public_table[0][1] not in {"public", "_public_"}):
            continue  # A named VPN table cannot establish the public forwarding choice.
        # A promptless FRRouting neighbor table also starts its data rows with IPs.
        linux = not frr and not vrp and (linux_command or not command
                and not any(FRR_NEIGHBOR_HEADER.fullmatch(line) for line in block["lines"])
                and any(LINUX_ROUTE_ROW.fullmatch(line) for line in block["lines"]))
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
            if vrp and not frr:
                all_rows = parse_routes(text)
                rows = parse_routes(text, installed_only=True) if all_rows is not None else None
            else:
                all_rows = None
                rows = parse_frr_routes(text) if frr else parse_linux_routes(text)
        except ValueError:
            rows = all_rows = None
        counts = [(i, int(match[1]), int(match[2])) for i, line in enumerate(block["lines"], 1)
                  if (match := VRP_TABLE_COUNTS.fullmatch(line))] if vrp and not frr else []
        complete = (command == "display ip routing-table" and len(public_table) == 1 and len(counts) == 1
                    and all_rows is not None and not OUTPUT_EXCERPT.search(text) and not OUTPUT_EXCERPT.search(block["material"])
                    and counts[0][1:] == (len({row["network"] for row in all_rows}), len(all_rows)))
        complete_linux = (linux and linux_command and block["complete"] and bool(rows)
                          and not OUTPUT_EXCERPT.search(text) and not OUTPUT_EXCERPT.search(block["material"]))
        chosen = select_route_records(rows, network if network is not None else address, lowest_metric=linux)
        if not chosen:
            result["evidence"] = [citation(block, header)]
            if complete or complete_linux:
                subject = f"没有匹配精确网段 {target} 的已安装路由" if network else f"没有可用于转发到 {target} 的已安装路由，包括默认路由"
                indices = {rows[0]["line"], rows[-1]["line"]} if complete_linux else {1, header, public_table[0][0], counts[0][0]}
                table_name = "完整 Linux IPv4 主路由表" if complete_linux else "完整公用 IPv4 路由表"
                return {**result, "status": "no_match", "request": None,
                        "evidence": [citation(block, i) for i in sorted(indices)],
                        "detail": f"{device} 当前{table_name}中，{subject}。"}
            if linux and rows:
                result["detail"] = f"{device} 已提交的 Linux 主路由表文本未列出到 {target} 的匹配项。请提供完整主表确认。"
            break
        down = [row for row in chosen if row.get("link_down")]
        if down:
            return {**result, "evidence": [citation(block, row["line"]) for row in down],
                    "detail": f"{device} 到 {target} 的匹配路由带有 linkdown，需核对接口状态与实际选路。",
                    "request": {**request, "information": "出接口当前链路状态及新采集的 IPv4 主路由表"}}
        routes = [{key: row[key] for key in ("network", "protocol", "next_hop", "interface")} for row in chosen]
        summary = "、".join(dict.fromkeys(f"{route['network']} {route['protocol'].upper()}"
                           + (f" 经 {route['next_hop']}" if route["next_hop"] else "")
                           + (f"，出接口 {route['interface']}" if route["interface"] else "") for route in routes))
        return {**result, "status": "observed", "routes": routes, "evidence": [citation(block, row["line"]) for row in chosen],
                "detail": f"{device} 到 {target} 的路由表匹配项为 {summary}。", "request": None}
    return result


def _route(goal, blocks):
    result = check_route_selection(goal["device"], goal["target"], blocks)
    if result["status"] == "no_match":
        return "failed", f"{result['detail']} 实验要求{goal['expected'].upper()}。", result["evidence"]
    if result["status"] != "observed":
        return "needs_evidence", result["detail"], result["evidence"]
    actual = {route["protocol"].upper() for route in result["routes"]}
    if any(protocol == "UNKNOWN" or protocol.isdecimal() for protocol in actual):
        return "needs_evidence", result["detail"] + " 当前输出未提供可核对的协议名称，请补充带协议来源的路由表。", result["evidence"]
    expected = goal["expected"].upper()
    accepted = {"OSPF", "OSPF_ASE", "OSPF_NSSA"} if expected == "OSPF" else {expected}
    return ("passed" if actual <= accepted else "failed"), f"{result['detail']} 实验要求{expected}。", result["evidence"]


def checked_frr_area_repair(assessment, sources):
    """Describe a narrow, evidenced repair for an explicitly allowed static fallback."""
    checks = assessment.get("checks", [])
    if len(checks) != 4:
        return None
    area = [g for g in checks if g["kind"] == "ospf_interface_area"]
    route = [g for g in checks if g["kind"] == "route_protocol"]
    ping = [g for g in checks if g["kind"] == "reachability"]
    if len(area) != 2 or len(route) != 1 or len(ping) != 1:
        return None
    if (route[0]["status"] != "failed" or route[0]["expected"].upper() != "OSPF"
            or ping[0]["status"] != "passed" or ping[0]["target"] != route[0]["target"]):
        return None
    good = next((g for g in area if g["status"] == "passed"), None)
    bad = next((g for g in area if g["status"] == "failed"), None)
    if not good or not bad or good["device"].casefold() != route[0]["device"].casefold() or good["device"].casefold() == bad["device"].casefold():
        return None
    explicit = explicit_area_goals(sources)
    if (not {good["id"], bad["id"]} <= {g["id"] for g in explicit}
            or good["instruction"] != bad["instruction"] or good["target"] != bad["target"] or good["expected"] != bad["expected"]):
        return None
    requirements = sources[bad["instruction"]["source_id"]]["lines"]
    # Match a standalone positive permission. Do not infer permission from a generic static route mention.
    allowance = [match[1] for line in requirements if (match := re.fullmatch(
        r"\s*允许保留管理距离\s*(\d{1,3})\s*的备用静态路由[。.]?\s*", line))]
    if len(allowance) != 1 or any(re.search(r"(?:禁止|不得|不允许|不保留|删除).{0,18}(?:静态路由|ip route(?:-static)?)", line, re.I)
                                  for line in requirements):
        return None
    actual_area = next((match[1] for ref in bad["evidence"] if (match := re.fullmatch(
        r"\s*ip ospf area\s+(\S+)\s*", ref["quote"], re.I))), None)
    good_area = any(re.fullmatch(r"\s*ip ospf area\s+\S+\s*", ref["quote"], re.I) for ref in good["evidence"])
    if not actual_area or not good_area:
        return None  # This repair sequence is for FRRouting interface commands only.
    installed = [re.fullmatch(r"\s*S>\*\s+\S+\s+\[(\d+)/\d+\]\s+via\s+\S+,\s*([^,\s]+).*", ref["quote"])
                 for ref in route[0]["evidence"]]
    if (len(installed) != 1 or not installed[0] or installed[0][1] != allowance[0]
            or installed[0][2].casefold() != good["target"].casefold()):
        return None
    peer, interface, expected, old = bad["device"], bad["target"], bad["expected"], area_id(actual_area)
    destination = ping[0]["target"]
    return {
        "summary": f"Ping 已通过，但静态备份正在转发；{peer} 的 {interface} 区域不达标。",
        "reply": (f"Ping 已通过，但 {good['device']} 到 {destination} 当前走管理距离 {allowance[0]} 的静态备份，"
                  f"还没有满足 OSPF 路由要求。{peer} 的 {interface} 在 Area {old}，实验要求 Area {expected}。"
                  f"只调整 {peer} 的这个接口区域，保留允许的备用静态路由。改后核对 OSPF 选路并复测 Ping。"),
        "steps": [f"{peer}：进入 configure terminal → interface {interface}",
                  f"执行 no ip ospf area {old}，再执行 ip ospf area {expected}",
                  f"{good['device']}：show ip route {destination}，确认选中的路由来自 OSPF",
                  f"从实验指定的源端重新 ping {destination}，核对收发统计"],
        "hypotheses": [], "evidence_requests": [], "outcome": "ready_for_retest",
    }


def is_config_block(block):
    command = " ".join(block["command"].lower().split())
    return (block.get("section") == "configuration" or command.startswith(("display current-configuration", "show running-config"))
            or any(re.match(r"\s*(?:sysname|hostname|frr version)\s+", line, re.I) for line in block["lines"])
            or Path(block["material"]).suffix.lower() in {".cfg", ".conf"} and not command)


def complete_config(block):
    command = " ".join(block["command"].lower().split())
    if ((command and command not in {"display current-configuration", "show running-config"})
            or PARTIAL_CONFIG.search(block["material"]) or PARTIAL_CONFIG.search("\n".join(block["lines"]))):
        return False
    last = next((line.strip().lower() for line in reversed(block["lines"]) if line.strip()), "")
    return last == "return" or last == "end" and any(re.match(r"\s*frr version\s+", line, re.I) for line in block["lines"])


def pending_ospf_loopback_evidence(sources, assessment):
    """Separate observed loss from a missing OSPF stanza in an explicit device excerpt."""
    pings, _, _ = submitted_pings(sources)
    failed_pings = [row for row in pings if not row["passed"]]
    if len(failed_pings) != 1 or len(pings) != 1:
        return None
    # The goal must be OSPF-related; a failed ping to an arbitrary address is not enough.
    target = failed_pings[0]["target"]
    goal_source = any(re.search(r"(?<![A-Za-z0-9_])OSPF(?![A-Za-z0-9_])", line, re.I)
                      and re.search(r"(?<![\d.])" + re.escape(target) + r"(?![\d.])", line)
                      for key, source in sources.items()
                      if key == "question:initial" or source["kind"] == "submitted_material" and INSTRUCTION_FILE.search(source["device"])
                      for line in source["lines"])
    if not goal_source:
        return None
    blocks = evidence_blocks(sources)
    for check in assessment.get("checks", []):
        if check["status"] != "failed" or check["kind"] == "reachability":
            continue
        if (check["kind"] != "route_protocol" or check["device"].casefold() != failed_pings[0]["test_device"].casefold()
                or ipaddress.IPv4Address(target) not in ipaddress.IPv4Network(check["target"], strict=False)
                or check_route_selection(check["device"], check["target"], blocks)["status"] != "no_match"):
            return None
    latest = {block["device"].casefold(): block for block in blocks if is_config_block(block)}
    owners = []
    for block in latest.values():
        text = "\n".join(block["lines"])
        if not re.search(r"^\s*sysname\s+\S+", text, re.I | re.M):
            continue
        device = parse_config(block["device"], text)
        owners.extend((block, device, port) for port in device.interfaces.values()
                      if port.address == target or (port.network and ipaddress.IPv4Address(target) in port.network))
    if len(owners) != 1:
        return None
    block, device, port = owners[0]
    if (device.name.casefold() == failed_pings[0]["test_device"].casefold() or port.shutdown
            or ospf_bindings(device, port) or complete_config(block)
            or not any(PARTIAL_CONFIG.search(value) for value in (block["material"], "\n".join(block["lines"])))):
        return None
    for output in blocks:
        if output["device"].casefold() != device.name.casefold() or not re.match(r"\s*display ospf(?:\s+\d+)?\s+interface\b", output["command"], re.I):
            continue
        if any(re.search(r"(?<![\w./-])" + re.escape(port.name) + r"(?![\w./-])", line, re.I)
               or re.search(r"(?<![\d.])" + re.escape(target) + r"(?![\d.])", line)
               for line in output["lines"] if not prompt_info(line)):
            return None  # Let the normal workflow interpret the newly supplied operational evidence.
    fact = f"{failed_pings[0]['test_device']} 到 {target} 的 Ping 发 {failed_pings[0]['sent']} 收 {failed_pings[0]['received']}，连通性未达标。"
    detail = (f"{device.name} 只提供了配置摘录，无法判断 {port.name} 是否已加入 OSPF。"
              f"请补 {device.name} 的完整 OSPF 配置，或该接口的 display ospf interface 输出，再确定改法。")
    return {"summary": fact, "reply": fact + detail, "steps": [], "hypotheses": [], "outcome": "investigating",
            "evidence_requests": [{"device": device.name, "information": f"完整 OSPF 配置，或 {port.name} 的 display ospf interface 输出",
                                   "why": f"确认 {target} 所在接口是否已加入 OSPF"}]}


def _config(goal, blocks):
    candidates = [block for block in blocks if is_config_block(block)]
    if not candidates:
        return "needs_evidence", f"补充{goal['device']}的当前配置。", []
    block = candidates[-1]
    prefix = " ".join(goal["target"].lower().split())
    found = [index for index, line in enumerate(block["lines"], 1)
             if (normalized := " ".join(line.lower().split())) == prefix or normalized.startswith(prefix + " ")]
    present = bool(found)
    complete = complete_config(block)
    if not present and not complete:
        return "needs_evidence", f"补充{goal['device']}完整当前配置，以核对命令“{goal['target']}”。", []
    passed = present if goal["kind"] == "config_contains" else not present
    evidence = [citation(block, index) for index in found[:3]]
    if not evidence:
        evidence = [citation(block, index) for index, line in enumerate(block["lines"], 1) if line.strip().lower() in {"return", "end"}][-1:]
    return ("passed" if passed else "failed"), f"{goal['device']}当前配置{'包含' if present else '没有'}命令“{goal['target']}”。", evidence


def _ospf_interface_area(goal, blocks):
    device, target, expected = goal["device"], goal["target"], area_id(goal["expected"].strip())
    candidates = [block for block in blocks if is_config_block(block)]
    pending = "needs_evidence", f"补充 {device} 的 {target} 接口配置及 OSPF 区域设置。", []
    if not candidates:
        return pending
    block = candidates[-1]
    wanted = interface_id(target)
    interface_line, matched, in_interface = None, [], False
    for number, raw in enumerate(block["lines"], 1):
        line = raw.strip()
        if match := re.fullmatch(r"interface\s+(.+)", line, re.I):
            in_interface = interface_id(match[1]) == wanted
            if in_interface:
                interface_line = number
            continue
        if re.fullmatch(r"[!#].*|exit|quit|end|return|router\s+.*|ospf\s+\d+.*", line, re.I):
            in_interface = False
        if in_interface and (match := re.fullmatch(r"(?:ip ospf area|ospf enable\s+\d+\s+area)\s+(\S+)", line, re.I)):
            try:
                matched.append((area_id(match[1]), [interface_line, number]))
            except ValueError:
                return pending
    if not matched and complete_config(block):
        parsed = parse_config(device, "\n".join(block["lines"]))
        interface = parsed.interfaces.get(wanted)
        if interface and not parsed.errors and not interface.unsupported and not any(p.unsupported for p in parsed.processes.values()):
            for _, area, evidence in ospf_bindings(parsed, interface):
                numbers = [interface.lines["interface"]["line"], interface.lines["ip"]["line"],
                           evidence["area_evidence"]["line"], evidence["line"]]
                matched.append((area, numbers))
    if not matched or len({area for area, _ in matched}) != 1:
        return pending
    actual = matched[0][0]
    refs = [citation(block, number) for number in sorted(set(matched[0][1]))]
    return ("passed" if actual == expected else "failed"), f"{device} 的 {target} 为 OSPF Area {actual}；实验要求 Area {expected}。", refs


def _ospf_neighbor(goal, blocks):
    device, router_id, expected = goal["device"], goal["target"], goal["expected"].strip()
    missing = f"补充 {device} 的 OSPF 邻居输出以核对 Router ID {router_id} 的状态。"
    for block in reversed(blocks):
        text = "\n".join(block["lines"])
        command = " ".join(block["command"].lower().split())
        frr_table = parse_frr_neighbors(text)
        peers = frr_table["rows"] if frr_table is not None else parse_neighbors(text)
        if frr_table is None and not peers and not OSPF_NEIGHBOR_COMMAND.fullmatch(command):
            continue
        # Use the latest recognized output for this device. An older Full row
        # cannot substitute for a target missing from the new capture.
        matches = [peer for peer in peers if peer["router_id"] == router_id]
        states = {peer["state"].casefold(): peer["state"] for peer in matches}
        if not states:
            if (frr_table is not None and frr_table["fully_parsed"] and block["complete"]
                    and command == FRR_NEIGHBOR_COMMAND and not OUTPUT_EXCERPT.search(text)):
                lines = [frr_table["header_line"]]
                if peers:
                    lines.extend(sorted({peers[0]["line"], peers[-1]["line"]}))
                evidence = [citation(block, number) for number in lines]
                return "failed", f"{device} 的完整 FRRouting OSPF 邻居表中没有 Router ID {router_id}。", evidence
            break
        evidence = [citation(block, number) for number in sorted({
            peer[key] for peer in matches for key in ("line", "peer_line")
        })]
        if len(states) != 1 or not states.keys() <= OSPF_NEIGHBOR_STATES:
            return "needs_evidence", f"{device} 的 Router ID {router_id} 状态不明确。{missing}", evidence
        actual = next(iter(states.values()))
        status = "passed" if actual.casefold() == expected.casefold() else "failed"
        return status, f"{device} 的 OSPF 邻居 {router_id} 为 {actual}，要求为 {expected}。", evidence
    return "needs_evidence", missing, []


def goal_request(goal):
    if goal["kind"] == "reachability":
        information = f"ping {goal['target']} 的完整收发统计"
    elif goal["kind"] == "route_protocol":
        information = f"display ip routing-table {goal['target'].split('/')[0]}"
    elif goal["kind"] == "ospf_interface_area":
        information = f"{goal['target']} 接口配置与 OSPF 区域设置"
    elif goal["kind"] == "ospf_neighbor":
        information = "OSPF 邻居表（FRRouting 用 show ip ospf neighbor，VRP 用 display ospf peer）"
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
        raise ValueError("该实验目标须指定设备")
    if goal["kind"] == "ospf_neighbor":
        ipaddress.IPv4Address(goal["target"])
        if goal["expected"].strip().casefold() not in OSPF_NEIGHBOR_STATES:
            raise ValueError("请填写有效的 OSPF 邻居目标状态，例如 Full")
    if goal["kind"] == "route_protocol" and not goal["expected"].strip():
        raise ValueError("请填写实验要求的路由协议")
    if goal["kind"] == "ospf_interface_area":
        area_id(goal["expected"].strip())
