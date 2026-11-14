"""Request statistics stored in the project, without an external service."""
from collections import Counter
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading

_lock = threading.Lock()


def record_invocation(path, *, elapsed_ms, http_status, result=None, error_code=None):
    model = (result or {}).get("result", {}).get("llm", {})
    calls = model.get("calls", [])
    tools = model.get("tool_trace", [])
    event = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_ms": elapsed_ms,
        "http_status": http_status,
        "transport": "success" if result else "failed",
        "diagnosis_status": (result or {}).get("status"),
        "model_status": model.get("status"),
        "error_code": error_code,
        "model_calls": len(calls),
        "model_elapsed_ms": sum(call.get("elapsed_ms", 0) for call in calls),
        "total_tokens": sum(call.get("usage", {}).get("total_tokens", 0) for call in calls),
        "tools": [{"name": tool["tool"], "status": tool["status"]} for tool in tools],
    }
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        logging.getLogger(__name__).warning("本轮监控记录写入失败，请检查项目runtime目录。")


def summarize(records):
    records = list(records)
    elapsed = sorted(row["elapsed_ms"] for row in records)
    tools = Counter(tool["name"] for row in records for tool in row["tools"])
    failures = Counter(row.get("error_code") or row.get("model_status") or row.get("diagnosis_status") or "unknown"
                       for row in records if row["transport"] != "success" or row.get("diagnosis_status") != "success")
    return {
        "requests": len(records),
        "transport_successes": sum(row["transport"] == "success" for row in records),
        "diagnosis_successes": sum(row.get("diagnosis_status") == "success" for row in records),
        "average_ms": round(sum(elapsed) / len(elapsed)) if elapsed else None,
        "p95_ms": elapsed[(95 * len(elapsed) + 99) // 100 - 1] if elapsed else None,
        "model_calls": sum(row["model_calls"] for row in records),
        "total_tokens": sum(row["total_tokens"] for row in records),
        "tools": dict(tools),
        "failures": dict(failures),
        "latest": records[-1] if records else None,
    }
