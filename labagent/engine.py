"""Public diagnosis contract, independent of UI and future cloud adapter."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import ipaddress

from .knowledge import search, validate_scope
from .parser import area_id, parse_config
from .tools import check_link, finding, verify_runtime


HINTS = {
    "ROUTE_MISSING": ("检查目标地址的去程和回程路由。", "提交的完整路由表没有匹配目标地址；邻居正常不代表端到端路径完整。", "按实验要求检查目标网段宣告与路由学习，修正后采集新的邻居、路由表和 Ping 输出。"),
    "AREA_MISMATCH": ("比较互联两端的 OSPF 区域。", "查看证据中的两个 area 配置，并对照实验要求。", "将互联链路两端区域调整为实验规定的区域，再查看邻居和远端路由。"),
    "AREA_POLICY_VIOLATION": ("检查区域是否符合本次实验要求。", "邻居能否建立与是否满足实验目标是两个检查项。", "依据结果中的 expected/actual 调整相应接口的区域归属。"),
    "INTERFACE_SHUTDOWN": ("先检查接口管理状态。", "证据指出接口配置了 shutdown。", "在实验设备上启用对应接口，再检查物理状态、邻居和端到端通信。"),
    "SUBNET_MISMATCH": ("比较互联接口的 IP 和掩码。", "互联两端应使用同一子网中的不同主机地址。", "按实验地址规划修正 IP/掩码，然后重新提交配置和运行结果。"),
    "DUPLICATE_IP": ("检查互联接口是否使用重复地址。", "两个接口的主 IP 相同。", "依据地址规划改为同一子网内不同的主机地址。"),
    "OSPF_NOT_ENABLED": ("检查互联接口是否参与 OSPF。", "确认 network 是否匹配接口主 IP，或是否配置接口级 ospf enable。", "使目标接口在正确进程和区域中启用 OSPF，然后重新检查邻居。"),
    "SILENT_INTERFACE": ("检查互联接口是否被设置为静默接口。", "silent-interface 会影响该接口发送 OSPF Hello。", "按实验要求取消互连接口的静默设置，再重新检查邻居。"),
    "DUPLICATE_ROUTER_ID": ("比较两端 Router ID。", "工具发现两端显式配置的 Router ID 相同。", "按实验规划配置不同 Router ID，并按设备要求确认运行值已生效。"),
    "TIMER_MISMATCH": ("比较两端 OSPF Hello/Dead 定时器。", "两端定时器配置不匹配；未配置时本地按普通广播/点到点实验默认值检查。", "统一两端定时器后重新检查邻居状态。"),
    "NEIGHBOR_NOT_FULL": ("运行信息表明邻居尚未达到预期状态。", "进一步收集接口状态、邻居输出和路由表。", "结合新增运行证据继续排查；不要仅因静态配置通过就认定恢复。"),
    "PING_FAILED": ("邻居状态之外，还要检查端到端通信。", "检查 PC 网关、远端路由和回程路径。", "收集两端路由表和终端网关配置，确认完整路径后再次 Ping。"),
}


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求必须是 JSON 对象")
    mode = payload.get("diagnostic_mode", "ospf_rules")
    if mode == "open_network":
        from .investigation import validate_open
        return validate_open(payload)
    if mode != "ospf_rules":
        raise ValueError("diagnostic_mode 必须为 ospf_rules 或 open_network")
    validate_scope(payload.get("knowledge_scope", "default"))
    configs = payload.get("configs")
    if not isinstance(configs, dict) or not 1 <= len(configs) <= 20 or not all(isinstance(k, str) and k and isinstance(v, str) and len(v) <= 200_000 for k, v in configs.items()):
        raise ValueError("configs 应为 1～20 台设备的名称到配置文本映射")
    topology = payload.get("topology")
    if not isinstance(topology, dict) or not isinstance(topology.get("links"), list) or not 1 <= len(topology["links"]) <= 40:
        raise ValueError("topology.links 必须包含 1～40 条互联链路")
    ids = set()
    for link in topology["links"]:
        if not isinstance(link, dict) or not isinstance(link.get("id"), str) or not link["id"] or link["id"] in ids:
            raise ValueError("每条链路需要唯一的非空字符串 id")
        ids.add(link["id"])
        endpoints = link.get("endpoints")
        if not isinstance(endpoints, list) or len(endpoints) != 2 or not all(isinstance(e, dict) and all(isinstance(e.get(k), str) and e[k] for k in ("device", "interface")) for e in endpoints):
            raise ValueError("每条链路需要两个包含 device 和 interface 的端点")
        if endpoints[0]["device"] == endpoints[1]["device"]:
            raise ValueError("互联链路的两端必须属于不同设备")
        if "ospf" in link and type(link["ospf"]) is not bool:
            raise ValueError("ospf 必须是布尔值")
        if "expected_area" in link:
            area_id(str(link["expected_area"]))
    targets = topology.get("ping_targets", [])
    if not isinstance(targets, list) or len(targets) > 40 or not all(isinstance(t, dict) and all(isinstance(t.get(k), str) and t[k] for k in ("source", "target")) for t in targets):
        raise ValueError("ping_targets 需要包含 source 和 target")
    if len({(t["source"], t["target"]) for t in targets}) != len(targets):
        raise ValueError("Ping 目标不能重复")
    requirements = payload.get("requirements", {})
    if not isinstance(requirements, dict):
        raise ValueError("requirements 必须是对象")
    if "expected_area" in requirements:
        area_id(str(requirements["expected_area"]))
    if type(payload.get("hint_level", 1)) is not int or not 1 <= payload.get("hint_level", 1) <= 3:
        raise ValueError("hint_level 必须为 1、2 或 3")
    if not isinstance(payload.get("question", ""), str) or len(payload.get("question", "")) > 10000:
        raise ValueError("question 必须是长度不超过 10000 的文本")
    if type(payload.get("use_llm", False)) is not bool:
        raise ValueError("use_llm 必须是布尔值")
    observations = payload.get("observations", {})
    if not isinstance(observations, dict):
        raise ValueError("observations 必须是对象")
    for key in ("neighbors", "pings"):
        rows = observations.get(key, [])
        if not isinstance(rows, list) or len(rows) > 100 or not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"observations.{key} 必须是记录数组")
    for row in observations.get("neighbors", []):
        if not isinstance(row.get("link_id"), str) or not isinstance(row.get("state"), str):
            raise ValueError("邻居观测需要字符串 link_id 和 state")
    outputs = payload.get("command_outputs", [])
    if payload.get("evidence_kind", "user_submitted") not in ("demo", "user_submitted"):
        raise ValueError("evidence_kind 必须为 demo 或 user_submitted")
    if not isinstance(outputs, list) or len(outputs) > 60:
        raise ValueError("command_outputs 必须为最多60条命令输出")
    for output in outputs:
        if not isinstance(output, dict) or not all(isinstance(output.get(k), str) for k in ("device", "command", "text")) or len(output["text"]) > 100000:
            raise ValueError("命令输出需要 device、command、text 字符串，单条不超过100KB")
        if "complete" in output and type(output["complete"]) is not bool:
            raise ValueError("complete 必须为布尔值")
    queries = payload.get("routing_queries", [])
    if not isinstance(queries, list) or len(queries) > 40:
        raise ValueError("routing_queries 必须为最多40条路由检查目标")
    for query in queries:
        if not isinstance(query, dict) or not isinstance(query.get("device"), str) or not isinstance(query.get("target"), str):
            raise ValueError("路由目标需要 device 和 target")
        ipaddress.IPv4Address(query["target"])


def fingerprint(payload):
    content = {key: payload.get(key, {}) for key in ("configs", "topology", "requirements")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def diagnose(payload, *, _inspection=None, _runtime=None, _explain=True, _context=None):
    validate(payload)
    if payload.get("diagnostic_mode") == "open_network":
        from .investigation import investigate
        return investigate(payload)
    trace = []
    def record(component, action, **details):
        trace.append({"step": len(trace) + 1, "component": component, "action": action, **details})
    record("实验识别", "读取用户选择的 OSPF 实验及显式拓扑", mode="local_rules")
    devices = {name: parse_config(name, content) for name, content in payload["configs"].items()}
    record("配置解析工具", "解析 VRP 配置并保留行号", devices=list(devices))
    findings, unknowns, checks = [], [], []
    for device in devices.values():
        for error in device.errors:
            unknowns.append(finding("PARSE_ERROR", "配置中存在无法解析的受支持命令", [device.name], [error], "unknown"))
    for link in payload["topology"]["links"]:
        if _inspection is not None:
            problems, missing, passed = _inspection[link["id"]]
        else:
            problems, missing, passed = check_link(link, devices, payload.get("requirements", {}).get("expected_area"))
        for item in problems + missing:
            item["link_id"] = link["id"]
        findings.extend(problems)
        unknowns.extend(missing)
        checks.extend({**item, "link_id": link["id"]} for item in passed)
        record("链路检查结果" if _inspection is not None else "链路检查工具", "汇总已执行的链路检查" if _inspection is not None else "检查接口、子网和 OSPF 归属", link_id=link["id"], findings=[f["code"] for f in problems], unknowns=[f["code"] for f in missing])
    runtime_checks, runtime_missing, runtime_failed = verify_runtime(payload["topology"], payload.get("observations"))
    digest = fingerprint(payload)
    observations = payload.get("observations", {})
    snapshot_matches = observations.get("config_fingerprint") == digest
    if not snapshot_matches:
        runtime_missing.append("运行观测须绑定本次配置指纹；请为当前配置采集并提交新观测")
    if snapshot_matches:
        findings.extend(runtime_failed)
    else:
        for check in runtime_checks:
            check["status"] = "UNKNOWN"
            check["reason"] = "观测未绑定当前配置，不能据此判断本次运行状态"
    if _runtime:
        findings.extend(_runtime["findings"])
        runtime_checks.extend(_runtime["runtime_checks"])
        runtime_missing.extend(_runtime["missing"])
    record("复测核对工具", "核对用户提交的运行观测（未连接设备执行命令）", missing=runtime_missing, config_matches=snapshot_matches)
    status = "fault_found" if findings else "needs_information" if unknowns else "awaiting_verification" if runtime_missing else "verified_from_observations"
    level = payload.get("hint_level", 1)
    if findings:
        hints = [HINTS.get(item["code"], ("检查工具指出的配置。",) * 3)[level - 1] for item in findings]
        hint = "\n".join(dict.fromkeys(hints))
    elif unknowns:
        hint = "补充缺失配置，或人工确认未支持的配置，再进行诊断。"
    elif runtime_missing:
        hint = "受支持范围内的配置检查通过。请提交绑定当前配置的邻居状态和端到端 Ping 结果。"
    else:
        hint = "当前配置检查通过，且用户提交的复测观测符合本实验要求。"
    knowledge_search = search(payload.get("question", "OSPF 实验"), [f["code"] for f in findings + unknowns], scope=payload.get("knowledge_scope", "default"))
    knowledge = knowledge_search["results"]
    record("知识检索", "检索本地教学说明", documents=[doc["id"] for doc in knowledge])
    record("教学指导", "按所选等级生成提示", hint_level=level)
    result = {
        "schema_version": "1.0", "mode": "local_rules", "experiment": "OSPF",
        "created_at": datetime.now(timezone.utc).isoformat(), "config_fingerprint": digest,
        "status": status, "findings": findings, "unknowns": unknowns,
        "checks": checks, "runtime_checks": runtime_checks, "runtime_missing": runtime_missing,
        "hint_level": level, "hint": hint, "knowledge": knowledge, "trace": trace,
        "summary": {"faults": len(findings), "unknowns": len(unknowns), "static_checks_passed": len(checks)},
        "next_steps": runtime_missing,
        "command_output_parsing": _runtime or {},
        "knowledge_scope": payload.get("knowledge_scope", "default"),
        "knowledge_retrieval": {key: value for key, value in knowledge_search.items() if key != "results"},
        "planning_knowledge": (_context or {}).get("results", []),
    }
    if _explain and payload.get("use_llm", False):
        from .agents import run_diagnostic_agent
        result["llm"] = run_diagnostic_agent(payload, result)
        record("可选模型诊断与教学", "模型在有限轮次内选择只读证据工具并补充教学解释", status=result["llm"]["status"],
               model=result["llm"].get("model"), calls=result["llm"].get("calls", []),
               elapsed_ms=result["llm"].get("elapsed_ms"), error_code=result["llm"].get("error_code"), tool_trace=result["llm"]["tool_trace"])
    return result
