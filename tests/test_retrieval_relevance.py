"""Self-authored regressions for lexical noise and reference/evidence boundaries."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from labagent import knowledge
from labagent.investigation import execute_tool, investigate, source_bundle
from labagent.report import generate_report
from scripts.evaluate_course_retrieval import run_search, valid_span
from test_investigation import ENV, completion, stages


def document(title, content, **changes):
    return knowledge.make_document({"title": title, "content": content, "scope": "relevance-test", "version": "one",
                                    "source_name": "Self-authored regression fixture", **changes})


def search(query, documents, **kwargs):
    return knowledge.rank_documents(query, documents, scope="relevance-test", **kwargs)


class RelevanceTests(unittest.TestCase):
    def setUp(self):
        self.documents = [document("Neighbor adjacency", "The OSPF neighbor process uses Hello packets. The routers compare area identifiers."),
                          document("Lease renewal", "The DHCP server assigns an address. A client must renew the lease."),
                          document("Route selection", "A gateway forwards packets using the longest matching prefix. The return route must exist.")]

    def test_function_words_do_not_return_unrelated_network_notes(self):
        for query in ("How to bake a chocolate cake?", "What is the capital of Iceland?", "Could you please explain watercolor pigments?",
                      "如何", "是否", "the and what is"):
            with self.subTest(query=query):
                found = search(query, self.documents)
                self.assertEqual(found["results"], [])
                self.assertEqual(found["match_summary"]["status"], "no_match")

    def test_network_questions_keep_supporting_passage_and_source_offsets(self):
        for query, expected in (("How does an OSPF neighbor use Hello packets?", 0),
                                ("Why must a client renew the DHCP lease?", 1),
                                ("How is the longest matching prefix used by a gateway?", 2)):
            with self.subTest(query=query):
                found = search(query, self.documents)
                self.assertEqual(found["results"][0]["id"], self.documents[expected]["id"])
                corpus = {doc["id"]: doc["content"] for doc in self.documents}
                self.assertTrue(all(valid_span(hit, corpus) for hit in found["results"]))

    def test_body_match_precedes_a_high_scoring_title_only_match(self):
        documents = [document("gateway " * 10, "Lease renewal records."), document("Forwarding", "Use the gateway for forwarding.")]
        hits = search("gateway", documents)["results"]
        self.assertEqual(hits[0]["id"], documents[1]["id"])
        self.assertEqual(hits[1]["match_type"], "title_only")

    def test_title_only_result_is_available_but_not_presented_as_answer_support(self):
        found = search("VLAN", [document("VLAN course", "An introductory reading list.")])
        self.assertEqual(len(found["results"]), 1)
        self.assertEqual(found["match_summary"]["status"], "title_only")
        self.assertEqual(found["match_summary"]["answerability"], "not_assessed")

    def test_related_words_do_not_imply_that_a_different_question_is_answerable(self):
        found = search("Which quantum authentication algorithm does OSPF use?", self.documents)
        self.assertTrue(found["results"])
        self.assertEqual(found["match_summary"]["answerability"], "not_assessed")
        self.assertNotIn("quantum", found["results"][0]["content"])

    def test_fault_tags_work_with_empty_query_without_changing_original_text(self):
        document = {**deepcopy(self.documents[0]), "codes": ["AREA_MISMATCH"]}
        found = search("", [document], codes=["AREA_MISMATCH"])
        self.assertEqual(found["results"][0]["match_type"], "fault_tag")
        self.assertEqual(found["results"][0]["content"], document["content"])

    def test_negation_protocol_names_and_state_numbers_are_retained(self):
        self.assertEqual(set(knowledge.search_tokens("OSPF not Full; no 2-Way")), {"ospf", "not", "full", "no", "2", "way"})

    def test_empty_corpus_is_an_explicit_no_match(self):
        found = search("OSPF", [])
        self.assertEqual(found["results"], [])
        self.assertEqual(found["match_summary"]["status"], "no_match")

    def test_protocol_and_qos_names_are_not_removed_as_english_function_words(self):
        documents = [document("Link-state protocol", "IS-IS exchanges link-state information."),
                     document("Controller network", "CAN bus arbitration."),
                     document("Traffic classes", "BE is a traffic service class.")]
        for query, expected in (("What is IS-IS?", 0), ("IS-IS协议是什么", 0), ("ISIS", 0), ("CAN", 1), ("BE", 2)):
            with self.subTest(query=query):
                self.assertEqual(search(query, documents)["results"][0]["id"], documents[expected]["id"])

    def test_model_tool_receives_the_same_matching_boundary_as_the_search_api(self):
        payload = stages()[0]
        sources, _ = source_bundle(payload)
        with patch("labagent.knowledge.corpus", return_value=(self.documents, [])):
            result, terminal = execute_tool("retrieve_reference", {"query": "OSPF"}, payload, sources, set())
        self.assertFalse(terminal)
        self.assertEqual(result["match_summary"]["answerability"], "not_assessed")
        self.assertEqual(result["results"][0]["match_type"], "body")

    def test_offline_predictions_record_matching_settings_without_reading_labels(self):
        questions = [{"id": "q1", "query": "OSPF"}, {"id": "q2", "query": "What is the capital of Iceland?"}]
        with patch("labagent.knowledge.corpus", return_value=(self.documents, [])):
            result = run_search("one", "relevance-test", questions)
        self.assertEqual(result["predictions"]["q2"], [])
        self.assertEqual(result["match_summaries"]["q2"]["status"], "no_match")
        self.assertTrue(result["settings"]["body_before_title_only"])

    def test_diagnosis_preserves_reference_search_metadata_and_report_labels(self):
        def complete(messages):
            if not any(m["role"] == "tool" for m in messages):
                return {"tool_calls": [{"id": "reference", "function": {"name": "retrieve_reference", "arguments": json.dumps({"query": "OSPF"})}}]}
            if messages[-1]["role"] == "tool" and "results" in json.loads(messages[-1]["content"]):
                return completion([{"role": "user", "content": "read current materials"}])
            return completion(messages)
        with patch.dict("os.environ", ENV), patch("labagent.knowledge.corpus", return_value=(self.documents, [])):
            result = investigate(stages()[0], completion=complete)
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertEqual(result["knowledge_retrieval"]["searches"][0]["answerability"], "not_assessed")
        # A report retains the retrieved text as reference material, rather than declaring it proof.
        history = {"attempts": [{"created_at": result["created_at"], "input": stages()[0], "result": result}],
                   "learning_record": {"requests": 1, "config_revisions": 1, "max_hint_level": 2}, "session_id": "a" * 32}
        report = generate_report(history)
        self.assertIn("## 检索参考资料", report)
        self.assertIn("候选片段", report)


if __name__ == "__main__":
    unittest.main()
