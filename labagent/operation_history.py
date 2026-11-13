"""Reconstruct CLI attempts, observed views and errors with original line evidence."""
import re

TIME = re.compile(r"^\s*\[?(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\]?\s*")
VRP = re.compile(r"^\s*(?:<([^>]+)>|\[([^\]]+)\])\s*(.*)$")
IOS = re.compile(r"^\s*([\w.-]+)(\([^)]*\))?[#>]\s*(.*)$")
ERROR = re.compile(r"^\s*(?:Error\s*:|%\s*(?:Invalid|Error|Incomplete|Ambiguous)|Unrecognized command|Wrong parameter|Too many parameters|Incomplete command|Ambiguous command|错误[：:]|无效命令)", re.I)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07)")
VIEW_LABELS = {"user": "用户视图", "system": "系统视图", "interface": "接口视图", "ospf": "OSPF进程视图", "ospf_area": "OSPF区域视图", "router": "路由协议视图", "other": "配置视图"}


def timestamp(value):
    parts = value.replace(",", ".").split(":")
    return sum(float(part) * 60 ** index for index, part in enumerate(reversed(parts)))


def time_label(seconds):
    if seconds is None:
        return "无时间戳"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


def prompt_info(text):
    match = VRP.match(text)
    if match:
        inner = match[1] or match[2]
        # Match the view suffix, preserving hyphens in actual device names.
        suffix = re.search(r"-(?:ospf-\d+(?:-area-[\w.]+)?|(?:GigabitEthernet|Ethernet|GE|LoopBack|Vlanif|Tunnel|Eth-Trunk)[\w/.-]*|interface[^\s]*)$", inner, re.I)
        device = inner[:suffix.start()] if suffix else inner
        view = "user" if match[1] else "ospf_area" if re.search(r"-ospf-\d+-area-", inner, re.I) else "ospf" if re.search(r"-ospf-\d+$", inner, re.I) else "interface" if suffix else "system"
        return device, view, inner, match[3], "vrp"
    match = IOS.match(text)
    if match:
        mode = match[2] or ""
        view = "interface" if "config-if" in mode else "router" if "config-router" in mode else "system" if mode == "(config)" else "other" if mode else "user"
        return match[1], view, match[1] + mode, match[3], "ios"
    return None


def _category(command):
    lower = command.lower().strip()
    if lower.startswith(("display ", "show ")):
        return "inspect"
    if lower.startswith(("ping ", "tracert ", "traceroute ")):
        return "test"
    if lower in {"system-view", "quit", "return", "exit", "end", "configure terminal"} or lower.startswith(("interface ", "area ", "ospf ", "router ospf")):
        return "navigation"
    return "configure"


def _effect(event):
    """Record CLI-accepted changes; later snapshots are separate observations."""
    command = " ".join(event["command"].lower().split())
    undo = command.startswith(("undo ", "no "))
    base = command.split(" ", 1)[1] if undo else command
    if base.startswith("ip address "):
        key = event["prompt"] + ":ip address"
    elif base in {"shutdown"}:
        key = event["prompt"] + ":shutdown"
    elif base.startswith(("network ", "ospf enable ")):
        key = event["prompt"] + ":" + base
    else:
        return None
    return {"device": event["device"], "key": key, "command": event["command"], "operation": "remove" if undo else "set",
            "event_id": event["id"], "seconds": event["seconds"], "basis": "cli_return", "evidence": event["evidence"]}


def parse_source(source):
    events, pending = [], None
    current_time = None
    for number, original in enumerate(source["lines"], 1):
        if original == "[敏感内容已省略]":
            pending = None
            continue
        text = ANSI.sub("", original).replace("\r", "")
        stamp = TIME.match(text)
        if stamp:
            current_time = timestamp(stamp[1])
            text = text[stamp.end():]
        info = prompt_info(text)
        quote = {"source_id": source["source_id"], "device": source["device"], "line": number, "quote": original}
        if info:
            device, view, prompt, command, dialect = info
            if pending and pending["device"] == device and pending["status"] == "issued":
                pending["status"] = "returned"
                pending["next_view"] = view
                pending["response_evidence"].append(quote)
            pending = None
            if not command.strip():
                continue
            # Complete display-current-configuration files may contain [R1] labels;
            # only a prompt followed by an actual command creates an event.
            pending = {"id": f"{source['source_id']}:{number}", "source_id": source["source_id"], "material": source["device"],
                       "line": number, "seconds": current_time, "time_label": time_label(current_time), "device": device,
                       "view": view, "view_label": VIEW_LABELS[view], "prompt": prompt, "dialect": dialect,
                       "command": command.strip(), "category": _category(command), "status": "issued", "recovered": False,
                       "evidence": [quote], "response_evidence": [], "explanation": ""}
            events.append(pending)
        elif pending and ERROR.search(text):
            pending["status"] = "error"
            pending["response_evidence"].append(quote)
            pending["error"] = text.strip()
            if pending["dialect"] == "vrp" and re.match(r"(?:undo\s+)?network\s+", pending["command"], re.I) and pending["view"] != "ospf_area":
                pending["explanation"] = f"network命令在{pending['view_label']}输入，终端返回报错。进入实验规定的OSPF区域视图后重新输入。"
            else:
                pending["explanation"] = "这条命令收到终端报错，后续操作应先处理该报错。"
        elif pending and text.strip() and len(pending["response_evidence"]) < 6 and text.strip() != "^":
            pending["response_evidence"].append(quote)
    for index, event in enumerate(events):
        if event["status"] == "error":
            retried = next((later for later in events[index + 1:] if later["device"] == event["device"] and
                            " ".join(later["command"].lower().split()) == " ".join(event["command"].lower().split()) and later["status"] == "returned"), None)
            if retried:
                event["recovered"] = True
                event["retry_event_id"] = retried["id"]
    return events


def inspect_history(sources):
    events = []
    for source in sources.values():
        if source["kind"] in {"user_question", "user_message"} and source["source_id"] != "message:current":
            continue
        events.extend(parse_source(source))
    ids = {event["id"]: f"op{index:03d}" for index, event in enumerate(events, 1)}
    for event in events:
        event["id"] = ids[event["id"]]
        if event.get("retry_event_id"):
            event["retry_event_id"] = ids[event["retry_event_id"]]
    errors = [event for event in events if event["status"] == "error"]
    unresolved = [event for event in errors if not event["recovered"]]
    focus = next(iter(unresolved or errors), None)
    changes = [effect for event in events if event["status"] == "returned" and (effect := _effect(event))]
    latest_changes = {change["key"]: change for change in changes}
    return {"events": events[:400], "event_count": len(events), "error_count": len(errors),
            "unresolved_error_count": len(unresolved), "first_issue_id": focus["id"] if focus else None,
            "devices": list(dict.fromkeys(event["device"] for event in events)), "changes": changes,
            "latest_changes": list(latest_changes.values()), "source": "terminal_history"}


HISTORY_TOOL = {"type": "function", "function": {"name": "inspect_operation_timeline",
    "description": "读取终端操作时间线，核对设备、实际配置视图、命令报错及后续重试。返回事件编号与原文。使用事件的error和response_evidence区分命令输入、终端返回和报错；结合当前配置判断对实验的影响。",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def model_history(history):
    important = [event for event in history["events"] if event["status"] == "error"]
    ids = {event["id"] for event in important}
    selected = important[:8] + [event for event in history["events"] if event["id"] not in ids][-12:]
    notes = []
    if any(re.match(r"(?:undo\s+)?network\s+", event["command"], re.I) for event in history["events"]):
        notes.append("OSPF network的地址与反掩码用于匹配参与OSPF的接口。0.0.0.0表示精确匹配接口主IP，通告路由前缀还取决于接口掩码与OSPF网络类型；不能将它直接解释为通告/32。")
    return {key: history[key] for key in ("event_count", "error_count", "unresolved_error_count", "first_issue_id", "devices")} | {"events": selected, "protocol_notes": notes}
