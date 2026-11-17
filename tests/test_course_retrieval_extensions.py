import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from labagent import knowledge
from scripts.evaluate_course_retrieval import evaluate, run_search

ROOT = Path(__file__).resolve().parents[1]


class CourseRetrievalExtensionsTests(unittest.TestCase):
    def setUp(self):
        self.document = knowledge.make_document({"title": "测试资料", "scope": "eval-course", "version": "one",
                                                 "source_name": "自编测试资料", "content": "# 标题 📘\n\n## 路径\n蓝杉核对回程路由。\n\n## 地址\n青柠核对租约续租。\n"})
        self.corpus = {self.document["id"]: self.document["content"]}
        self.ids = {"guide": self.document["id"]}
        self.hits = list(knowledge.chunks(self.document))
        self.questions = [{"id": "q1", "query": "蓝杉", "category": "路径", "language": "zh"},
                          {"id": "q2", "query": "无答案", "category": "资料不足", "language": "zh"}]
        self.label = {"document": "guide", "start_line": 4, "end_line": 4}
        self.labels = {"version": "one", "cases": {"q1": [self.label], "q2": []}}

    def grade(self, hits, *, questions=None, labels=None):
        predictions = {"version": "one", "scope": "eval-course", "method": "test", "predictions": hits}
        return evaluate("one", "eval-course", questions or self.questions, labels or self.labels,
                        predictions, self.ids, self.corpus)

    def test_rank_and_empty_metrics_keep_separate_denominators(self):
        report = self.grade({"q1": [self.hits[1], self.hits[0]], "q2": []})
        self.assertEqual(report["all"]["top1"]["rate"], 0)
        self.assertEqual(report["all"]["top3"]["rate"], 1)
        self.assertEqual(report["all"]["mrr_at_3"], .5)
        self.assertEqual(report["all"]["unanswerable_empty"]["rate"], 1)
        self.assertIsNone(report["by_category"]["资料不足"]["top3"]["rate"])

    def test_same_document_wrong_section_does_not_count(self):
        report = self.grade({"q1": [self.hits[1]], "q2": [self.hits[0]]})
        self.assertIsNone(report["records"][0]["rank"])
        self.assertEqual(report["all"]["unanswerable_empty"]["rate"], 0)
        self.assertEqual(report["all"]["source_alignment"]["rate"], 1)

    def test_fabricated_text_offsets_cross_scope_and_boolean_lines_are_invalid(self):
        for changes in ({"content": "伪造原文"}, {"char_start": 1}, {"char_start": True},
                        {"id": "foreign-course"}, {"line_start": True}, {"line_end": 99}):
            with self.subTest(changes=changes):
                report = self.grade({"q1": [{**self.hits[0], **changes}], "q2": []})
                self.assertEqual(report["all"]["top3"]["rate"], 0)
                self.assertEqual(report["records"][0]["invalid_citations"], 1)

    def test_invalid_candidate_keeps_its_rank(self):
        report = self.grade({"q1": [{**self.hits[0], "content": "伪造"}, self.hits[0]], "q2": []})
        self.assertEqual(report["records"][0]["rank"], 2)
        self.assertEqual(report["all"]["mrr_at_3"], .5)

    def test_partial_long_line_cannot_match_a_duplicate_quote_elsewhere(self):
        text = "共同引文\n" + "x" * 650 + "共同引文"
        self.corpus[self.document["id"]] = text
        hit = {"id": self.document["id"], "content": text[:600], "char_start": 0,
               "char_end": 600, "line_start": 1, "line_end": 2}
        labels = {"version": "one", "cases": {"q1": [{"document": "guide", "start_line": 2, "end_line": 2}], "q2": []}}
        report = self.grade({"q1": [hit], "q2": []}, labels=labels)
        self.assertIsNone(report["records"][0]["rank"])
        self.assertEqual(report["all"]["source_alignment"]["rate"], 1)

    def test_multiple_evidence_and_unanswerable_only_coverage(self):
        labels = {"version": "one", "cases": {"q1": [self.label, {"document": "guide", "start_line": 7, "end_line": 7}], "q2": []}}
        report = self.grade({"q1": [self.hits[0]], "q2": []}, labels=labels)
        self.assertEqual(report["all"]["evidence_coverage"]["rate"], .5)
        report = self.grade({"q2": []}, questions=[self.questions[1]], labels={"version": "one", "cases": {"q2": []}})
        self.assertIsNone(report["all"]["top3"]["rate"])
        self.assertIsNone(report["all"]["mrr_at_3"])
        self.assertIsNone(report["all"]["source_alignment"]["rate"])

    def test_duplicate_or_outdated_labels_and_missing_predictions_are_rejected(self):
        for labels in ({"version": "old", "cases": self.labels["cases"]},
                       {"version": "one", "cases": {"q1": [self.label]}},
                       {"version": "one", "cases": {"q1": [self.label, self.label], "q2": []}},
                       {"version": "one", "cases": {"q1": [{**self.label, "start_line": 99}], "q2": []}}):
            with self.subTest(labels=labels), self.assertRaises(ValueError):
                self.grade({"q1": [], "q2": []}, labels=labels)
        for hits in ({"q1": []}, {"q1": None, "q2": []}, {"q1": [self.hits[0]] * 4, "q2": []}):
            with self.subTest(hits=hits), self.assertRaises(ValueError):
                self.grade(hits)

    def test_search_freezes_corpus_and_sends_only_query_to_ranking(self):
        with patch("scripts.evaluate_course_retrieval.knowledge.corpus", return_value=([self.document], [])) as corpus, \
                patch("scripts.evaluate_course_retrieval.knowledge.rank_documents", wraps=knowledge.rank_documents) as rank:
            report = run_search("one", "eval-course", self.questions)
        corpus.assert_called_once_with("eval-course")
        self.assertEqual(rank.call_args_list[0].args, ("蓝杉", [self.document]))
        self.assertEqual(rank.call_args_list[0].kwargs, {"scope": "eval-course", "limit": 3})
        self.assertEqual(report["sources"][0]["content_hash"], self.document["content_hash"])

    def test_broken_corpus_is_reported_instead_of_becoming_empty_predictions(self):
        with patch("scripts.evaluate_course_retrieval.knowledge.corpus", return_value=([], ["校验错误"])), self.assertRaises(ValueError):
            run_search("one", "eval-course", self.questions)

    def test_team_package_search_and_score_cli_roundtrip_without_model_keys(self):
        base = ROOT / "runtime/tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            package = Path(folder)
            (package / "guide.md").write_text(self.document["content"], encoding="utf-8")
            source = {key: self.document[key] for key in ("title", "version", "source_name", "reference_url")}
            files = {"manifest.json": {"version": "one", "scope": "eval-course", "documents": [{**source, "key": "guide", "file": "guide.md"}]},
                     "questions.json": {"version": "one", "cases": self.questions}, "labels.json": self.labels}
            for name, value in files.items():
                (package / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            environment = {**os.environ, "LABAGENT_DATA_DIR": (package / "store").relative_to(ROOT).as_posix(),
                           "LABAGENT_LLM_KEY": "", "LABAGENT_LLM_URL": ""}
            prefix = [sys.executable, "-B", "-X", "utf8", "scripts/evaluate_course_retrieval.py"]
            predictions, output = package / "outputs/predictions.json", package / "outputs/report.json"
            search = subprocess.run(prefix + ["search", str(package), str(predictions)], cwd=ROOT, env=environment,
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(search.returncode, 0, search.stderr)
            command = prefix + ["score", str(package), str(output), "--labels", str(package / "labels.json"), "--predictions", str(predictions)]
            result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["all"]["top3"]["rate"], 1)
            repeated = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(repeated.returncode, 2)
            self.assertIn("输出文件已存在", repeated.stderr)
