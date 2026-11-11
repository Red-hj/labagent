"""Bounded VRP command-output parsing. Inputs remain user-supplied evidence."""
from copy import deepcopy
import ipaddress
import re

from .engine import fingerprint
from .parser import interface_id, parse_config


def parse_neighbors(text):
    records = []
    local_ip = peer = None
    peer_line = None
    for number, line in enumerate(text.splitlines(), 1):
        match = re.search(r"Area\s+[\d.]+\s+interface\s+([\d.]+)", line, re.I)
        if match:
            local_ip = match[1]
            peer = None
        match = re.search(r"Router ID:\s*([\d.]+)\s+Address:\s*([\d.]+)", line, re.I)
        if match:
            peer = match[2]
            peer_line = number
        match = re.search(r"\bState:\s*(\w+)", line, re.I)
        if match and local_ip and peer:
            records.append({"local_ip": local_ip, "peer_ip": peer, "state": match[1], "line": number, "peer_line": peer_line})
            peer = None
    return records


def parse_ping(text):
    target = re.search(r"\bPING\s+([\d.]+)\s*:", text, re.I)
    sent = re.search(r"(\d+)\s+packet\(s\)\s+transmitted", text, re.I)
    received = re.search(r"(\d+)\s+packet\(s\)\s+received", text, re.I)
    if not (target and sent and received):
        return None
    ipaddress.IPv4Address(target[1])
    if int(received[1]) > int(sent[1]) or int(sent[1]) == 0:
        return None
    return {"target": target[1], "sent": int(sent[1]), "received": int(received[1])}


def parse_routes(text):
    if not re.search(r"Destination/Mask\s+Proto", text):
        return None
    records = []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"\s*(\d+\.\d+\.\d+\.\d+/\d+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+([\d.]+)\s+(\S+)", line)
        if match:
            network = ipaddress.IPv4Network(match[1], strict=False)
            ipaddress.IPv4Address(match[6])
            records.append({"network": str(network), "protocol": match[2], "next_hop": match[6], "interface": match[7], "line": number, "text": line.strip()})
        elif re.match(r"\s*\d+\.\d+\.\d+\.\d+/", line):
            return None  # A partially parsed table must not prove a route absent.
    return records


def normalize_outputs(payload):
    """Only use outputs explicitly tied to this configuration fingerprint."""
    prepared = deepcopy(payload)
    digest = fingerprint(payload)
    observations = prepared.setdefault("observations", {})
    grouped = {}
    routes = {}
    trace = []
    warnings = []
    devices = {name: parse_config(name, text) for name, text in payload["configs"].items()}
    eligible = []
    for index, output in enumerate(payload.get("command_outputs", []), 1):
        if output.get("config_fingerprint") != digest:
            warnings.append(f"命令输出 {index} 未绑定当前配置，未用作本次运行证据。")
            continue
        if output.get("device") not in devices and not output.get("command", "").lower().startswith("ping"):
            warnings.append(f"命令输出 {index} 的设备配置缺失。")
            continue
        eligible.append((index, output))
    if eligible:
        # Do not rebind old hand-entered observations when new raw output arrives.
        if observations and observations.get("config_fingerprint") != digest:
            observations = {"neighbors": [], "pings": []}
            prepared["observations"] = observations
        observations["config_fingerprint"] = digest
    for index, output in eligible:
        command = output["command"].strip().lower()
        evidence = {"output_index": index, "device": output["device"], "command": output["command"], "source": "user_submitted_command_output", "kind": output.get("kind", "user_submitted")}
        try:
            if command == "display ospf peer":
                parsed = parse_neighbors(output["text"])
                mapped = 0
                for row in parsed:
                    for link in payload["topology"]["links"]:
                        ends = link["endpoints"]
                        local = next((e for e in ends if e["device"] == output["device"]), None)
                        remote = next((e for e in ends if e["device"] != output["device"]), None)
                        if not local or not remote:
                            continue
                        left = devices.get(local["device"])
                        right = devices.get(remote["device"])
                        li = left.interfaces.get(interface_id(local["interface"])) if left else None
                        ri = right.interfaces.get(interface_id(remote["interface"])) if right else None
                        if li and ri and li.address == row["local_ip"] and ri.address == row["peer_ip"]:
                            grouped.setdefault(link["id"], []).append({**row, **evidence})
                            mapped += 1
                trace.append({**evidence, "parsed_rows": len(parsed), "mapped_rows": mapped})
                if not mapped:
                    warnings.append(f"命令输出 {index} 未找到可映射到互联接口的邻居记录；不能据此确认邻居正常。")
            elif command.startswith("ping "):
                row = parse_ping(output["text"])
                if row:
                    observations.setdefault("pings", []).append({**row, "source": output["device"], "evidence": evidence})
                    trace.append({**evidence, "parsed_rows": 1})
                else:
                    warnings.append(f"命令输出 {index} 的 Ping 汇总格式尚不支持。")
            elif command == "display ip routing-table":
                rows = parse_routes(output["text"])
                if output["device"] in routes:
                    routes[output["device"]] = None
                    warnings.append(f"设备 {output['device']} 有重复路由表，请只提交当前的一份。")
                else:
                    routes[output["device"]] = {"rows": rows, "complete": output.get("complete") is True, "evidence": evidence}
                trace.append({**evidence, "parsed_rows": len(rows) if rows is not None else 0})
            else:
                warnings.append(f"命令输出 {index} 的命令尚不支持：{output['command']}")
        except ValueError:
            warnings.append(f"命令输出 {index} 含无效地址，未采用。")
    for link_id, rows in grouped.items():
        bad = next((row["state"] for row in rows if row["state"].lower() != "full"), None)
        observations.setdefault("neighbors", []).append({"link_id": link_id, "state": bad or "Full", "evidence": rows})
    route_checks, route_findings, missing = [], [], []
    for query in payload.get("routing_queries", []):
        table = routes.get(query["device"])
        label = f"{query['device']} 到 {query['target']} 的完整路由表证据"
        if not table or table["rows"] is None or not table["complete"]:
            missing.append(label)
            continue
        address = ipaddress.IPv4Address(query["target"])
        matches = [row for row in table["rows"] if address in ipaddress.IPv4Network(row["network"])]
        match = max(matches, key=lambda row: ipaddress.IPv4Network(row["network"]).prefixlen) if matches else None
        check = {"check": "route_lookup", "device": query["device"], "target": query["target"], "status": "PASS" if match else "FAIL", "source": "user_submitted_command_output", "evidence": [table["evidence"]], "route": match}
        route_checks.append(check)
        if not match:
            route_findings.append({"code": "ROUTE_MISSING", "message": f"{query['device']} 提交的完整路由表没有到 {query['target']} 的匹配路由", "devices": [query["device"]], "evidence": [table["evidence"]], "severity": "error"})
    return prepared, {"trace": trace, "warnings": warnings, "runtime_checks": route_checks, "findings": route_findings, "missing": missing}
