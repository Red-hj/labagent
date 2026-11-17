import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from labagent import knowledge
from labagent.retrieval_evaluation import evaluate

ROOT = Path(__file__).resolve().parents[1]


class RetrievalEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.document = knowledge.make_document({"title": "测试资料", "scope": "eval-test", "version": "v1",
                                                 "source_name": "自编测试资料", "content": "# 标题 📘\n\n## 路径\n蓝杉核对回程路由。\n\n## 地址\n青柠核对租约续租。\n"})
        self.documents = [self.document]
        self.hits = list(knowledge.chunks(self.document))
        self.questions = [{"id": "q1", "query": "蓝杉", "category": "路径"},
                          {"id": "q2", "query": "无答案", "category": "空结果"}]
        self.label = {"document_id": self.document["id"], "line_start": 4, "line_end": 4, "quote": "蓝杉核对回程路由"}
        self.labels = {"q1": [self.label], "q2": []}

    def run_evaluation(self, results, **changes):
        retrieve = Mock(side_effect=lambda query, *, limit: {"method": "test", "results": results[query]})
        report = evaluate(changes.get("questions", self.questions), changes.get("labels", self.labels),
                          self.documents, retrieve)
        return report, retrieve

    def test_rank_metrics_empty_questions_and_query_label_separation(self):
        report, retrieve = self.run_evaluation({"蓝杉": [self.hits[1], self.hits[0]], "无答案": []})
        self.assertEqual(report["metrics"]["hit_at_1"]["rate"], 0)
        self.assertEqual(report["metrics"]["hit_at_k"]["rate"], 1)
        self.assertEqual(report["metrics"]["mrr_at_k"], .5)
        self.assertEqual(report["metrics"]["empty_on_unanswerable"]["rate"], 1)
        self.assertEqual(report["metrics"]["source_validity"]["count"], 2)
        self.assertIsNone(report["by_category"]["空结果"]["hit_at_k"]["rate"])
        self.assertEqual(retrieve.call_args_list[0].args, ("蓝杉",))
        self.assertEqual(retrieve.call_args_list[0].kwargs, {"limit": 3})

    def test_same_document_wrong_section_does_not_count_as_hit(self):
        report, _ = self.run_evaluation({"蓝杉": [self.hits[1]], "无答案": [self.hits[0]]})
        self.assertIsNone(report["records"][0]["rank"])
        self.assertEqual(report["metrics"]["empty_on_unanswerable"]["rate"], 0)
        self.assertEqual(report["metrics"]["source_validity"]["rate"], 1)

    def test_fabricated_text_bad_offsets_and_cross_scope_never_count(self):
        for changes in ({"content": self.hits[0]["content"] + "伪造答案"}, {"line_start": 2},
                        {"char_start": 1}, {"char_start": True}, {"id": "another-course"}):
            with self.subTest(changes=changes):
                report, _ = self.run_evaluation({"蓝杉": [{**self.hits[0], **changes}], "无答案": []})
                self.assertEqual(report["metrics"]["hit_at_k"]["rate"], 0)
                self.assertEqual(report["metrics"]["source_validity"]["rate"], 0)
                self.assertTrue(report["records"][0]["results"][0]["source_error"])

    def test_invalid_result_keeps_its_rank_instead_of_promoting_next_hit(self):
        report, _ = self.run_evaluation({"蓝杉": [{**self.hits[0], "content": "伪造"}, self.hits[0]], "无答案": []})
        self.assertEqual(report["records"][0]["rank"], 2)
        self.assertEqual(report["metrics"]["mrr_at_k"], .5)

    def test_long_partial_line_does_not_match_an_identical_quote_on_another_line(self):
        text = "共同引文\n" + "x" * 650 + "共同引文"
        self.document["content"] = text
        hit = {"id": self.document["id"], "content": text[:600], "char_start": 0,
               "char_end": 600, "line_start": 1, "line_end": 2}
        labels = {"q1": [{**self.label, "line_start": 2, "line_end": 2, "quote": "共同引文"}], "q2": []}
        report, _ = self.run_evaluation({"蓝杉": [hit], "无答案": []}, labels=labels)
        self.assertIsNone(report["records"][0]["rank"])
        self.assertEqual(report["metrics"]["source_validity"]["rate"], 1)

    def test_missing_labels_duplicate_ids_stale_quotes_and_foreign_docs_stop_before_retrieval(self):
        for questions, labels in (
            (self.questions, {"q1": [self.label]}),
            (self.questions + [self.questions[0]], self.labels),
            ([{**self.questions[0], "answer": "leak"}], {"q1": [self.label]}),
            (self.questions, {**self.labels, "q1": [{**self.label, "quote": "另一版本的内容"}]}),
            (self.questions, {**self.labels, "q1": [{**self.label, "document_id": "foreign-course"}]}),
            (self.questions, {**self.labels, "q1": [self.label, self.label]}),
        ):
            retrieve = Mock()
            with self.subTest(questions=questions, labels=labels), self.assertRaises(ValueError):
                evaluate(questions, labels, self.documents, retrieve)
            retrieve.assert_not_called()

    def test_evidence_coverage_and_unanswerable_only_denominators(self):
        labels = {**self.labels, "q1": [self.label, {"document_id": self.document["id"], "line_start": 7,
                                                     "line_end": 7, "quote": "青柠核对租约续租"}]}
        report, _ = self.run_evaluation({"蓝杉": [self.hits[0]], "无答案": []}, labels=labels)
        self.assertEqual(report["metrics"]["evidence_recall_at_k"]["rate"], .5)
        report, _ = self.run_evaluation({"无答案": []}, questions=[self.questions[1]], labels={"q2": []})
        self.assertIsNone(report["metrics"]["hit_at_k"]["rate"])
        self.assertIsNone(report["metrics"]["mrr_at_k"])
        self.assertIsNone(report["metrics"]["source_validity"]["rate"])

    def test_retriever_failures_are_reported_instead_of_becoming_empty_results(self):
        for response in ({"method": "test", "results": None},
                         {"method": "test", "results": [], "warnings": ["资料校验失败"]},
                         {"method": "test", "results": [self.hits[0]] * 4},
                         {"results": []}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                evaluate(self.questions, self.labels, self.documents, Mock(return_value=response))

    def test_production_ranking_and_frozen_corpus_have_identical_results(self):
        documents, warnings = knowledge.corpus("b-ospf")
        query = "最长前缀路由"
        self.assertEqual(knowledge.search(query, scope="b-ospf"),
                         knowledge.rank_documents(query, documents, scope="b-ospf", warnings=warnings))
        report = evaluate([self.questions[0]], {"q1": [self.label]}, self.documents,
                          lambda query, *, limit: knowledge.rank_documents(query, self.documents, limit=limit))
        self.assertEqual(report["sources"][0]["content_hash"], self.document["content_hash"])
        self.assertEqual(report["sources"][0]["version"], "v1")

    def test_public_example_cli_writes_report_without_touching_imported_notes(self):
        base = ROOT / "runtime/tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            target = Path(folder) / "evaluation"
            command = [sys.executable, "-X", "utf8", "scripts/evaluate_retrieval.py", "--questions",
                       "data/examples/retrieval_questions.json", "--labels", "data/examples/retrieval_labels.json", "--output", str(target)]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((target / "evaluation.json").read_text(encoding="utf-8"))
            self.assertEqual(report["question_count"], 4)
            self.assertEqual(report["metrics"]["hit_at_k"]["total"], 2)
            self.assertEqual(report["metrics"]["empty_on_unanswerable"]["total"], 2)
            self.assertIn("不是独立课程评测", (target / "报告.md").read_text(encoding="utf-8"))
            second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(second.returncode, 2)
            self.assertIn("输出目录已存在", second.stderr)


if __name__ == "__main__":
    unittest.main()
