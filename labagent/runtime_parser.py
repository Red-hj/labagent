"""Bounded VRP, FRRouting and Linux output parsing from submitted evidence."""
from copy import deepcopy
import ipaddress
import re

from .engine import fingerprint
from .parser import interface_id, parse_config
from .connectivity import parse_ping_records


FRR_ROUTE_LEGEND = re.compile(r">\s*-\s*selected route,\s*\*\s*-\s*FIB route", re.I)
FRR_ROUTE = re.compile(r"^\s*([A-Za-z])([>*qrbto ]*)\s+(\d+(?:\.\d+){3}/\d+)\s+(.*)$")
FRR_CONTINUATION = re.compile(r"^\s+\*\s+((?:via\s|is directly connected,).*)$")
FRR_PROTOCOLS = dict(K="Kernel", C="Direct", L="Local", S="Static", R="RIP", O="OSPF", I="ISIS", B="BGP", E="EIGRP", N="NHRP")
LINUX_ROUTE_COMMAND = re.compile(r"ip(?: -4)? route(?: (?:show|list))?(?: table (?:main|254))?", re.I)
LINUX_ROUTE_ROW = re.compile(r"\s*(default|\d+(?:\.\d+){3}(?:/\d+)?)\s+(.+)")
LINUX_PROTOCOLS = dict(kernel="Direct", static="Static", boot="Boot", ospf="OSPF", bgp="BGP", rip="RIP", isis="ISIS", dhcp="DHCP")


OSPF_NEIGHBOR_STATES = frozenset(("down", "attempt", "init", "2-way", "exstart", "exchange", "loading", "full"))
FRR_NEIGHBOR_HEADER = re.compile(r"\s*Neighbor\s+ID\s+Pri\s+State\s+.*\bAddress\s+Interface\b.*", re.I)
FRR_NEIGHBOR_ROW = re.compile(r"\s*(\d+(?:\.\d+){3})\s+\d+\s+([\w-]+)(?:/\S+)?"
                             r"\s+(?:\S+\s+){1,2}\d+(?:\.\d+){3}\s+\S+(?:\s+.*)?", re.I)


def parse_frr_neighbors(text):
    """Return rows for the last recognized FRRouting neighbor table, even if empty."""
    lines = text.splitlines()
    header = next((index for index in range(len(lines) - 1, -1, -1)
                   if FRR_NEIGHBOR_HEADER.fullmatch(lines[index])), None)
    if header is None:
        return None
    rows = []
    for number, line in enumerate(lines[header + 1:], header + 2):
        if match := FRR_NEIGHBOR_ROW.fullmatch(line):
            rows.append({"router_id": match[1], "state": match[2], "line": number, "peer_line": number})
    return rows


def parse_neighbors(text):
    records = []
    local_ip = peer = router_id = None
    peer_line = None
    for number, line in enumerate(text.splitlines(), 1):
        match = re.search(r"Area\s+[\d.]+\s+interface\s+([\d.]+)", line, re.I)
        if match:
            local_ip = match[1]
            peer = None
        match = re.search(r"Router ID:\s*([\d.]+)\s+Address:\s*([\d.]+)", line, re.I)
        if match:
            router_id, peer = match[1], match[2]
            peer_line = number
        match = re.search(r"\bState:\s*([\w-]+)", line, re.I)
        if match and local_ip and peer:
            records.append({"local_ip": local_ip, "peer_ip": peer, "router_id": router_id,
                            "state": match[1], "line": number, "peer_line": peer_line})
            peer = None
    return records


def parse_ping(text):
    records = parse_ping_records(text.splitlines())
    if len(records) != 1:
        return None
    return {key: records[0][key] for key in ("target", "sent", "received")}


def parse_routes(text, *, installed_only=False):
    if not re.search(r"Destination/Mask\s+Proto", text):
        return None
    records = []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"\s*(\d+\.\d+\.\d+\.\d+/\d+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+([\d.]+)\s+(\S+(?:[ \t]+\d+(?:/\d+)+)?)", line)
        if match:
            network = ipaddress.IPv4Network(match[1], strict=False)
            ipaddress.IPv4Address(match[6])
            if not installed_only or "D" in match[5].upper():
                records.append({"network": str(network), "protocol": match[2], "next_hop": match[6], "interface": match[7], "line": number, "text": line.strip()})
        elif re.match(r"\s*\d+\.\d+\.\d+\.\d+/", line):
            return None  # A partially parsed table must not prove a route absent.
    return records


def parse_frr_routes(text):
    if not FRR_ROUTE_LEGEND.search(text):
        return None
    records, current = [], None
    for number, line in enumerate(text.splitlines(), 1):
        row = FRR_ROUTE.fullmatch(line)
        if row:
            protocol, flags, prefix, detail = row.groups()
            network = str(ipaddress.IPv4Network(prefix))
            installed = ">" in flags and "*" in flags and "r" not in flags and protocol != "r"
            current = (network, FRR_PROTOCOLS.get(protocol, protocol)) if installed else None
        elif current and (continuation := FRR_CONTINUATION.fullmatch(line)):
            detail = continuation[1]
        else:
            current = None
            continue
        if not current:
            continue
        via = re.search(r"\bvia\s+([\d.]+),\s*(\S+)", detail)
        connected = re.search(r"is directly connected,\s*(\S+)", detail)
        gateway = str(ipaddress.IPv4Address(via[1])) if via else None
        interface = (via[2] if via else connected[1] if connected else "").rstrip(",")
        records.append({"network": current[0], "protocol": current[1], "next_hop": gateway,
                        "interface": interface, "line": number, "text": line.strip()})
    return records


def parse_linux_routes(text):
    """Read main-table IPv4 unicast rows, preserving metrics and exact line numbers.

    Unknown or incomplete rows invalidate the table rather than allowing a less
    specific route to hide an unsupported entry. A missing proto stays unknown.
    """
    records = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        prompt = re.fullmatch(r"\s*(?:<[\w.-]+>|[\w.-]+#)\s*(.*)", line)
        if LINUX_ROUTE_COMMAND.fullmatch(prompt[1] if prompt else line.strip()):
            continue
        row = LINUX_ROUTE_ROW.fullmatch(line)
        if not row:
            return None
        network = str(ipaddress.IPv4Network("0.0.0.0/0" if row[1] == "default" else row[1], strict=False))
        tokens, fields, position = row[2].split(), {}, 0
        flags = set()
        while position < len(tokens):
            key = tokens[position]
            if key in {"onlink", "linkdown"}:
                flags.add(key)
                position += 1
                continue
            if key not in {"via", "dev", "proto", "scope", "src", "metric", "nhid", "table", "mtu", "advmss", "pref"} or key in fields:
                return None
            position += 1
            if key == "via" and position < len(tokens) and tokens[position] == "inet":
                position += 1
            if position >= len(tokens):
                return None
            fields[key] = tokens[position]
            position += 1
        if not fields.get("dev") or fields.get("table", "main") not in {"main", "254"}:
            return None
        gateway = str(ipaddress.IPv4Address(fields["via"])) if "via" in fields else None
        if "src" in fields:
            ipaddress.IPv4Address(fields["src"])
        metric = fields.get("metric", "0")
        if not metric.isdecimal() or int(metric) > 2 ** 32 - 1:
            return None
        protocol = fields.get("proto", "Unknown")
        records.append({"network": network, "protocol": LINUX_PROTOCOLS.get(protocol.lower(), protocol),
                        "next_hop": gateway, "interface": fields["dev"], "metric": int(metric),
                        "link_down": "linkdown" in flags, "line": number, "text": line.strip()})
    return records


def select_route_records(rows, target, *, lowest_metric=False):
    """Use an exact destination for a network and longest match for a host."""
    matches = []
    for row in rows or []:
        prefix = ipaddress.IPv4Network(row["network"])
        if (prefix == target if isinstance(target, ipaddress.IPv4Network) else target in prefix):
            matches.append((prefix.prefixlen, row))
    if not matches:
        return []
    longest = max(length for length, _ in matches)
    chosen = [row for length, row in matches if length == longest]
    if lowest_metric:
        metric = min(row["metric"] for row in chosen)
        chosen = [row for row in chosen if row["metric"] == metric]
    return chosen


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
