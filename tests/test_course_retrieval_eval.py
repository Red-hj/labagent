import unittest

from labagent import knowledge
from scripts.evaluate_course_retrieval import diagnose_misses, evaluate, merge_overlapping_hits, valid_span


class CourseRetrievalEvaluationTests(unittest.TestCase):
    def test_bilingual_candidate_matches_english_body_and_preserves_citable_offsets(self):
        documents = [knowledge.make_document({"title": title, "content": content}) for title, content in (
            ("Packet types", "\nA link-state advertisement (LSA) describes part of the topology."),
            ("Link-state advertisement", "The interface keeps a timer."),
            ("邻接", "邻居交换数据库描述报文。"))]
        baseline = knowledge.rank_documents("链路状态通告携带什么？", documents)
        result = knowledge.rank_documents("链路状态通告携带什么？", documents, bilingual_terms=True)
        self.assertEqual(baseline["results"], [])
        hit, = result["results"]
        self.assertEqual(hit["id"], documents[0]["id"])
        self.assertEqual(hit["match_type"], "body")
        self.assertEqual(result["match_summary"]["answerability"], "not_assessed")
        self.assertIsNotNone(valid_span(hit, {doc["id"]: doc["content"] for doc in documents}))

    def test_bilingual_candidate_distinguishes_designated_and_backup_roles(self):
        backup = knowledge.make_document({"title": "Backup", "content": "The backup designated router is available."})
        primary = knowledge.make_document({"title": "Primary", "content": "The designated router originates a network LSA."})
        documents = [backup, primary]
        for query, expected in (("备份指定路由器", backup), ("指定路由器", primary)):
            with self.subTest(query=query):
                result = knowledge.rank_documents(query, documents, bilingual_terms=True)
                hit, = result["results"]
                self.assertEqual(hit["id"], expected["id"])
        english = knowledge.rank_documents("Network LSA", documents)
        self.assertEqual(knowledge.rank_documents("Network LSA", documents, bilingual_terms=True)["results"], english["results"])

    def test_overlapping_chunks_preserve_cross_boundary_evidence_and_section_limits(self):
        evidence = "Step A completed.\nStep B completed."
        content = "# Lab\n## Verify\n" + "x" * 47 + evidence + "\n\n## Reset\nUse the original configuration."
        doc = knowledge.make_document({"title": "Exercise", "content": content})
        baseline = list(knowledge.chunks(doc, size=85))
        overlap = list(knowledge.chunks(doc, size=85, overlap=24))
        self.assertFalse(any(evidence in row["content"] for row in baseline))
        self.assertTrue(any(evidence in row["content"] for row in overlap))
        for row in overlap:
            self.assertLessEqual(len(row["content"]), 85)
            self.assertIsNotNone(valid_span(row, {doc["id"]: content}))
            if "## Reset" in row["content"]:
                self.assertTrue(row["content"].startswith("## Reset"))

    def test_diversification_prefers_distinct_results_within_the_same_relevance_tier(self):
        first = {"id": "one", "char_start": 0, "char_end": 600, "match_type": "body"}
        repeated = {**first, "char_start": 480, "char_end": 800}
        different = {**repeated, "id": "two"}
        adjacent = {**first, "char_start": 600, "char_end": 1000}
        title = {**first, "id": "three", "match_type": "title_only"}
        self.assertEqual(knowledge.select_distinct_spans([first, repeated, different, adjacent, title], 3),
                         [first, different, adjacent])
        self.assertEqual(knowledge.select_distinct_spans([first, repeated, title], 3), [first, repeated, title])
        self.assertEqual(knowledge.select_distinct_spans([first, repeated, title], 2), [first, repeated])

    def test_report_counts_questions_with_overlapping_valid_spans(self):
        content = "start to finish"
        first = {"id": "source", "char_start": 0, "char_end": 8, "line_start": 1, "line_end": 1, "content": content[:8]}
        repeated = {**first, "char_start": 5, "char_end": 15, "content": content[5:]}
        adjacent = {**repeated, "char_start": 8, "content": content[8:]}
        questions = [{"id": qid, "category": "direct", "language": "en"} for qid in ("q1", "q2", "q3")]
        predictions = {"q1": [first, repeated], "q2": [first, adjacent], "q3": [first, {**repeated, "line_end": 2}]}
        report = evaluate("one", "course", questions, {"version": "one", "cases": {q['id']: [] for q in questions}},
                          {"version": "one", "scope": "course", "method": "test", "predictions": predictions},
                          {}, {"source": content})
        self.assertEqual([row["overlapping_candidates"] for row in report["records"]], [True, False, False])
        self.assertEqual(report["all"]["overlapping_candidates"]["numerator"], 1)
        self.assertEqual(report["all"]["overlapping_candidates"]["denominator"], 3)

    def test_merged_hit_keeps_both_ends_of_the_evidence_and_original_offsets(self):
        content = "hello here\nsecond line\nthird line"
        corpus = {"guide": content, "separate": "a distinct source"}

        def hit(source, start, end):
            source_text = corpus[source]
            return {"id": source, "chunk_id": f"{source}:{start}-{end}", "char_start": start, "char_end": end,
                    "line_start": source_text.count("\n", 0, start) + 1, "line_end": source_text.count("\n", 0, end - 1) + 1,
                    "content": source_text[start:end]}

        original = [hit("guide", 0, 9), hit("guide", 12, 24), hit("guide", 8, 16), hit("separate", 0, 9)]
        merged, other = merge_overlapping_hits(original, corpus)
        self.assertEqual(valid_span(merged, corpus), ("guide", 0, 24))
        self.assertEqual(other, original[-1])
        self.assertTrue(all(row["content"] in merged["content"] for row in original[:-1]))
        adjacent = [hit("guide", 0, 8), hit("guide", 8, 13)]
        self.assertEqual(merge_overlapping_hits(adjacent, corpus), adjacent)

    def test_miss_analysis_distinguishes_split_evidence_from_ranking(self):
        boundary = knowledge.make_document({"title": "Boundary", "content": "x" * 580 + "\nfirst step\n" + "y" * 35})
        ranked = knowledge.make_document({"title": "Reference", "content": "hello " + "other info " * 30})
        distractors = [knowledge.make_document({"title": f"Packet {i}", "content": "hello hello"}) for i in range(3)]
        documents = [boundary, ranked, *distractors]
        corpus = {doc["id"]: doc["content"] for doc in documents}
        ids = {"boundary": boundary["id"], "ranked": ranked["id"]}
        questions = [{"id": name, "query": query, "category": "direct", "language": "en"}
                     for name, query in (("q1", "first step"), ("q2", "hello"), ("q3", "routerdeadinterval"))]
        labels = {"version": "one", "cases": {"q1": [{"document": "boundary", "start_line": 2, "end_line": 3}],
                  **{name: [{"document": "ranked", "start_line": 1, "end_line": 1}] for name in ("q2", "q3")}}}
        report = evaluate("one", "course", questions, labels, {"version": "one", "scope": "course",
                          "method": "local_bm25_with_fault_tags", "predictions": {q["id"]: [] for q in questions}}, ids, corpus)
        result = diagnose_misses(report, questions, labels, ids, corpus, documents)
        self.assertEqual(result["counts"], {"outside_chunks": 1, "rank_4_to_10": 1, "outside_top_10": 1, "missing_from_predictions": 0})
        self.assertEqual([item["rank_at_10"] for item in result["cases"]], [None, 4, None])

    def test_ranking_requires_full_support_and_verifies_source_offsets(self):
        prefix, evidence = 'chapter\n', 'supporting evidence'
        content = prefix + evidence + '\n'
        hit = {'id': 'source-1', 'char_start': len(prefix), 'char_end': len(prefix) + len(evidence),
               'line_start': 2, 'line_end': 2, 'content': evidence}
        partial = {**hit, 'char_end': hit['char_end'] - 1, 'content': evidence[:-1]}
        mislabeled = {**hit, 'line_end': 3}
        questions = [{'id': 'q1', 'category': 'direct', 'language': 'en'},
                     {'id': 'q2', 'category': 'unanswerable', 'language': 'zh'}]
        labels = {'version': 'one', 'cases': {'q1': [{'document': 'guide', 'start_line': 2, 'end_line': 2}], 'q2': []}}
        result = evaluate('one', 'course', questions, labels, {
            'version': 'one', 'scope': 'course', 'method': 'test', 'predictions': {
                'q1': [partial, mislabeled, hit], 'q2': [hit],
            }}, {'guide': 'source-1'}, {'source-1': content})
        self.assertEqual(result['records'][0]['rank'], 3)
        self.assertEqual(result['all']['top1']['numerator'], 0)
        self.assertEqual(result['all']['top3']['numerator'], 1)
        self.assertEqual(result['all']['source_alignment']['numerator'], 3)
        self.assertEqual(result['all']['source_alignment']['denominator'], 4)
        self.assertEqual(result['all']['unanswerable_returned']['numerator'], 1)

    def test_empty_result_on_unanswerable_question_is_recorded(self):
        questions = [{'id': 'q1', 'category': 'unanswerable', 'language': 'en'}]
        result = evaluate('one', 'course', questions, {'version': 'one', 'cases': {'q1': []}}, {
            'version': 'one', 'scope': 'course', 'method': 'test', 'predictions': {'q1': []},
        }, {}, {})
        self.assertEqual(result['all']['unanswerable_returned']['numerator'], 0)
        self.assertEqual(result['all']['unanswerable_returned']['denominator'], 1)
        self.assertIsNone(result['all']['top3']['rate'])
