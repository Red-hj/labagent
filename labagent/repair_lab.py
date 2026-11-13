"""Evaluate actual FRR observations against one shared set of experiment goals."""
import difflib
import ipaddress
import json

from .connectivity import parse_ping_records

RUN_KIND = "labagent_repair_run"


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
            detail = f"{goal['device']} → {goal['target']}：" + (f"{records[-1]['sent']}发{records[-1]['received']}收" if records else "补充完整Ping统计")
            evidence = output
        elif kind == "route_protocol":
            routes = route_to(observed["routes"], goal["target"])
            installed = [row for row in routes if row.get("selected") and row.get("installed")]
            protocols = sorted({str(row.get("protocol", "")).lower() for row in installed})
            passed = bool(protocols) and all(protocol == goal["expected"].lower() for protocol in protocols)
            detail = "实际路由来源：" + ("、".join(protocols) or "目标路由尚未安装")
            evidence = json.dumps(routes, ensure_ascii=False)
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


def compare_run(run):
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
