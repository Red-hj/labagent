"""Rank proposed checks by the hypothesis pairs their outcomes distinguish.

Predictions are explicit model hypotheses, not calibrated probabilities. Ranking
is deterministic; fault names and branch labels remain open-ended.
"""
from itertools import combinations

PLAN_TOOL = {"type": "function", "function": {"name": "plan_diagnostic_checks",
    "description": "比较候选检查，按可区分的原因对数/采集成本选择下一项。候选原因可自由命名；状态更新需要本轮引用。各原因预测同一种结果时用相同标签，未知写unknown。仅提出有诊断价值的实际命令。",
    "parameters": {"type": "object", "properties": {
        "hypotheses": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"}, "state": {"type": "string", "enum": ["active", "supported", "ruled_out"]},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {"source_id": {"type": "string"}, "line": {"type": "integer"}, "quote": {"type": "string"}},
                        "required": ["source_id", "line", "quote"], "additionalProperties": False}}},
            "required": ["id", "title", "state", "evidence"], "additionalProperties": False}},
        "tests": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "device": {"type": "string"}, "command": {"type": "string"}, "purpose": {"type": "string"},
            "cost": {"type": "integer", "description": "采集成本1～5；一条只读命令通常为1，多设备采集成本更高。"},
            "predictions": {"type": "array", "items": {"type": "object", "properties": {"hypothesis_id": {"type": "string"}, "result": {"type": "string"}},
                            "required": ["hypothesis_id", "result"], "additionalProperties": False}}},
            "required": ["id", "device", "command", "purpose", "cost", "predictions"], "additionalProperties": False}}},
        "required": ["hypotheses", "tests"], "additionalProperties": False}}}


def rank_checks(hypotheses, tests):
    active = {item["id"] for item in hypotheses if item["state"] != "ruled_out"}
    pairs = list(combinations(sorted(active), 2))
    ranked = []
    for test in tests:
        predictions = {item["hypothesis_id"]: item["result"].strip().casefold() for item in test["predictions"]
                       if item["hypothesis_id"] in active and item["result"].strip().casefold() != "unknown"}
        separated = [list(pair) for pair in pairs if all(key in predictions for key in pair) and predictions[pair[0]] != predictions[pair[1]]]
        if not predictions or not active:
            continue
        ranked.append({**test, "separated_pairs": separated, "pair_count": len(separated),
                       "coverage": len(predictions), "score": round(len(separated) / test["cost"], 3)})
    ranked.sort(key=lambda item: (item["pair_count"] / item["cost"], item["coverage"], -item["cost"]), reverse=True)
    selected = ranked[0] if ranked else None
    reason = "当前原因均已排除，等待新的排查方向。"
    if selected:
        reason = (f"这项检查能区分{selected['pair_count']}组候选原因，采集成本为{selected['cost']}。"
                  if selected["pair_count"] else "这项检查用于核对剩余原因；当前候选结果还不能进一步区分原因。")
    return {"hypotheses": hypotheses, "tests": ranked, "selected": selected, "reason": reason,
            "method": "separated_hypothesis_pairs_per_cost"}


def validate_plan(args, check_citation):
    if not isinstance(args, dict) or set(args) != {"hypotheses", "tests"}:
        raise ValueError("检查规划需要hypotheses和tests")
    hypotheses, tests = args["hypotheses"], args["tests"]
    if not isinstance(hypotheses, list) or not 1 <= len(hypotheses) <= 6 or not isinstance(tests, list) or len(tests) > 6:
        raise ValueError("提供1～6个候选原因和最多6项检查")
    ids, checked = set(), []
    for item in hypotheses:
        if not isinstance(item, dict) or set(item) != {"id", "title", "state", "evidence"}:
            raise ValueError("候选原因字段不符合定义")
        if any(not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 160 for key in ("id", "title")) or item["id"] in ids:
            raise ValueError("候选原因需要唯一编号和标题")
        if item["state"] not in {"active", "supported", "ruled_out"}:
            raise ValueError("候选原因状态无效")
        if not isinstance(item["evidence"], list) or not 1 <= len(item["evidence"]) <= 3:
            raise ValueError("候选原因状态需要1～3条本轮材料依据")
        checked.append({**item, "evidence": [check_citation(value) for value in item["evidence"]]})
        ids.add(item["id"])
    test_ids = set()
    for test in tests:
        if not isinstance(test, dict) or set(test) != {"id", "device", "command", "purpose", "cost", "predictions"}:
            raise ValueError("检查字段不符合定义")
        if any(not isinstance(test[key], str) or not test[key].strip() or len(test[key]) > 500 for key in ("id", "device", "command", "purpose")):
            raise ValueError("检查需要设备、命令和用途")
        if test["id"] in test_ids or type(test["cost"]) is not int or not 1 <= test["cost"] <= 5:
            raise ValueError("检查编号须唯一，成本为1～5")
        test_ids.add(test["id"])
        if not isinstance(test["predictions"], list) or not 1 <= len(test["predictions"]) <= 6:
            raise ValueError("请填写各候选原因对应的预期结果标签")
        predicted = set()
        for prediction in test["predictions"]:
            if not isinstance(prediction, dict) or set(prediction) != {"hypothesis_id", "result"} or prediction["hypothesis_id"] not in ids or prediction["hypothesis_id"] in predicted:
                raise ValueError("预期结果须指向不同的有效候选原因")
            if not isinstance(prediction["result"], str) or not prediction["result"].strip() or len(prediction["result"]) > 80:
                raise ValueError("预期结果标签须为80字以内文本")
            predicted.add(prediction["hypothesis_id"])
    return rank_checks(checked, tests)
