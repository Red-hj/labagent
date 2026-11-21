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
EMBEDDING_MODELS = {
    "minilm": {
        "model": MODEL, "repository": MODEL_REPOSITORY, "revision": MODEL_REVISION,
        "model_file": "model_optimized.onnx", "token_budget": TOKEN_BUDGET,
        "query_prefix": "", "passage_prefix": "",
        "semantic_method": "local_multilingual_semantic", "hybrid_method": "bm25_semantic_rrf",
    },
    "e5-small": {
        "model": "intfloat/multilingual-e5-small", "repository": "Xenova/multilingual-e5-small",
        "revision": "761b726dd34fb83930e26aab4e9ac3899aa1fa78", "model_file": "onnx/model_quantized.onnx",
        "token_budget": 500, "query_prefix": "query: ", "passage_prefix": "passage: ",
        "semantic_method": "local_multilingual_e5_small", "hybrid_method": "bm25_e5_small_rrf",
    },
}


def unit(vector):
    values = [float(value) for value in vector]
    if not values or not all(math.isfinite(value) for value in values):
        raise ValueError("模型返回了空向量或非有限值")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or not norm:
        raise ValueError("模型返回的向量无法归一化")
    return [value / norm for value in values]


def text_windows(text, token_count, *, token_budget=TOKEN_BUDGET):
    """Cover long passages without silently dropping text at the model context limit."""
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        if token_count(text[start:low]) > token_budget:
            raise ValueError("单个字符超过模型窗口，无法编码该资料")
        while low < high:
            middle = (low + high + 1) // 2
            if token_count(text[start:middle]) <= token_budget:
                low = middle
            else:
                high = middle - 1
        yield text[start:low]
        if low == len(text):
            break
        start = max(start + 1, low - min(20, (low - start) // 4))


def embedding_windows(text, token_count, prefix, token_budget):
    for window in text_windows(text, lambda value: token_count(prefix + value), token_budget=token_budget):
        yield prefix + window


class SemanticIndex:
    def __init__(self, passages, embedder, *, token_budget=TOKEN_BUDGET, query_prefix="", passage_prefix=""):
        self.passages, self.embedder = passages, embedder
        self.token_budget, self.query_prefix = token_budget, query_prefix
        texts, self.owners = [], []
        for index, passage in enumerate(passages):
            for window in embedding_windows(passage["content"], embedder.token_count, passage_prefix, token_budget):
                texts.append(window)
                self.owners.append(index)
        self.vectors = [unit(vector) for vector in embedder.embed(texts, batch_size=16)] if texts else []
        if len(self.vectors) != len(texts) or len({len(vector) for vector in self.vectors}) > 1:
            raise ValueError("文档向量数量或维度不匹配")

    def search(self, query):
        if not query.strip() or not self.passages:
            return []
        windows = list(embedding_windows(query, self.embedder.token_count, self.query_prefix, self.token_budget))
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


def compare(version, scope, questions, documents, embedder, *, embedding_model="minilm", english_course=False):
    profile = EMBEDDING_MODELS[embedding_model]
    passages = [passage for document in documents for passage in knowledge.chunks(document)]
    started = time.perf_counter()
    index = SemanticIndex(passages, embedder, token_budget=profile["token_budget"],
                          query_prefix=profile["query_prefix"], passage_prefix=profile["passage_prefix"])
    index_seconds = time.perf_counter() - started
    sources = [knowledge.summary(document) for document in documents]
    methods = {name: {"version": version, "scope": scope, "method": name, "sources": sources, "predictions": {}}
               for name in ("local_bm25_with_fault_tags", profile["semantic_method"], profile["hybrid_method"])}
    routed_method = profile["semantic_method"] + "_english_course"
    if english_course:
        methods[routed_method] = {"version": version, "scope": scope, "method": routed_method, "sources": sources,
                                  "predictions": {}, "query_routes": {}}
    timings = {}
    for question in questions:
        started = time.perf_counter()
        query, qid = question["query"], question["id"]
        lexical = knowledge.rank_documents(query, documents, scope=scope, limit=RANK_WINDOW)["results"]
        semantic = index.search(query)
        grouped = {"local_bm25_with_fault_tags": lexical[:TOP_K], profile["semantic_method"]: semantic[:TOP_K],
                   profile["hybrid_method"]: fuse_rankings(lexical, semantic)}
        if english_course:
            route = profile["semantic_method"] if knowledge.CJK.search(query) else "bm25_section_titles"
            grouped[routed_method] = (semantic[:TOP_K] if route == profile["semantic_method"] else
                                      knowledge.rank_documents(query, documents, scope=scope, limit=TOP_K, section_titles=True)["results"])
            methods[routed_method]["query_routes"][qid] = route
        for name, hits in grouped.items():
            methods[name]["predictions"][qid] = [{field: hit[field] for field in (*HIT_FIELDS, "score", "retrieval_reason")}
                                                   for hit in hits]
        timings[qid] = round(time.perf_counter() - started, 4)
    settings = {key: profile[key] for key in ("model", "repository", "revision", "model_file", "token_budget", "query_prefix", "passage_prefix")}
    settings.update({"passage_aggregation": "max_window_cosine",
                "query_aggregation": "mean_normalized_windows", "rank_window": RANK_WINDOW,
                "rrf_constant": RRF_CONSTANT, "relevance_threshold": None,
                "documents": len(documents), "passages": len(passages), "encoded_windows": len(index.vectors),
                "index_seconds": round(index_seconds, 4), "query_seconds": timings})
    for name, result in methods.items():
        if name != "local_bm25_with_fault_tags":
            result["settings"] = settings
    if english_course:
        methods[routed_method]["settings"] = {**settings, "course_language": "en",
                                               "query_routing": "han_to_semantic_other_to_section_bm25"}
    return methods, settings


def load_embedder(*, offline=False, embedding_model="minilm"):
    os.environ.setdefault("HF_HOME", str(ROOT / "runtime/huggingface"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType
    from huggingface_hub import snapshot_download
    profile = EMBEDDING_MODELS[embedding_model]
    cache = ROOT / "runtime/embedding-models"
    snapshot = snapshot_download(profile["repository"], revision=profile["revision"], cache_dir=str(cache),
                                 allow_patterns=["*.json", profile["model_file"]], token=False, local_files_only=offline)
    model_name = profile["model"]
    if embedding_model == "e5-small":
        model_name = profile["repository"]
        if model_name not in {model["model"] for model in TextEmbedding.list_supported_models()}:
            TextEmbedding.add_custom_model(model=model_name, pooling=PoolingType.MEAN, normalization=True, dim=384,
                                           sources=ModelSource(hf=model_name), model_file=profile["model_file"],
                                           output_name="last_hidden_state")
    return TextEmbedding(model_name=model_name, specific_model_path=str(snapshot), cache_dir=str(cache),
                         providers=["CPUExecutionProvider"], threads=2, local_files_only=True)


def main():
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="本地生成检索预测；答案由独立评分步骤读取")
    parser.add_argument("package", type=Path)
    parser.add_argument("output", type=Path, help="项目 runtime 内的新目录")
    parser.add_argument("--embedding-model", choices=EMBEDDING_MODELS, default="minilm", help="选择本地多语言向量模型")
    parser.add_argument("--english-course", action="store_true", help="额外评测英文课件中含汉字问法的语义检索")
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
        embedder = load_embedder(offline=args.offline, embedding_model=args.embedding_model)
        load_seconds = round(time.perf_counter() - started, 4)
        results, settings = compare(version, scope, questions, documents, embedder, embedding_model=args.embedding_model,
                                    english_course=args.english_course)
        settings["model_load_seconds"] = load_seconds
        output.mkdir(parents=True, exist_ok=False)
        for name, result in results.items():
            if "settings" in result:
                result["settings"]["model_load_seconds"] = load_seconds
            (output / (name + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, KeyError, OSError, ImportError) as error:
        parser.exit(2, f"检索未完成：{error}\n")
    print(f"{len(questions)} 题，{len(results)} 种预测已写入 {output}。使用 evaluate_course_retrieval.py score 评分。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
