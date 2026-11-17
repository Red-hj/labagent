import unittest

from scripts.evaluate_course_retrieval import evaluate


class CourseRetrievalEvaluationTests(unittest.TestCase):
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
