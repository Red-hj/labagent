import unittest

from labagent import knowledge
from scripts.compare_course_retrieval import (SemanticIndex, compare, fuse_rankings, promote_shared_first, rerank_with_mmarco,
                                              text_windows, unit)
from scripts.evaluate_course_retrieval import valid_span


class FakeEmbedder:
    def __init__(self):
        self.inputs = []

    def token_count(self, text):
        return len(text) + 2

    def embed(self, texts, batch_size=16):
        for text in texts:
            self.inputs.append(text)
            yield [1., 0.] if "gateway" in text.lower() or "默认网关" in text else [0., 1.]


def document(title, content):
    return knowledge.make_document({"title": title, "content": content, "scope": "semantic-test",
                                    "version": "one", "source_name": "自编测试数据"})


class SemanticRetrievalTests(unittest.TestCase):
    def test_asymmetric_prefixes_apply_to_every_window_and_count_toward_the_budget(self):
        doc = document("网关", "route " * 40 + "gateway")
        embedder = FakeEmbedder()
        index = SemanticIndex(list(knowledge.chunks(doc)), embedder, token_budget=40,
                              query_prefix="query: ", passage_prefix="passage: ")
        passage_windows = list(embedder.inputs)
        self.assertGreater(len(passage_windows), 1)
        self.assertTrue(all(value.startswith("passage: ") for value in passage_windows))
        self.assertTrue(any(value.endswith("gateway") for value in passage_windows))
        query = "路由 " * 50 + "默认网关"
        results = index.search(query)
        query_windows = embedder.inputs[len(passage_windows):]
        self.assertGreater(len(query_windows), 1)
        self.assertTrue(all(value.startswith("query: ") for value in query_windows))
        self.assertTrue(any(value.endswith("默认网关") for value in query_windows))
        self.assertTrue(all(embedder.token_count(value) <= 40 for value in embedder.inputs))
        self.assertEqual(results[0]["content"], doc["content"])

    def test_e5_predictions_record_the_model_snapshot_and_keep_citation_text(self):
        docs = [document("默认出口", "The default gateway forwards packets.")]
        questions = [{"id": "q1", "query": "默认网关", "category": "同义", "language": "zh"}]
        embedder = FakeEmbedder()
        results, settings = compare("one", "semantic-test", questions, docs, embedder, embedding_model="e5-small")
        self.assertEqual(set(results), {"local_bm25_with_fault_tags", "local_multilingual_e5_small", "bm25_e5_small_rrf"})
        self.assertEqual(settings["model"], "intfloat/multilingual-e5-small")
        self.assertEqual(len(settings["revision"]), 40)
        self.assertEqual(embedder.inputs, ["passage: The default gateway forwards packets.", "query: 默认网关"])
        corpus = {docs[0]["id"]: docs[0]["content"]}
        for method in ("local_multilingual_e5_small", "bm25_e5_small_rrf"):
            hit, = results[method]["predictions"]["q1"]
            self.assertIsNotNone(valid_span(hit, corpus))

    def test_english_course_routes_based_on_query_text_and_preserves_native_candidate_order(self):
        docs = [document("Exercise notes", "The default gateway forwards packets."), document("Other", "Renew a lease.")]
        # Language labels do not determine retrieval behavior.
        questions = [{"id": "han", "query": "默认网关如何转发？", "category": "direct", "language": "en"},
                     {"id": "latin", "query": "default gateway", "category": "direct", "language": "zh"}]
        results, _ = compare("one", "semantic-test", questions, docs, FakeEmbedder(), english_course=True)
        routed = results["local_multilingual_semantic_english_course"]
        agreed = results["local_multilingual_semantic_english_course_agreement"]
        self.assertEqual(routed["query_routes"], {"han": "local_multilingual_semantic", "latin": "bm25_section_titles"})
        self.assertEqual(routed["predictions"]["han"], results["local_multilingual_semantic"]["predictions"]["han"])
        expected = knowledge.rank_documents(questions[1]["query"], docs, scope="semantic-test", section_titles=True)["results"]
        self.assertEqual([row["chunk_id"] for row in routed["predictions"]["latin"]], [row["chunk_id"] for row in expected])
        self.assertEqual(agreed["predictions"]["latin"], routed["predictions"]["latin"])
        self.assertEqual(agreed["query_routes"], routed["query_routes"])

    def test_reranking_promotes_only_existing_body_matches_and_keeps_other_candidates_in_order(self):
        a, b, c = ({"chunk_id": name, "content": name.upper()} for name in ("a", "b", "c"))
        candidates = [a, b, c]
        match = {**c, "match_type": "body"}
        self.assertEqual(promote_shared_first(candidates, [match]), [c, a, b])
        self.assertEqual(promote_shared_first(candidates, [{**match, "match_type": "title_only"}]), candidates)
        self.assertEqual(promote_shared_first(candidates, [{**match, "chunk_id": "other"}]), candidates)
        self.assertEqual(promote_shared_first(candidates, []), candidates)
        self.assertEqual(promote_shared_first([], [match]), [])

    def test_mmarco_reordering_keeps_candidates_and_stable_ties(self):
        hits = [{"chunk_id": key, "content": text} for key, text in (("a", "first"), ("b", "second"), ("c", "third"))]
        result = rerank_with_mmarco(hits, [-3.0, 5.1, 5.1])
        self.assertEqual([(r["chunk_id"], r["content"]) for r in result], [("b", "second"), ("c", "third"), ("a", "first")])
        self.assertEqual([r["score"] for r in result], [5.1, 5.1, -3.0])
        for invalid in ([], [1.0, 2.0, float("nan")]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                rerank_with_mmarco(hits, invalid)

    def test_mmarco_scores_only_chinese_queries_and_cites_the_same_original_passages(self):
        class FakeCrossEncoder:
            def __init__(self):
                self.requests = []

            def score(self, query, hits):
                self.requests.append((query, len(hits)))
                return list(range(len(hits)))

        docs = [document("出口", "A gateway forwards packets."), document("续约", "Renew a lease.")]
        questions = [{"id": "zh", "query": "默认网关", "category": "direct", "language": "zh"},
                     {"id": "en", "query": "gateway", "category": "direct", "language": "en"}]
        reranker = FakeCrossEncoder()
        methods, _ = compare("one", "semantic-test", questions, docs, FakeEmbedder(), reranker=reranker)
        baseline = methods["local_multilingual_semantic_english_course"]["predictions"]
        ranked = methods["local_multilingual_semantic_english_course_mmarco"]
        self.assertEqual(reranker.requests, [("默认网关", 2)])
        self.assertEqual(ranked["predictions"]["en"], baseline["en"])
        self.assertTrue(ranked["settings"]["reranker_revision"])
        corpus = {doc["id"]: doc["content"] for doc in docs}
        self.assertEqual([valid_span(row, corpus) for row in ranked["predictions"]["zh"]],
                         [valid_span(row, corpus) for row in reversed(baseline["zh"])])

    def test_expanded_ranking_retrieves_beyond_the_first_three_and_can_keep_the_previous_first(self):
        class FakeCrossEncoder:
            def __init__(self):
                self.requests = []

            def score(self, query, hits):
                self.requests.append((query, len(hits)))
                return list(range(len(hits)))

        docs = [document(f"Note {i}", f"A gateway forwards packets in exercise {i}.") for i in range(6)]
        questions = [{"id": "zh", "query": "默认网关", "category": "direct", "language": "zh"},
                     {"id": "en", "query": "gateway", "category": "direct", "language": "en"}]
        reranker = FakeCrossEncoder()
        results, _ = compare("one", "semantic-test", questions, docs, FakeEmbedder(), reranker=reranker, rerank_depth=6)
        name = "local_multilingual_semantic_english_course_mmarco"
        narrow, expanded, anchored = (results[f"{name}{suffix}"]["predictions"] for suffix in ("", "_depth6", "_depth6_anchored"))
        self.assertEqual(reranker.requests, [("默认网关", 3), ("默认网关", 6)])
        self.assertEqual(expanded["en"], narrow["en"])
        self.assertEqual(anchored["en"], narrow["en"])
        self.assertFalse({row["chunk_id"] for row in expanded["zh"]} & {row["chunk_id"] for row in narrow["zh"]})
        self.assertEqual(anchored["zh"], [narrow["zh"][0], *expanded["zh"][:2]])
        self.assertEqual(results[f"{name}_depth6"]["settings"]["rerank_depth"], 6)
        self.assertEqual(results[f"{name}_depth6_anchored"]["settings"]["first_result"], "narrow_mmarco_first")
        corpus = {row["id"]: row["content"] for row in docs}
        self.assertTrue(all(valid_span(row, corpus) for row in anchored["zh"]))

    def test_long_passage_is_covered_without_context_truncation(self):
        text = "开头" + "x" * 580 + "gateway"
        windows = list(text_windows(text, lambda value: min(128, len(value) + 2)))
        self.assertTrue(windows[0].startswith("开头"))
        self.assertTrue(windows[-1].endswith("gateway"))
        self.assertTrue(all(len(window) + 2 <= 120 for window in windows))
        self.assertGreater(len(windows), 1)

    def test_tail_match_returns_original_chunk_and_exact_source_positions(self):
        docs = [document("租约", "lease renewal"), document("默认出口", "x" * 570 + " gateway")]
        passages = [part for doc in docs for part in knowledge.chunks(doc)]
        embedder = FakeEmbedder()
        index = SemanticIndex(passages, embedder)
        results = index.search("默认网关")
        self.assertEqual(results[0]["id"], docs[1]["id"])
        self.assertEqual(results[0]["content"], docs[1]["content"])
        self.assertEqual(len(results), len(passages))
        corpus = {doc["id"]: doc["content"] for doc in docs}
        self.assertIsNotNone(valid_span(results[0], corpus))

    def test_empty_scope_and_blank_query_do_not_generate_hits(self):
        embedder = FakeEmbedder()
        index = SemanticIndex([], embedder)
        self.assertEqual(index.search("默认网关"), [])
        self.assertEqual(embedder.inputs, [])
        index = SemanticIndex(list(knowledge.chunks(document("地址", "gateway"))), embedder)
        before = list(embedder.inputs)
        self.assertEqual(index.search("  "), [])
        self.assertEqual(embedder.inputs, before)

    def test_vector_errors_and_incomplete_embeddings_stop_the_run(self):
        for vector in ([], [0., 0.], [float("nan"), 1.], [float("inf"), 1.]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                unit(vector)
        class Missing(FakeEmbedder):
            def embed(self, texts, batch_size=16):
                return iter([])
        with self.assertRaises(ValueError):
            SemanticIndex(list(knowledge.chunks(document("地址", "gateway"))), Missing())

    def test_fusion_rewards_agreement_without_duplicate_votes(self):
        a, b, c = ({"chunk_id": key, "content": key} for key in ("a", "b", "c"))
        results = fuse_rankings([a, b], [b, c])
        self.assertEqual(results[0]["chunk_id"], "b")
        self.assertEqual([row["chunk_id"] for row in fuse_rankings([a, a], [b])], ["a", "b"])
        self.assertEqual(fuse_rankings([], []), [])

    def test_three_methods_share_sources_offsets_and_blind_scoring_format(self):
        docs = [document("默认出口", "The default gateway forwards packets."), document("地址分配", "A lease records a renewal.")]
        questions = [{"id": "q1", "query": "默认网关", "category": "同义", "language": "zh"}]
        embedder = FakeEmbedder()
        results, settings = compare("one", "semantic-test", questions, docs, embedder)
        self.assertEqual(len(results), 3)
        self.assertIsNone(settings["relevance_threshold"])
        corpus = {doc["id"]: doc["content"] for doc in docs}
        for result in results.values():
            self.assertEqual(result["version"], "one")
            self.assertEqual(result["scope"], "semantic-test")
            self.assertEqual(set(result["predictions"]), {"q1"})
            self.assertTrue(all(valid_span(hit, corpus) is not None for hit in result["predictions"]["q1"]))
            self.assertEqual([doc["id"] for doc in result["sources"]], [doc["id"] for doc in docs])
        self.assertEqual(results["local_multilingual_semantic"]["predictions"]["q1"][0]["id"], docs[0]["id"])
        self.assertTrue(all("labels" not in value for value in embedder.inputs))


if __name__ == "__main__":
    unittest.main()
