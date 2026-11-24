import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts import evaluate_b as evaluator
from scripts import review_b_links as review


def example_request(name='15_multiarea'):
    case = evaluator.read(evaluator.DATA / 'cases' / f'{name}.json')
    payload = {key: case[key] for key in ('question', 'configs', 'topology', 'requirements')}
    request, = evaluator.model_requests({'b001': payload}, 'model-direct', link_evidence='checked')
    return request


def normal_prediction(case_id='b001'):
    return {'case_id': case_id, 'status': 'awaiting_verification', 'findings': [], 'unknowns': [],
            'elapsed_ms': 22, 'call': {'usage': {'total_tokens': 30}}, 'raw_response': 'initial'}


def shutdown_prediction():
    return {**normal_prediction(), 'status': 'fault_found',
            'findings': [{'code': 'INTERFACE_SHUTDOWN', 'link_id': 'r2-r3', 'devices': ['R2']}]}


class LinkReviewTests(unittest.TestCase):
    def test_detects_conflicting_status_and_unsubstantiated_shutdown(self):
        record = example_request()
        good = normal_prediction()
        self.assertEqual(review.claim_issues(record, good), [])
        self.assertIn('明细不一致', review.claim_issues(record, {**good, 'status': 'fault_found'})[0])
        self.assertIn('显式 shutdown', review.claim_issues(record, shutdown_prediction())[0])

        subnet_case = example_request('14_multiple')
        subnet = {**good, 'status': 'fault_found',
                  'findings': [{'code': 'SUBNET_MISMATCH', 'link_id': 'r1-r2', 'devices': ['R2']}]}
        self.assertIn('同时列出两个端点', review.claim_issues(subnet_case, subnet)[0])
        subnet['findings'][0]['devices'].append('R1')
        self.assertEqual(review.claim_issues(subnet_case, subnet), [])

    def test_review_keeps_unflagged_predictions_and_counts_both_calls(self):
        bad = shutdown_prediction()
        good = normal_prediction('b002')
        request = example_request()
        expected = {**normal_prediction(), 'elapsed_ms': 25}
        completion = Mock(return_value={'content': json.dumps(expected), '_meta': {'usage': {'total_tokens': 60}}})
        runtime = review.ROOT / 'runtime'
        runtime.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=runtime) as folder:
            root = Path(folder)
            requests = root / 'requests.jsonl'
            inputs = root / 'predictions.jsonl'
            output = root / 'reviewed.jsonl'
            requests.write_text('\n'.join(map(json.dumps, [request, {**request, 'case_id': 'b002'}])), encoding='utf-8')
            inputs.write_text('\n'.join(map(json.dumps, [bad, good])), encoding='utf-8')
            settings = {'provider': 'test', 'model': 'test-model'}
            evaluator.write(inputs.with_suffix('.meta.json'), {**settings, 'max_tokens_per_call': 800,
                                                               'requests_sha256': evaluator.digest(requests)})
            plan = review.review_run(requests, inputs, None)
            self.assertEqual(plan['additional_calls'], 1)
            with patch.object(evaluator, 'load_model_settings'), patch.object(review.model, 'configured', return_value=True), \
                    patch.object(review.model, 'public_config', return_value=settings):
                result = review.review_run(requests, inputs, output, execute=True, completion=completion)
            self.assertEqual(result['accepted'], 1)
            completion.assert_called_once()
            messages, = completion.call_args.args
            self.assertEqual(messages[1], evaluator.make_messages(request)[1])
            self.assertIn('INTERFACE_SHUTDOWN', messages[0]['content'])
            selection, unchanged = (json.loads(line) for line in output.read_text(encoding='utf-8').splitlines())
            self.assertEqual(unchanged, good)
            self.assertEqual(selection['status'], 'awaiting_verification')
            usage = evaluator.summarize_runtime({'b001': {'result': selection}})
            self.assertEqual(usage['usage']['total_tokens'], {'total': 90, 'reported_calls': 2})
            self.assertEqual(evaluator.read(output.with_suffix('.meta.json'))['attempted_calls'], 3)

    def test_structure_normalization_preserves_claims_and_checks_their_evidence(self):
        normal = example_request()
        unsupported_status = {**normal_prediction(), 'status': 'fault_found'}
        self.assertEqual(review.normalize_prediction(normal, unsupported_status)['status'], 'awaiting_verification')
        self.assertTrue(review.claim_issues(normal, review.normalize_prediction(normal, shutdown_prediction())))
        mismatch = example_request('14_multiple')
        original = {**normal_prediction(), 'status': 'fault_found',
                    'findings': [{'code': 'SUBNET_MISMATCH', 'link_id': 'r1-r2', 'devices': ['R2']}]}
        corrected = review.normalize_prediction(mismatch, original)
        self.assertEqual(corrected['findings'][0]['devices'], ['R1', 'R2'])
        self.assertEqual(original['findings'][0]['devices'], ['R2'])
        self.assertEqual(review.claim_issues(mismatch, corrected), [])

    def test_persistent_evidence_conflict_keeps_the_initial_prediction(self):
        request = example_request()
        bad = shutdown_prediction()
        completion = Mock(return_value={'content': json.dumps(bad), '_meta': {'usage': {'total_tokens': 60}}})
        row, trace = review.revise_prediction(request, bad, review.claim_issues(request, bad), 800, completion)
        self.assertFalse(trace['accepted'])
        self.assertEqual(row['raw_response'], bad['raw_response'])
        self.assertEqual(row['additional_call']['usage']['total_tokens'], 60)


if __name__ == '__main__':
    unittest.main()
