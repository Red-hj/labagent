"""Evidence-backed network map and hop-by-hop explanations from lab materials."""
import ipaddress
import re

from .experiment_goals import evidence_blocks, citation, submitted_pings
from .runtime_parser import parse_routes
from .operation_history import inspect_history

IP = r"(?:\d{1,3}\.){3}\d{1,3}"
DEVICE = r"[A-Za-z][A-Za-z0-9_.-]{0,39}"

PATH_TOOL = {"type": "function", "function": {
    "name": "explain_network_path",
    "description": "从本轮材料重建网络地图，按源设备和目标IP逐步检查接口、网关、最长前缀路由和已提交的Ping。每一步附原文依据；可用于解释数据包在哪一步遇到问题。",
    "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "target": {"type": "string"}},
                   "required": ["source", "target"], "additionalProperties": False}}}


def build_map(sources):
    nodes, edges, arps = {}, {}, []

    def node(name):
        if name not in nodes:
            nodes[name] = {"id": name, "interfaces": [], "gateway": None, "routes": [],
                           "route_table": False, "evidence": []}
        return nodes[name]

    def evidence(n, ref):
        if ref not in n["evidence"]:
            n["evidence"].append(ref)

    def address(n, iface, value, mask, ref):
        try:
            parsed = ipaddress.IPv4Interface(value + "/" + mask)
        except ValueError:
            return
        entry = {"name": iface, "address": str(parsed.ip), "prefix": parsed.network.prefixlen,
                 "network": str(parsed.network), "shutdown": False, "evidence": [ref]}
        n["interfaces"] = [old for old in n["interfaces"] if old["name"] != iface] + [entry]
        evidence(n, ref)

    for block in evidence_blocks(sources):
        name = block["device"]
        # Configuration headings, prompts and ordinary device filenames establish ownership.
        known = bool(re.fullmatch(r"(?:R|SW|PC|AR|LSW|FW|SRV)\d+", name, re.I) or block["command"] or block["section"])
        current = node(name) if known else None
        iface = None
        host_ip = host_mask = None
        for number, raw in enumerate(block["lines"], 1):
            text = raw.strip()
            ref = citation(block, number)
            if match := re.fullmatch(r"(?:sysname|hostname)\s+(" + DEVICE + r")", text, re.I):
                current = node(match[1]); evidence(current, ref)
            if re.search(r"\s(?:--+|→|<->)\s", text):
                clean = re.sub(r"[（(][^）)]*[）)]", "", text)
                parts = re.split(r"\s*(?:--+|→|<->)\s*", clean)
                names = [m[1] for part in parts if (m := re.match(r"^(" + DEVICE + r")(?:的|\s|$)", part.strip()))]
                for left, right in zip(names, names[1:]):
                    if left == right:
                        continue
                    node(left); node(right)
                    key = tuple(sorted((left, right)))
                    edges[key] = {"source": left, "target": right, "kind": "declared", "label": "拓扑连线", "evidence": [ref]}
            if match := re.search(r"\b(" + DEVICE + r")地址(?:为|是|[:：])\s*(" + IP + r")/(\d+)", text):
                named = node(match[1]); address(named, "主机", match[2], match[3], ref)
                if gateway := re.search(r"默认网关(?:为|是|[:：])\s*(" + IP + r")", text):
                    named["gateway"] = {"address": gateway[1], "evidence": [ref]}
            if match := re.search(r"\b(" + DEVICE + r")(?:的)?ARP.*?(" + IP + r").*?(?:Incomplete|不完整)", text, re.I):
                # A row can mention several addresses; use the address immediately before Incomplete.
                before = re.split(r"Incomplete|不完整", text, flags=re.I)[0]
                ips = re.findall(IP, before)
                if ips:
                    arps.append({"device": match[1], "address": ips[-1], "state": "incomplete", "evidence": [ref]})
            if not current:
                continue
            if match := re.match(r"interface\s+(\S+)", text, re.I):
                iface = match[1]
            elif text in ("#", "!", "return", "quit", "exit"):
                iface = None
            elif iface and (match := re.fullmatch(r"ip address\s+(" + IP + r")(?:/(\d+)|\s+(" + IP + r"))", text, re.I)):
                address(current, iface, match[1], match[2] or match[3], ref)
            elif iface and text in ("shutdown", "undo shutdown", "no shutdown"):
                for entry in current["interfaces"]:
                    if entry["name"] == iface:
                        entry["shutdown"] = text == "shutdown"; entry["evidence"].append(ref)
            if match := re.search(r"(?:IPv4 Address|IP Address|IPv4地址|IP地址)\s*[:：.]?\s*(" + IP + r")", text, re.I):
                host_ip = (match[1], ref)
            if match := re.search(r"(?:Subnet Mask|子网掩码)\s*[:：.]?\s*(" + IP + r")", text, re.I):
                host_mask = match[1]
            if host_ip and host_mask:
                address(current, "主机", host_ip[0], host_mask, host_ip[1]); host_ip = None
            if match := re.search(r"(?:Default Gateway|默认网关)\s*[:：.]?\s*(" + IP + r")", text, re.I):
                current["gateway"] = {"address": match[1], "evidence": [ref]}
            if match := re.match(r"(" + IP + r"/\d+|default)\s+(?:via\s+(" + IP + r")\s+)?dev\s+(\S+)", text):
                current["route_table"] = True
                current["routes"].append({"network": "0.0.0.0/0" if match[1] == "default" else match[1],
                                          "next_hop": match[2], "interface": match[3], "protocol": "运行路由", "evidence": [ref]})
        if current:
            try:
                routes = parse_routes("\n".join(block["lines"]))
            except ValueError:
                routes = None
            if routes is not None:
                current["route_table"] = True
                current["routes"] = [{**row, "evidence": [citation(block, row["line"])]} for row in routes]
    pings, _, _ = submitted_pings(sources)
    for event in inspect_history(sources)["events"]:
        if re.fullmatch(DEVICE, event["device"]):
            current = node(event["device"])
            for ref in event["evidence"]:
                evidence(current, ref)
    for ping in pings:
        if re.fullmatch(DEVICE, ping["test_device"]):
            node(ping["test_device"])
    values = list(nodes.values())
    def declared_path(left, right):
        queue, visited = [(left, [left])], {left}
        while queue:
            name, path = queue.pop(0)
            if name == right:
                return path
            for edge in edges.values():
                if edge["kind"] != "declared":
                    continue
                other = edge["target"] if edge["source"] == name else edge["source"] if edge["target"] == name else None
                if other and other not in visited and (other == right or re.match(r"(?:SW|LSW)\d", other, re.I)):
                    visited.add(other); queue.append((other, path + [other]))
        return []
    for index, left in enumerate(values):
        for right in values[index + 1:]:
            key = tuple(sorted((left["id"], right["id"])))
            for a in left["interfaces"]:
                for b in right["interfaces"]:
                    if a["address"] != b["address"] and a["network"] == b["network"] and a["prefix"] < 32 and key not in edges and not declared_path(left["id"], right["id"]):
                        edges[key] = {"source": left["id"], "target": right["id"], "kind": "subnet", "label": a["network"],
                                      "evidence": a["evidence"] + b["evidence"]}
    return {"nodes": values, "links": list(edges.values()), "pings": pings, "arps": arps,
            "targets": sorted({row["target"] for row in pings} | {i["address"] for n in values for i in n["interfaces"]})}


def explain_path(network, source, target):
    try:
        destination = ipaddress.IPv4Address(target)
    except (ValueError, TypeError):
        raise ValueError("请输入目标IPv4地址") from None
    nodes = {n["id"]: n for n in network["nodes"]}
    if source not in nodes:
        raise ValueError("请选择地图中的源设备")
    steps, path = [], [source]

    def step(device, title, status, detail, refs=()):
        steps.append({"device": device, "title": title, "status": status, "detail": detail, "evidence": list(refs)})

    observed = [p for p in network["pings"] if p["test_device"].casefold() == source.casefold() and p["target"] == target]
    observation = observed[-1] if observed else None
    if observation and observation["passed"]:
        step(source, "端到端测试通过", "passed", f"{source} → {target}：{observation['sent']}发{observation['received']}收。", observation["evidence"])
    current = nodes[source]
    for _ in range(len(nodes) + 1):
        name = current["id"]
        if any(i["address"] == target for i in current["interfaces"]):
            step(name, "到达目标设备", "configured", f"目标地址{target}属于{name}。", next(i["evidence"] for i in current["interfaces"] if i["address"] == target))
            break
        direct = [i for i in current["interfaces"] if destination in ipaddress.IPv4Network(i["network"])]
        if direct:
            entry = max(direct, key=lambda i: i["prefix"])
            step(name, "目标位于直连网段", "failed" if entry["shutdown"] else "configured",
                 f"{entry['name']}连接{entry['network']}。" + ("接口配置为shutdown。" if entry["shutdown"] else ""), entry["evidence"])
            if entry["shutdown"]:
                break
            hop = target
        elif current["gateway"]:
            gateway = current["gateway"]
            hop = gateway["address"]
            step(name, "选择默认网关", "configured", f"跨网段访问交给{hop}。", gateway["evidence"])
            on_link = [i for i in current["interfaces"] if ipaddress.IPv4Address(hop) in ipaddress.IPv4Network(i["network"])]
            if current["interfaces"] and not on_link:
                step(name, "网关不在本地网段", "failed", f"{hop}不属于已提交的本机接口网段。", gateway["evidence"])
                break
        else:
            matching = [r for r in current["routes"] if destination in ipaddress.IPv4Network(r["network"], strict=False)]
            if not matching:
                if observation and observation["passed"]:
                    step(name, "路径细节", "unmapped", f"端到端Ping已通过；当前材料尚未展开{name}的具体下一跳。")
                else:
                    step(name, "检查目标路由", "failed" if current["route_table"] else "needs_evidence",
                         f"路由表中没有到{target}的匹配路由。" if current["route_table"] else f"补充{name}的路由表，继续确定到{target}的下一跳。")
                break
            route = max(matching, key=lambda r: ipaddress.IPv4Network(r["network"], strict=False).prefixlen)
            hop = route.get("next_hop") or target
            if hop == "0.0.0.0":
                hop = target
            step(name, "最长前缀路由匹配", "observed", f"{route['network']} · {route['protocol']} · 下一跳{hop}", route["evidence"])
        incomplete = next((a for a in reversed(network["arps"]) if a["device"] == name and a["address"] == hop), None)
        if incomplete and not (observation and observation["passed"]):
            step(name, "下一跳地址解析失败", "failed", f"{hop}的ARP状态为Incomplete，数据包停在下一跳解析。", incomplete["evidence"])
            break
        peers = [n for n in nodes.values() if n["id"] != name and any(i["address"] == hop for i in n["interfaces"])]
        if len(peers) != 1:
            step(name, "定位下一跳设备", "unmapped" if observation and observation["passed"] else "needs_evidence",
                 f"端到端Ping已通过；材料中尚未对应下一跳{hop}的设备信息。" if observation and observation["passed"] else f"补充地址{hop}所属设备的接口或ARP输出。")
            break
        peer = peers[0]
        if peer["id"] in path:
            step(name, "下一跳重复", "failed", f"下一跳回到{peer['id']}，路由形成循环。")
            break
        # Keep explicitly documented layer-2 switches visible between IP hops.
        queue, visited, physical = [(name, [name])], {name}, []
        while queue:
            device, chain = queue.pop(0)
            if device == peer["id"]:
                physical = chain; break
            for edge in network["links"]:
                if edge["kind"] != "declared":
                    continue
                other = edge["target"] if edge["source"] == device else edge["source"] if edge["target"] == device else None
                if other and other not in visited and (other == peer["id"] or re.match(r"(?:SW|LSW)\d", other, re.I)):
                    visited.add(other); queue.append((other, chain + [other]))
        path.extend(physical[1:] if physical else [peer["id"]]); current = peer
    if observation and not observation["passed"]:
        step(source, "端到端测试失败", "failed", f"{source} → {target}：{observation['sent']}发{observation['received']}收。", observation["evidence"])
    status = "passed" if observation and observation["passed"] else "failed" if any(s["status"] == "failed" for s in steps) else "needs_evidence"
    return {"source": source, "target": target, "status": status, "path": path, "steps": steps}
