"""Reproducible local fixture evaluation; not a generalization accuracy claim."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from datetime import datetime, timezone
import json
import time
from labagent.engine import diagnose


def main():
    records = []
    for file in sorted((ROOT / "data" / "cases").glob("*.json")):
        case = json.loads(file.read_text(encoding="utf-8"))
        expected = case.pop("expected")
        start = time.perf_counter()
        result = diagnose(case)
        actual = {"codes": sorted({item["code"] for item in result["findings"]}),
                  "unknown_codes": sorted({item["code"] for item in result["unknowns"]}), "status": result["status"]}
        records.append({"case": file.stem, "passed": actual == expected, "expected": expected,
                        "actual": actual, "elapsed_ms": round((time.perf_counter() - start) * 1000, 3)})
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "scope": "16 个手工定义的本地回归案例；不代表独立测试集或真实教学效果。模型与云端未参与本次评测。",
              "total": len(records), "passed": sum(item["passed"] for item in records), "records": records}
    folder = ROOT / "runtime"
    folder.mkdir(exist_ok=True)
    (folder / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 本地回归评测结果", "", report["scope"], "", f"通过 {report['passed']}/{report['total']} 个案例。", "", "| 案例 | 结果 | 耗时毫秒 |", "| --- | --- | --- |"]
    lines.extend(f"| {item['case']} | {'通过' if item['passed'] else '失败'} | {item['elapsed_ms']} |" for item in records)
    (folder / "evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Regression cases passed: {report['passed']}/{report['total']}")
    print(folder / "evaluation.md")
    return 0 if report["total"] and report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
