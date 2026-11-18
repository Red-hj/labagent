"""Local BM25 retrieval over immutable, source-addressed teaching documents."""
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid
from urllib.parse import urlparse
from .storage import data_directory

ROOT = Path(__file__).resolve().parents[1]
NOTES_DIR = ROOT / "data" / "course_notes"
IMPORT_LOCK = threading.Lock()
# Keep protocol names, numbers and negation; remove only common question/function words.
SEARCH_STOPWORDS = frozenset("a an the is are be been being do does did what how why where when which please can could to of for in on at by with about from that this these those it its your my me and or 如何 什么 怎么 是否 为什 请问 哪些 一个 进行".split())
REFERENCE_NOTE = "检索结果是词面相关的候选参考，未判断原文是否足以回答问题；设备实际参数仍需当前配置或运行输出。"


def notes_directory():
    return data_directory() / "course_notes" if os.environ.get("LABAGENT_DATA_DIR") else NOTES_DIR


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
    folder = notes_directory()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (document["id"].removeprefix("course-") + ".json")
    with IMPORT_LOCK:
        if path.exists():
            document = json.loads(path.read_text(encoding="utf-8"))
            created = False
        else:
            temporary = folder / (uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(path)
                created = True
            finally:
                temporary.unlink(missing_ok=True)
    return {"created": created, "document": summary(document)}


def validate_course_documents(value, scope):
    """Normalize a request's imported references without writing shared files."""
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("云端课程资料须为最多100份资料的列表")
    documents, seen, size = [], set(), 0
    for item in value:
        document = make_document(item)
        if document["scope"] != scope:
            raise ValueError("云端课程资料须属于本轮选择的资料范围")
        if document["id"] in seen:
            raise ValueError("云端课程资料不可重复")
        seen.add(document["id"])
        size += len(document["content"])
        if size > 1_000_000:
            raise ValueError("本轮云端课程资料正文合计不能超过100万字，请拆分资料范围")
        documents.append(document)
    return documents


def export_course_documents(scope):
    documents, _ = corpus(scope)
    imported = [document for document in documents
                if document.get("source_type") == "课程资料（用户导入，未自动核实）"]
    return validate_course_documents(imported, scope)


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
        document["sectioned"] = True
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
    for file in sorted(notes_directory().glob("*.json")):
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


def get_document(document_id, scope="default"):
    documents, _ = corpus(scope)
    document = next((doc for doc in documents if doc["id"] == document_id), None)
    if document is None:
        return None
    return {**summary(document), "content": document["content"],
            "line_count": len(document["content"].splitlines())}


def list_scopes():
    scopes = {"default", *(doc["scope"] for doc in bundled_documents())}
    for file in notes_directory().glob("*.json"):
        try:
            if file.stat().st_size <= 1_000_000:
                scopes.add(validate_scope(json.loads(file.read_text(encoding="utf-8"))["scope"]))
        except (ValueError, OSError, KeyError, TypeError):
            pass
    return sorted(scopes)


def section_boundaries(text):
    headings, fence = [], None
    position = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if marker:
            run, tail = marker.groups()
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not tail.strip():
                fence = None
        elif fence is None and (heading := re.match(r"^ {0,3}(#{1,6})[ \t]+\S", line)):
            headings.append((len(heading[1]), position))
        position += len(line)
    sections = [start for level, start in headings if level >= 2]
    # Keep a document's title and introduction with its first real section.
    return (sections or [start for _, start in headings])[1:]


def chunks(document, size=600):
    text = document["content"]
    newlines = [match.start() for match in re.finditer("\n", text)]
    section_ends = section_boundaries(text)
    sectioned = bool(section_ends) or document.get("sectioned", False)
    section_ends.append(len(text))
    metadata = {key: value for key, value in document.items() if key not in ("content", "sectioned")}
    start, number = 0, 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        section_end = section_ends[bisect_right(section_ends, start)]
        end = min(start + size, section_end)
        if end < section_end:
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary
        while end > start and text[end - 1].isspace():
            end -= 1
        number += 1
        chunk = f"section-{number}" if sectioned else str(number)
        yield {**metadata, "content": text[start:end],
               "chunk_id": f"{document['id']}:{chunk}", "char_start": start, "char_end": end,
               "line_start": bisect_right(newlines, start - 1) + 1, "line_end": bisect_right(newlines, end - 2) + 1}
        start = end


def tokens(text):
    values = re.findall(r"[a-z0-9_]+", text.lower())
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        values.extend(span[index:index + 2] for index in range(len(span) - 1))
        if len(span) == 1:
            values.append(span)
    return values


def search_tokens(text):
    return [token for token in tokens(text) if token not in SEARCH_STOPWORDS]


def search(query, codes=None, scope="default", limit=3, *, course_documents=None):
    documents, warnings = corpus(scope)
    if course_documents is not None:
        documents = [document for document in documents
                     if document.get("source_type") != "课程资料（用户导入，未自动核实）"]
        documents.extend(validate_course_documents(course_documents, scope))
    return rank_documents(query, documents, codes, scope, limit, warnings)


def rank_documents(query, documents, codes=None, scope="default", limit=3, warnings=None):
    """Use the production ranking on a fixed corpus, including offline evaluations."""
    validate_scope(scope)
    if not isinstance(query, str) or len(query) > 10000:
        raise ValueError("检索问题须为10000字以内文本")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("检索数量须为1～10")
    codes = codes or []
    passages = [chunk for document in documents for chunk in chunks(document)]
    bodies = [Counter(search_tokens(passage["content"])) for passage in passages]
    counters = [body + Counter(search_tokens(passage["title"]) * 2) for passage, body in zip(passages, bodies)]
    lengths = [sum(counter.values()) for counter in counters]
    average = sum(lengths) / max(len(lengths), 1) or 1
    frequency = Counter(token for counter in counters for token in counter)
    query_tokens = set(search_tokens(query))
    ranked = []
    for passage, body, counter, length in zip(passages, bodies, counters, lengths):
        lexical = 0.0
        for token in query_tokens & counter.keys():
            tf = counter[token]
            idf = math.log(1 + (len(passages) - frequency[token] + .5) / (frequency[token] + .5))
            lexical += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length / average))
        matched_codes = set(codes) & set(passage.get("codes", []))
        score = lexical + 20 * len(matched_codes)
        if score > 0:
            match_type = "fault_tag" if matched_codes else "body" if query_tokens & body.keys() else "title_only"
            reason = {"fault_tag": "故障标签匹配", "body": "正文关键词匹配", "title_only": "仅标题匹配，正文未命中关键词"}[match_type]
            ranked.append({**passage, "score": round(score, 5), "retrieval_reason": reason, "match_type": match_type})
    ranked.sort(key=lambda item: (item["match_type"] == "title_only", -item["score"], item["chunk_id"]))
    results = ranked[:limit]
    match_status = "no_match" if not results else "title_only" if all(p["match_type"] == "title_only" for p in results) else "reference_candidates"
    return {"scope": scope, "method": "local_bm25_with_fault_tags", "query": query, "results": results,
            "match_summary": {"status": match_status, "answerability": "not_assessed", "note": REFERENCE_NOTE},
            "settings": {"question_stopwords": "v1", "body_before_title_only": True},
            "document_count": len(documents), "chunk_count": len(passages), "warnings": warnings or []}


def retrieve(query: str, codes: list[str], limit=3, scope="default"):
    return search(query, codes, scope, limit)["results"]
