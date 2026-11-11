"""Deterministic checks. No network command is executed or simulated here."""
from __future__ import annotations

from .parser import area_id, interface_id, ospf_bindings


def finding(code, message, devices, evidence=None, severity="error", **details):
    return {"code": code, "message": message, "devices": devices,
            "evidence": evidence or [], "severity": severity, **details}


def area_evidence(binding):
    return binding[2].get("area_evidence", binding[2])


def check_link(link, devices, expected_area=None):
    problems, unknowns, checks = [], [], []
    names = [endpoint["device"] for endpoint in link["endpoints"]]
    resolved = []
    for endpoint in link["endpoints"]:
        device = devices.get(endpoint["device"])
        if device is None:
            unknowns.append(finding("MISSING_DEVICE", "缺少设备配置", [endpoint["device"]], severity="unknown"))
            continue
        interface = device.interfaces.get(interface_id(endpoint["interface"]))
        if interface is None:
            unknowns.append(finding("MISSING_INTERFACE", f"找不到接口 {endpoint['interface']}", [device.name], severity="unknown"))
            continue
        resolved.append((device, interface))
        if interface.shutdown:
            problems.append(finding("INTERFACE_SHUTDOWN", f"{device.name} 的 {interface.name} 被关闭", [device.name], [interface.lines["state"]]))
        if not interface.address:
            unknowns.append(finding("MISSING_IP", f"{device.name} 的互连接口缺少可解析的主 IP", [device.name], severity="unknown"))
    if len(resolved) != 2:
        return problems, unknowns, checks
    (left, li), (right, ri) = resolved
    if li.address and ri.address:
        evidence = [li.lines["ip"], ri.lines["ip"]]
        if li.address == ri.address:
            problems.append(finding("DUPLICATE_IP", "互连接口使用了相同 IP", names, evidence))
        elif li.network != ri.network:
            problems.append(finding("SUBNET_MISMATCH", "互连接口网段或掩码不一致", names, evidence))
        else:
            checks.append({"check": "interface_subnet", "status": "PASS", "evidence": evidence})
    if not link.get("ospf", True):
        return problems, unknowns, checks
    bindings = []
    for device, interface in resolved:
        options = ospf_bindings(device, interface)
        if interface.unsupported:
            unknowns.append(finding("UNSUPPORTED_CONFIG", "接口包含尚未支持的 IP/OSPF 配置，需要人工确认", [device.name], interface.unsupported, "unknown"))
        if len(options) > 1:
            unknowns.append(finding("AMBIGUOUS_OSPF_BINDING", "接口匹配多个 OSPF 进程或区域", [device.name], [option[2] for option in options], "unknown"))
            bindings.append(None)
            continue
        if not options:
            # An absent IP or unsupported syntax cannot prove OSPF is disabled.
            unsupported = [item for process in device.processes.values() for item in process.unsupported]
            if unsupported:
                unknowns.append(finding("UNSUPPORTED_CONFIG", "进程包含未支持的配置，无法确认接口是否启用 OSPF", [device.name], unsupported, "unknown"))
            if interface.address and not interface.unsupported and not unsupported:
                problems.append(finding("OSPF_NOT_ENABLED", f"{device.name} 的互连接口未匹配任何 OSPF 启用配置", [device.name], [interface.lines["ip"]]))
            bindings.append(None)
            continue
        binding = options[0]
        bindings.append(binding)
        process = device.processes.get(binding[0])
        if process is None:
            unknowns.append(finding("MISSING_OSPF_PROCESS", "接口引用的 OSPF 进程未出现在配置中", [device.name], [binding[2]], "unknown"))
        else:
            if "all" in process.silent or interface_id(interface.name) in process.silent:
                key = "silent:all" if "all" in process.silent else "silent:" + interface_id(interface.name)
                problems.append(finding("SILENT_INTERFACE", "互连接口被设置为 OSPF 静默接口", [device.name], [process.lines[key]]))
            if process.unsupported:
                unknowns.append(finding("UNSUPPORTED_CONFIG", "进程包含尚未支持的 OSPF 配置，需要人工确认", [device.name], process.unsupported, "unknown"))
        required = link.get("expected_area", expected_area)
        if required is not None and binding[1] != area_id(str(required)):
            problems.append(finding("AREA_POLICY_VIOLATION", f"{device.name} 的区域不符合本实验要求", [device.name], [area_evidence(binding)], expected=area_id(str(required)), actual=binding[1]))
    if all(bindings):
        if bindings[0][1] != bindings[1][1]:
            problems.append(finding("AREA_MISMATCH", "同一互联链路两端的 OSPF 区域不一致", names, [area_evidence(binding) for binding in bindings]))
        else:
            checks.append({"check": "ospf_area", "status": "PASS", "area": bindings[0][1], "evidence": [area_evidence(binding) for binding in bindings]})
        router_ids = [device.processes[binding[0]].router_id if binding[0] in device.processes else None
                      for (device, _), binding in zip(resolved, bindings)]
        if router_ids[0] and router_ids[0] == router_ids[1]:
            problems.append(finding("DUPLICATE_ROUTER_ID", "邻接两端显式配置了相同的 Router ID", names,
                                    [device.processes[binding[0]].lines["router_id"] for (device, _), binding in zip(resolved, bindings)]))
        if (li.hello, li.dead) != (ri.hello, ri.dead):
            problems.append(finding("TIMER_MISMATCH", "两端 OSPF Hello/Dead 定时器不一致", names,
                                    [item for interface in (li, ri) for key, item in interface.lines.items() if key in ("hello", "dead")]))
    return problems, unknowns, checks


def verify_runtime(topology, observations):
    checks, missing, failed = [], [], []
    observations = observations or {}
    for link in topology["links"]:
        if not link.get("ospf", True):
            continue
        rows = [row for row in observations.get("neighbors", []) if row.get("link_id") == link["id"]]
        if len(rows) != 1:
            missing.append(f"链路 {link['id']} 的唯一邻居状态记录")
            continue
        row = rows[0]
        ok = row.get("state", "").lower() == "full"
        checks.append({"check": "ospf_neighbor", "link_id": link["id"], "status": "PASS" if ok else "FAIL", "source": "user_submitted", "observation": row})
        if not ok:
            failed.append(finding("NEIGHBOR_NOT_FULL", f"链路 {link['id']} 的用户提交邻居状态不是 Full", [e["device"] for e in link["endpoints"]], [row]))
    targets = topology.get("ping_targets", [])
    if not targets:
        missing.append("拓扑中需要验证的端到端 Ping 目标")
    for target in targets:
        rows = [row for row in observations.get("pings", [])
                if row.get("source") == target["source"] and row.get("target") == target["target"]]
        if len(rows) != 1:
            missing.append(f"{target['source']} → {target['target']} 的唯一 Ping 记录")
            continue
        row = rows[0]
        sent, received = row.get("sent"), row.get("received")
        valid = type(sent) is int and type(received) is int and sent > 0 and 0 <= received <= sent
        ok = valid and received == sent
        if not valid:
            missing.append(f"{target['source']} → {target['target']} 的合法收发包数量")
        else:
            checks.append({"check": "end_to_end_ping", "status": "PASS" if ok else "FAIL", "source": "user_submitted", "observation": row})
            if not ok:
                failed.append(finding("PING_FAILED", "用户提交的端到端 Ping 仍存在丢包", [], [row]))
    return checks, missing, failed
