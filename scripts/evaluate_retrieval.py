"""Evaluate any local course corpus using separate questions and source labels."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from labagent import knowledge
from labagent.model import load_env
from labagent.retrieval_evaluation import evaluate, validate_questions


def metric_text(metric):
    if metric["rate"] is None:
        return "无对应样本"
    return f'{metric["count"]}/{metric["total"]}（{metric["rate"]:.1%}）'


def markdown(report):
    scores, k = report["metrics"], report["limit"]
    mrr = f'{scores["mrr_at_k"]:.4f}' if scores["mrr_at_k"] is not None else "无对应样本"
    lines = ["# 课程检索评测", "", report["name"], "", report["description"], "",
             f'资料范围：`{report["scope"]}`；方法：`{report["method"]}`；题目：{report["question_count"]}。', "",
             f'- Top-1 命中：{metric_text(scores["hit_at_1"])}',
             f'- Top-{k} 命中：{metric_text(scores["hit_at_k"])}',
             f'- MRR@{k}：{mrr}',
             f'- 标注证据覆盖：{metric_text(scores["evidence_recall_at_k"])}',
             f'- 无答案题返回空结果：{metric_text(scores["empty_on_unanswerable"])}',
             f'- 返回片段原文校验：{metric_text(scores["source_validity"])}', "",
             "命中须为正确文档且片段覆盖标注行与引文。无答案题单独统计；非空结果表示检索返回了干扰片段，不代表模型回答错误。",
             "题目和标注分开读取，标注不进入检索；仅评测检索，不调用模型。", "",
             "| 题号 | 分类 | 首次正确排名 | 证据覆盖 | 返回片段 |", "| --- | --- | --- | --- | --- |"]
    for row in report["records"]:
        values = [row["id"], row["category"], str(row["rank"] or ("未命中" if row["expected"] else "无答案题")),
                  f'{row["evidence_found"]}/{len(row["expected"])}', str(len(row["results"]))]
        lines.append("| " + " | ".join(value.replace("|", "\\|").replace("\n", " ") for value in values) + " |")
    lines += ["", "逐条片段、位置、原文校验及分类统计见 evaluation.json。", ""]
    return "\n".join(lines)


def main():
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="课程检索离线评测，不调用模型；输出仅写入项目 runtime")
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    try:
        destination = args.output.resolve()
        if not destination.is_relative_to((ROOT / "runtime").resolve()) or destination == (ROOT / "runtime").resolve():
            raise ValueError("输出目录须位于项目 runtime 的子目录内")
        if destination.exists():
            raise ValueError("输出目录已存在，请选择新的目录以保留对照结果")
        dataset = json.loads(args.questions.read_text(encoding="utf-8-sig"))
        if not isinstance(dataset, dict) or set(dataset) != {"name", "description", "scope", "questions"}:
            raise ValueError("题集须包含 name、description、scope、questions")
        for key in ("name", "description"):
            if not isinstance(dataset[key], str) or not dataset[key].strip():
                raise ValueError(f"题集 {key} 须为非空文字")
        knowledge.validate_scope(dataset["scope"])
        validate_questions(dataset["questions"])
        labels = json.loads(args.labels.read_text(encoding="utf-8-sig"))
        documents, warnings = knowledge.corpus(dataset["scope"])
        if warnings:
            raise ValueError("资料有读取或校验错误，请先在资料库修复")
        def retrieve(query, *, limit):
            return knowledge.rank_documents(query, documents, scope=dataset["scope"], limit=limit)
        report = evaluate(dataset["questions"], labels, documents, retrieve, limit=args.limit)
        report.update({key: dataset[key] for key in ("name", "description", "scope")})
        report["generated_at"] = datetime.now(timezone.utc).isoformat()
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (destination / "报告.md").write_text(markdown(report), encoding="utf-8")
    except (ValueError, OSError) as error:
        parser.exit(2, f"评测未完成：{error}\n")
    print(f'Top-{args.limit}: {metric_text(report["metrics"]["hit_at_k"])}')
    print(f'无答案题空结果: {metric_text(report["metrics"]["empty_on_unanswerable"])}')
    print(destination / "报告.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
