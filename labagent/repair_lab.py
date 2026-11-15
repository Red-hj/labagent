"""Evaluate actual FRR observations against one shared set of experiment goals."""
import difflib
import ipaddress
import json

from .connectivity import parse_ping_records

RUN_KIND = "labagent_repair_run"
REPAIR_TOOL = {"type":"function", "function": {"name":"compare_repair_candidates",
    "description":"读取FRR实验运行记录，用共同实验目标重新核对故障基线与各候选修复，比较实际Ping、已安装路由来源和配置差异。返回全部目标满足的候选方案及原始观测。",
    "parameters":{"type":"object","properties":{"source_id":{"type":"string"}},"required":["source_id"],"additionalProperties":False}}}


def route_to(table, target):
    address = ipaddress.ip_interface(target).ip
    choices = [(ipaddress.ip_network(prefix), rows) for prefix, rows in table.items()
               if address in ipaddress.ip_network(prefix)]
    if not choices:
        return []
    return max(choices, key=lambda item: item[0].prefixlen)[1]


def evaluate_phase(goals, phase):
    checks = []
    observations = phase["observations"]
    for goal in goals:
        observed = observations[goal["device"]]
        kind = goal["kind"]
        if kind == "ping":
            output = observed["pings"][goal["id"]]
            records = [row for row in parse_ping_records(output.splitlines()) if row["target"] == goal["target"]]
            passed = bool(records) and all(row["passed"] and row["sent"] == goal.get("count", 3) for row in records)
            failure = "内核返回Network unreachable，目标不可达" if "network unreachable" in output.lower() else "测试未返回完整收发统计"
            detail = f"{goal['device']} → {goal['target']}：" + (f"{records[-1]['sent']}发{records[-1]['received']}收" if records else failure)
            evidence = output
        elif kind == "route_protocol":
            routes = route_to(observed["routes"], goal["target"])
            installed = [row for row in routes if row.get("selected") and row.get("installed")]
            protocols = sorted({str(row.get("protocol", "")).lower() for row in installed})
            passed = bool(protocols) and all(protocol == goal["expected"].lower() for protocol in protocols)
            detail = "实际路由来源：" + ("、".join(protocols) or "目标路由尚未安装")
            evidence = observed.get("route_output") or json.dumps(routes, ensure_ascii=False)
        elif kind == "ospf_neighbor":
            neighbors = observed["neighbors"]["neighbors"]
            rows = neighbors.get(goal["target"], [])
            passed = len(rows) == 1 and rows[0].get("converged") == "Full" and rows[0].get("ifaceAddress") == goal["peer_address"]
            detail = (f"{goal['device']}：对端 {goal['target']} 已达 Full。" if passed else
                      f"{goal['device']}：没有匹配对端地址 {goal['peer_address']} 的 Full 邻接。")
            evidence = json.dumps(observed["neighbors"], ensure_ascii=False, indent=2)
        elif kind in {"config_absent", "config_contains"}:
            matching = [line for line in observed["config"].splitlines() if line.strip().startswith(goal["target"])]
            passed = bool(matching) if kind == "config_contains" else not matching
            detail = ("配置包含：" + "；".join(matching)) if matching else "当前配置没有“" + goal["target"].strip() + "”"
            evidence = observed["config"]
        else:
            raise ValueError("实验目标类型无效")
        checks.append({"id": goal["id"], "title": goal["title"], "kind": kind, "device": goal["device"],
                       "passed": passed, "status": "passed" if passed else "failed", "detail": detail, "evidence": evidence})
    return checks


def _compare_run(run):
    if not isinstance(run, dict) or run.get("kind") != RUN_KIND or run.get("schema_version") != 1:
        raise ValueError("请选择LabAgent实验运行记录JSON")
    goals, phases = run.get("goals"), run.get("phases")
    if not isinstance(goals, list) or not 1 <= len(goals) <= 8 or not isinstance(phases, list) or not 2 <= len(phases) <= 7:
        raise ValueError("运行记录需要1～8个共同目标与2～7个实验阶段")
    if phases[0].get("id") != "baseline":
        raise ValueError("第一阶段须为baseline故障基线")
    results = []
    baseline = phases[0]["observations"]
    for phase in phases:
        checks = evaluate_phase(goals, phase)
        changes = []
        for device, observed in phase["observations"].items():
            before = baseline[device]["config"].splitlines()
            after = observed["config"].splitlines()
            difference = "\n".join(difflib.unified_diff(before, after, fromfile=device + "修改前", tofile=device + "修改后", lineterm=""))
            if difference:
                changes.append({"device": device, "diff": difference})
        results.append({"id": phase["id"], "title": phase["title"], "checks": checks,
                        "passed_count": sum(item["passed"] for item in checks), "total": len(checks),
                        "status": "passed" if all(item["passed"] for item in checks) else "incomplete",
                        "commands": phase.get("commands", {}), "changes": changes})
    successful = [row for row in results[1:] if row["status"] == "passed"]
    recommended = min(successful, key=lambda row: sum(len(lines) for lines in row["commands"].values())) if successful else None
    return {"title": run["title"], "source": "FRR实验运行记录", "run_id": run["run_id"],
            "environment": run["environment"], "phases": results, "recommended": recommended["id"] if recommended else None}


def compare_run(run):
    try:
        return _compare_run(run)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("运行记录字段不完整，请提供实验工具导出的run.json") from error


def discover_runs(sources):
    output = []
    for source_id, source in sources.items():
        if not source["text"].lstrip().startswith("{"):
            continue
        try:
            run = json.loads(source["text"])
        except ValueError:
            continue
        if not isinstance(run, dict) or run.get("kind") != RUN_KIND:
            continue
        try:
            output.append({"source_id":source_id,"material":source["device"],**compare_run(run)})
        except ValueError as error:
            output.append({"source_id":source_id,"material":source["device"],"error":str(error)})
    return output
