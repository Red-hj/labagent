# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastembed==0.9.1",
# ]
# ///
"""Generate BM25, local multilingual semantic and RRF predictions; never read labels."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from labagent import knowledge
from labagent.model import load_env
from scripts.evaluate_course_retrieval import HIT_FIELDS, TOP_K, load_package, prepare_corpus

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REPOSITORY = "Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
MODEL_REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
TOKEN_BUDGET = 120  # Model context is 128; keep margin for special tokens.
RANK_WINDOW = 10
RRF_CONSTANT = 60


def unit(vector):
    values = [float(value) for value in vector]
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("模型返回了空向量或非有限值")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or not norm:
        raise ValueError("模型返回的向量无法归一化")
    return [value / norm for value in values]


def text_windows(text, token_count):
    """Cover long passages without silently dropping text at the model context limit."""
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        if token_count(text[start:low]) > TOKEN_BUDGET:
            raise ValueError("单个字符超过模型窗口，无法编码该资料")
        while low < high:
            middle = (low + high + 1) // 2
            if token_count(text[start:middle]) <= TOKEN_BUDGET:
                low = middle
            else:
                high = middle - 1
        yield text[start:low]
        if low == len(text):
            break
        start = max(start + 1, low - min(20, (low - start) // 4))


class SemanticIndex:
    def __init__(self, passages, embedder):
        self.passages, self.embedder = passages, embedder
        texts, self.owners = [], []
        for index, passage in enumerate(passages):
            for window in text_windows(passage["content"], embedder.token_count):
                texts.append(window)
                self.owners.append(index)
        self.vectors = [unit(vector) for vector in embedder.embed(texts, batch_size=16)] if texts else []
        if len(self.vectors) != len(texts) or len({len(vector) for vector in self.vectors}) > 1:
            raise ValueError("文档向量数量或维度不匹配")

    def search(self, query):
        if not query.strip() or not self.passages:
            return []
        windows = list(text_windows(query, self.embedder.token_count))
        vectors = [unit(vector) for vector in self.embedder.embed(windows, batch_size=16)]
        if len(vectors) != len(windows) or any(len(vector) != len(self.vectors[0]) for vector in vectors):
            raise ValueError("问题向量数量或维度不匹配")
        query_vector = unit([sum(column) / len(vectors) for column in zip(*vectors)])
        scores = [-math.inf] * len(self.passages)
        for owner, vector in zip(self.owners, self.vectors):
            scores[owner] = max(scores[owner], sum(a * b for a, b in zip(query_vector, vector)))
        order = sorted(range(len(scores)), key=lambda index: (-scores[index], self.passages[index]["chunk_id"]))
        return [{**self.passages[index], "score": round(scores[index], 7), "retrieval_reason": "本地多语言语义匹配"}
                for index in order[:RANK_WINDOW]]


def fuse_rankings(*rankings):
    scores, passages = {}, {}
    for ranking in rankings:
        seen = set()
        for rank, passage in enumerate(ranking[:RANK_WINDOW], 1):
            key = passage["chunk_id"]
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0) + 1 / (RRF_CONSTANT + rank)
            passages[key] = passage
    return [{**passages[key], "score": round(scores[key], 10), "retrieval_reason": "BM25 与语义排名融合"}
            for key in sorted(scores, key=lambda key: (-scores[key], key))[:TOP_K]]


def compare(version, scope, questions, documents, embedder):
    passages = [passage for document in documents for passage in knowledge.chunks(document)]
    started = time.perf_counter()
    index = SemanticIndex(passages, embedder)
    index_seconds = time.perf_counter() - started
    sources = [knowledge.summary(document) for document in documents]
    methods = {name: {"version": version, "scope": scope, "method": name, "sources": sources, "predictions": {}}
               for name in ("local_bm25_with_fault_tags", "local_multilingual_semantic", "bm25_semantic_rrf")}
    timings = {}
    for question in questions:
        started = time.perf_counter()
        query, qid = question["query"], question["id"]
        lexical = knowledge.rank_documents(query, documents, scope=scope, limit=RANK_WINDOW)["results"]
        semantic = index.search(query)
        for name, hits in zip(methods, (lexical[:TOP_K], semantic[:TOP_K], fuse_rankings(lexical, semantic))):
            methods[name]["predictions"][qid] = [{field: hit[field] for field in (*HIT_FIELDS, "score", "retrieval_reason")} for hit in hits]
        timings[qid] = round(time.perf_counter() - started, 4)
    settings = {"model": MODEL, "repository": MODEL_REPOSITORY, "revision": MODEL_REVISION,
                "token_budget": TOKEN_BUDGET, "passage_aggregation": "max_window_cosine",
                "query_aggregation": "mean_normalized_windows", "rank_window": RANK_WINDOW,
                "rrf_constant": RRF_CONSTANT, "relevance_threshold": None,
                "documents": len(documents), "passages": len(passages), "encoded_windows": len(index.vectors),
                "index_seconds": round(index_seconds, 4), "query_seconds": timings}
    for name, result in methods.items():
        if name != "local_bm25_with_fault_tags":
            result["settings"] = settings
    return methods, settings


def load_embedder(*, offline=False):
    os.environ.setdefault("HF_HOME", str(ROOT / "runtime/huggingface"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from fastembed import TextEmbedding
    from huggingface_hub import snapshot_download
    cache = ROOT / "runtime/embedding-models"
    snapshot = snapshot_download(MODEL_REPOSITORY, revision=MODEL_REVISION, cache_dir=str(cache),
                                 allow_patterns=["*.json", "model_optimized.onnx"], token=False, local_files_only=offline)
    return TextEmbedding(model_name=MODEL, specific_model_path=str(snapshot), cache_dir=str(cache),
                         providers=["CPUExecutionProvider"], threads=2, local_files_only=True)


def main():
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="本地生成三种检索预测；不读取答案，不调用收费接口")
    parser.add_argument("package", type=Path)
    parser.add_argument("output", type=Path, help="项目 runtime 内的新目录")
    parser.add_argument("--offline", action="store_true", help="只使用已缓存的固定模型快照")
    args = parser.parse_args()
    try:
        output = args.output.resolve()
        if output == (ROOT / "runtime").resolve() or not output.is_relative_to((ROOT / "runtime").resolve()):
            raise ValueError("输出目录须位于项目 runtime 的子目录")
        if output.exists():
            raise ValueError("输出目录已存在，请使用新目录保留对照结果")
        version, scope, sources, questions = load_package(args.package)
        prepare_corpus(scope, sources)
        documents, warnings = knowledge.corpus(scope)
        if warnings:
            raise ValueError("资料读取或校验失败")
        started = time.perf_counter()
        embedder = load_embedder(offline=args.offline)
        load_seconds = round(time.perf_counter() - started, 4)
        results, settings = compare(version, scope, questions, documents, embedder)
        settings["model_load_seconds"] = load_seconds
        output.mkdir(parents=True, exist_ok=False)
        for name, result in results.items():
            (output / (name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, KeyError, OSError, ImportError) as error:
        parser.exit(2, f"检索未完成：{error}\n")
    print(f"{len(questions)} 题；三种预测已写入 {output}。请由标注持有人使用 evaluate_course_retrieval.py score 评分。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
