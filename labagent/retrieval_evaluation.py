"""Grade scoped retrieval against separate source annotations, without a model."""
from collections import defaultdict
import json
import time


def validate_questions(questions):
    if not isinstance(questions, list) or not questions:
        raise ValueError("题目须为非空数组")
    seen = set()
    for question in questions:
        if not isinstance(question, dict) or set(question) - {"id", "query", "category"}:
            raise ValueError("每题只接受 id、query 和可选 category；答案请放在独立标注文件中")
        qid, query = question.get("id"), question.get("query")
        category = question.get("category", "未分类")
        if not isinstance(qid, str) or not 1 <= len(qid) <= 80 or qid in seen:
            raise ValueError("题号须为1～80字且不可重复")
        if not isinstance(query, str) or not query.strip() or len(query) > 10000:
            raise ValueError(f"题目 {qid} 的问题须为1～10000字")
        if not isinstance(category, str) or not 1 <= len(category) <= 80:
            raise ValueError(f"题目 {qid} 的 category 须为1～80字")
        seen.add(qid)
    return seen


def validate_labels(questions, labels, documents):
    ids = validate_questions(questions)
    if not isinstance(labels, dict) or set(labels) != ids:
        raise ValueError("题目与标注的题号必须完全一致")
    for qid, evidence in labels.items():
        if not isinstance(evidence, list):
            raise ValueError(f"题目 {qid} 的标注须为数组；无答案题使用空数组")
        for expected in evidence:
            if not isinstance(expected, dict) or set(expected) != {"document_id", "line_start", "line_end", "quote"}:
                raise ValueError(f"题目 {qid} 需标注 document_id、line_start、line_end、quote")
            doc_id = expected["document_id"]
            if not isinstance(doc_id, str) or doc_id not in documents:
                raise ValueError(f"题目 {qid} 的文档不在当前资料范围，或已换版")
            lines = documents[doc_id]["content"].splitlines()
            start, end, quote = expected["line_start"], expected["line_end"], expected["quote"]
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
                raise ValueError(f"题目 {qid} 的原文行号超出范围")
            if not isinstance(quote, str) or not quote.strip() or quote not in "\n".join(lines[start - 1:end]):
                raise ValueError(f"题目 {qid} 的 quote 与标注行不符，请核对资料版本与位置")
        if len({json.dumps(item, sort_keys=True) for item in evidence}) != len(evidence):
            raise ValueError(f"题目 {qid} 存在重复标注")


def source_error(hit, documents):
    if not isinstance(hit, dict) or not isinstance(hit.get("id"), str) or hit["id"] not in documents:
        return "返回文档不在当前资料范围"
    text = documents[hit["id"]]["content"]
    start, end = hit.get("char_start"), hit.get("char_end")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        return "字符位置无效"
    if hit.get("content") != text[start:end]:
        return "片段与原文不一致"
    first = text.count("\n", 0, start) + 1
    last = text.count("\n", 0, end - 1) + 1
    if type(hit.get("line_start")) is not int or type(hit.get("line_end")) is not int or (hit["line_start"], hit["line_end"]) != (first, last):
        return "行号与原文位置不一致"
    return None


def ratio(count, total):
    return {"count": count, "total": total, "rate": round(count / total, 6) if total else None}


def contains_evidence(hit, annotation, document):
    if hit["id"] != annotation["document_id"] or not hit["line_start"] <= annotation["line_start"] <= annotation["line_end"] <= hit["line_end"]:
        return False
    lines = document["content"].splitlines(keepends=True)
    start = max(hit["char_start"], sum(map(len, lines[:annotation["line_start"] - 1])))
    end = min(hit["char_end"], sum(map(len, lines[:annotation["line_end"]])))
    return document["content"].find(annotation["quote"], start, end) >= 0


def metrics(rows):
    positive = [row for row in rows if row["expected"]]
    negative = [row for row in rows if not row["expected"]]
    hits = [hit for row in rows for hit in row["results"]]
    return {
        "hit_at_1": ratio(sum(row["rank"] == 1 for row in positive), len(positive)),
        "hit_at_k": ratio(sum(row["rank"] is not None for row in positive), len(positive)),
        "mrr_at_k": round(sum(1 / row["rank"] for row in positive if row["rank"]) / len(positive), 6) if positive else None,
        "evidence_recall_at_k": ratio(sum(row["evidence_found"] for row in positive), sum(len(row["expected"]) for row in positive)),
        "empty_on_unanswerable": ratio(sum(not row["results"] for row in negative), len(negative)),
        "source_validity": ratio(sum(hit["source_error"] is None for hit in hits), len(hits)),
    }


def evaluate(questions, labels, documents, retrieve, *, limit=3):
    """Only query and limit reach the retriever; labels are used after retrieval."""
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("检索数量须为1～10")
    by_id = {doc["id"]: doc for doc in documents}
    if len(by_id) != len(documents):
        raise ValueError("资料包含重复文档编号")
    validate_labels(questions, labels, by_id)
    rows, methods = [], set()
    for question in questions:
        started = time.perf_counter()
        response = retrieve(question["query"], limit=limit)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        if not isinstance(response, dict) or not isinstance(response.get("method"), str) or not response["method"]:
            raise ValueError("检索器须返回 method 与 results")
        if response.get("warnings"):
            raise ValueError("资料有读取或校验错误，请修复后重新评测")
        hits = response.get("results")
        if not isinstance(hits, list) or len(hits) > limit:
            raise ValueError("检索器返回的 results 须为不超过 limit 的数组")
        methods.add(response["method"])
        expected = labels[question["id"]]
        matched, rank, checked = set(), None, []
        for position, hit in enumerate(hits, 1):
            error = source_error(hit, by_id)
            checked.append({"passage": hit, "source_error": error})
            if error:
                continue
            for index, annotation in enumerate(expected):
                if contains_evidence(hit, annotation, by_id[hit["id"]]):
                    matched.add(index)
                    rank = rank or position
        rows.append({**question, "category": question.get("category", "未分类"), "expected": expected,
                     "rank": rank, "evidence_found": len(matched), "results": checked, "elapsed_ms": elapsed})
    if len(methods) != 1:
        raise ValueError("同一轮评测的检索方法必须一致")
    sources = [{"id": doc["id"], "title": doc["title"], "scope": doc["scope"], "version": doc["version"],
                "content_hash": doc["content_hash"]} for doc in documents]
    sources.sort(key=lambda doc: doc["id"])
    groups = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)
    return {"method": next(iter(methods)), "limit": limit, "question_count": len(rows),
            "sources": sources, "metrics": metrics(rows),
            "by_category": {key: metrics(value) for key, value in sorted(groups.items())}, "records": rows}
