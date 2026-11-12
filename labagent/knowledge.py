"""Local BM25 retrieval over immutable, source-addressed teaching documents."""
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import uuid
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
NOTES_DIR = ROOT / "data" / "course_notes"
IMPORT_LOCK = threading.Lock()


def validate_scope(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("资料范围只能包含1～64位英文字母、数字、下划线或短横线")
    return value


def make_document(payload):
    if not isinstance(payload, dict):
        raise ValueError("资料必须为对象")
    title, content = payload.get("title"), payload.get("content")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        raise ValueError("资料标题须为1～120字")
    if not isinstance(content, str) or not 1 <= len(content.strip()) <= 150000:
        raise ValueError("资料正文须为1～150000字")
    scope = validate_scope(payload.get("scope", "default"))
    version = payload.get("version", "未注明版本")
    source_name = payload.get("source_name", "用户提交文本")
    url = payload.get("reference_url", "")
    if not isinstance(version, str) or not 1 <= len(version) <= 60 or not isinstance(source_name, str) or not 1 <= len(source_name) <= 200:
        raise ValueError("版本须为1～60字，来源说明须为1～200字")
    if not isinstance(url, str) or len(url) > 2000 or (url and (urlparse(url).scheme not in ("https", "http") or not urlparse(url).hostname)):
        raise ValueError("参考链接须为空或有效的 HTTP/HTTPS 地址")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    metadata = {"title": title.strip(), "content": normalized, "scope": scope, "version": version,
                "source_name": source_name, "reference_url": url}
    digest = hashlib.sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {**metadata, "id": "course-" + digest, "content_hash": hashlib.sha256(normalized.encode()).hexdigest(),
            "source_type": "课程资料（用户导入，未自动核实）", "codes": []}


def import_document(payload):
    document = {**make_document(payload), "imported_at": datetime.now(timezone.utc).isoformat()}
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    path = NOTES_DIR / (document["id"].removeprefix("course-") + ".json")
    with IMPORT_LOCK:
        if path.exists():
            document = json.loads(path.read_text(encoding="utf-8"))
            created = False
        else:
            temporary = NOTES_DIR / (uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(path)
                created = True
            finally:
                temporary.unlink(missing_ok=True)
    return {"created": created, "document": summary(document)}


def summary(document):
    return {key: document.get(key) for key in ("id", "title", "scope", "version", "source_name", "reference_url", "source_type", "content_hash")}


def bundled_documents():
    folder = ROOT / "data" / "b_knowledge"
    source = json.loads((folder / "sources.json").read_text(encoding="utf-8"))
    documents = []
    for row in source["notes"]:
        document = make_document({**{key: row[key] for key in ("title", "source_name", "reference_url")},
                                  "scope": source["scope"], "version": source["version"],
                                  "content": (folder / row["file"]).read_text(encoding="utf-8")})
        document["source_type"] = source["source_type"]
        documents.append(document)
    return documents


def corpus(scope="default"):
    validate_scope(scope)
    base = json.loads((ROOT / "data" / "knowledge.json").read_text(encoding="utf-8"))
    documents, warnings = [], []
    for original in base:
        documents.append({**original, "scope": "common", "version": "bundled-v1", "source_name": "项目整理的教学说明",
                          "content_hash": hashlib.sha256(original["content"].encode()).hexdigest()})
    documents.extend(doc for doc in bundled_documents() if scope in ("default", doc["scope"]))
    seen = {doc["id"] for doc in documents}
    for file in sorted(NOTES_DIR.glob("*.json")):
        try:
            if file.stat().st_size > 1_000_000:
                raise ValueError("too large")
            document = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or not isinstance(document.get("content"), str) or not isinstance(document.get("title"), str):
                raise ValueError("invalid document")
            actual = hashlib.sha256(document["content"].encode()).hexdigest()
            if actual != document.get("content_hash"):
                raise ValueError("content hash mismatch")
            metadata = {key: document[key] for key in ("title", "content", "scope", "version", "source_name", "reference_url")}
            identity = hashlib.sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if file.stem != identity or document.get("id") != "course-" + identity:
                raise ValueError("document metadata mismatch")
            document["codes"] = []
            document["source_type"] = "课程资料（用户导入，未自动核实）"
            if document.get("scope") == scope and document["id"] not in seen:
                documents.append(document)
                seen.add(document["id"])
        except (ValueError, OSError, TypeError, KeyError):
            warnings.append(f"资料文件 {file.name} 格式或校验不通过，未加入检索。")
    return documents, warnings


def list_documents(scope="default"):
    documents, warnings = corpus(scope)
    return {"scope": scope, "documents": [summary(doc) for doc in documents], "warnings": warnings}


def list_scopes():
    scopes = {"default", *(doc["scope"] for doc in bundled_documents())}
    for file in NOTES_DIR.glob("*.json"):
        try:
            if file.stat().st_size <= 1_000_000:
                scopes.add(validate_scope(json.loads(file.read_text(encoding="utf-8"))["scope"]))
        except (ValueError, OSError, KeyError, TypeError):
            pass
    return sorted(scopes)


def chunks(document, size=600):
    text = document["content"]
    newlines = [match.start() for match in re.finditer("\n", text)]
    start, number = 0, 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary
        while end > start and text[end - 1].isspace():
            end -= 1
        number += 1
        yield {**{key: value for key, value in document.items() if key != "content"}, "content": text[start:end],
               "chunk_id": f"{document['id']}:{number}", "char_start": start, "char_end": end,
               "line_start": bisect_right(newlines, start - 1) + 1, "line_end": bisect_right(newlines, end - 2) + 1}
        start = end


def tokens(text):
    values = re.findall(r"[a-z0-9_]+", text.lower())
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        values.extend(span[index:index + 2] for index in range(len(span) - 1))
        if len(span) == 1:
            values.append(span)
    return values


def search(query, codes=None, scope="default", limit=3):
    validate_scope(scope)
    if not isinstance(query, str) or len(query) > 10000:
        raise ValueError("检索问题须为10000字以内文本")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("检索数量须为1～10")
    codes = codes or []
    documents, warnings = corpus(scope)
    passages = [chunk for document in documents for chunk in chunks(document)]
    counters = [Counter(tokens(passage["title"]) * 2 + tokens(passage["content"])) for passage in passages]
    lengths = [sum(counter.values()) for counter in counters]
    average = sum(lengths) / max(len(lengths), 1) or 1
    frequency = Counter(token for counter in counters for token in counter)
    query_tokens = set(tokens(query))
    ranked = []
    for passage, counter, length in zip(passages, counters, lengths):
        lexical = 0.0
        for token in query_tokens & counter.keys():
            tf = counter[token]
            idf = math.log(1 + (len(passages) - frequency[token] + .5) / (frequency[token] + .5))
            lexical += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / average))
        matched_codes = set(codes) & set(passage.get("codes", []))
        score = lexical + 20 * len(matched_codes)
        if score > 0:
            ranked.append({**passage, "score": round(score, 5), "retrieval_reason": "故障标签与正文匹配" if matched_codes else "正文与标题匹配"})
    ranked.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return {"scope": scope, "method": "local_bm25_with_fault_tags", "query": query, "results": ranked[:limit],
            "document_count": len(documents), "chunk_count": len(passages), "warnings": warnings}


def retrieve(query: str, codes: list[str], limit=3, scope="default"):
    return search(query, codes, scope, limit)["results"]
