import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from labagent.engine import diagnose


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('b_evaluator', ROOT / 'scripts/evaluate_b.py')
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)


class BEvaluationTests(unittest.TestCase):
    def test_explicit_env_uses_only_its_model_settings_without_copying(self):
        path = Mock()
        overrides = {'LABAGENT_LLM_URL': 'https://example.invalid/chat', 'LABAGENT_LLM_MODEL': 'demo-model',
                     'LABAGENT_LLM_KEY': 'test-value', 'PORT': '9999'}
        with patch.dict(os.environ, {'LABAGENT_LLM_KEY': 'old', 'LABAGENT_LLM_THINKING': 'old', 'PORT': '8120'}, clear=True), \
                patch.object(b.model, 'read_env', return_value=overrides):
            b.load_model_settings(path)
            self.assertEqual(os.environ['LABAGENT_LLM_KEY'], 'test-value')
            self.assertEqual(os.environ['PORT'], '8120')
            self.assertNotIn('LABAGENT_LLM_THINKING', os.environ)
        with patch.object(b.model, 'read_env', return_value={'LABAGENT_LLM_MODEL': 'demo-model'}):
            with self.assertRaisesRegex(ValueError, 'LABAGENT_LLM_KEY'):
                b.load_model_settings(path)

    def test_single_model_probe_excludes_content_and_benchmark_data(self):
        completion = Mock(return_value={'content': 'unused provider text', '_meta': {
            'provider': 'DeepSeek', 'model': 'deepseek-flash', 'elapsed_ms': 19, 'usage': {'total_tokens': 9}}})
        result = b.probe_model(completion)
        self.assertEqual(result, {'status': 'ok', 'provider': 'DeepSeek', 'model': 'deepseek-flash',
                                  'elapsed_ms': 19, 'usage': {'total_tokens': 9}})
        completion.assert_called_once_with([{'role': 'user', 'content': 'Connectivity check. Reply only: OK'}], max_tokens=128)

    def test_knowledge_uses_per_case_materials_with_same_base_prompt(self):
        inputs = {'b001': {'question': '检查地址', 'configs': {'R1': 'interface GE0/0/0\n ip address 10.1.1.1 24'}},
                  'b002': {'question': '检查区域', 'configs': {'R2': 'ospf 1\n area 1\n authentication cipher dont-index'}}}
        query = Mock(return_value={'results': []})
        with patch.object(b.knowledge, 'search', query):
            direct = list(b.model_requests(inputs, 'model-direct'))
            enhanced = list(b.model_requests(inputs, 'model-knowledge'))
        queries = [call.args[0] for call in query.call_args_list]
        self.assertIn('10.1.1.1', queries[0])
        self.assertIn('area 1', queries[1])
        self.assertNotIn('dont-index', queries[1])
        for a, k in zip(direct, enhanced):
            self.assertEqual(b.make_messages(a)[0], b.make_messages(k)[0])
            self.assertEqual(a['input'], k['input'])
            self.assertNotIn('retrieval_query', b.make_messages(k)[1]['content'])

    def test_model_row_and_missing_usage(self):
        record = {'case_id': 'b001', 'instruction': 'JSON', 'input': {}}
        raw = '{"case_id":"b001","status":"fault_found","findings":[],"unknowns":[]}'
        call = Mock(return_value={'content': '```json\n' + raw + '\n```', '_meta': {'usage': {'total_tokens': 122}}})
        row = b.predict(record, 800, call)
        self.assertEqual(row['status'], 'fault_found')
        self.assertEqual(call.call_args.kwargs, {'max_tokens': 800})
        bad = b.predict(record, 800, Mock(return_value={'content': raw.replace('b001', 'b002')}))
        self.assertEqual(bad['request_error'], 'invalid_json_or_case_id')
        stats = b.summarize_runtime({str(i): {'result': r, 'elapsed_ms': r['elapsed_ms']} for i, r in enumerate((row, bad))})
        self.assertEqual(stats['usage']['total_tokens'], {'total': 122, 'reported_calls': 1})

    def test_comparison_keeps_unrun_modes_pending_and_rejects_mixed_labels(self):
        fields = ('state_agreement', 'strict_case_agreement', 'fault_location', 'normal_false_alarm', 'unknown_handling')
        rule = {'mode': 'rules', 'version': 'v1', 'manifest_sha256': 'inputs', 'labels_sha256': 'labels',
                'candidate_count': 60, 'metrics': {key: b.score(1, 2) for key in fields}}
        model = {**rule, 'mode': 'model-direct'}
        self.assertIn('| 状态吻合 | 1/2（50.0%） | 待运行 | 待运行 |', b.comparison([rule]))
        self.assertIn('| 状态吻合 | 1/2（50.0%） | 1/2（50.0%） | 待运行 |', b.comparison([rule, model]))
        with self.assertRaisesRegex(ValueError, '标签版本'):
            b.comparison([rule, {**model, 'labels_sha256': 'other'}])

    @unittest.skipUnless((b.PRIVATE / 'diagnosis_labels.json').is_file(), 'Private B benchmark not distributed')
    def test_benchmark_fixed_split_and_no_answer_fields(self):
        from collections import Counter
        manifest, inputs = b.load_candidate_inputs()
        labels = b.read(b.PRIVATE / 'diagnosis_labels.json')['cases']
        self.assertEqual(set(inputs), set(labels))
        self.assertEqual(manifest['device_verified'], 0)
        self.assertEqual(Counter(row['case_type'] for row in labels.values()), {'fault': 30, 'normal': 18, 'unknown': 12})
        self.assertEqual(Counter(row['family'] for row in labels.values()), {name: 10 for name in {row['family'] for row in labels.values()}})
        self.assertEqual(len({row['family'] for row in labels.values()}), 6)
        for case in inputs.values():
            self.assertEqual(set(case), b.REQUEST_FIELDS)
            self.assertNotIn('observations', case)

    def test_strict_judgment_detects_wrong_side_missing_answer_and_extra_finding(self):
        expected = {'status': 'fault_found', 'findings': [{'code': 'AREA_POLICY_VIOLATION', 'link_id': 'L1', 'devices': ['R2']}], 'unknowns': []}
        bad = {**expected, 'findings': [{**expected['findings'][0], 'devices': ['R1']}]}
        self.assertTrue(b.assess(expected, bad)['state_match'])
        self.assertFalse(b.assess(expected, bad)['strict_match'])
        self.assertFalse(b.assess(expected, None)['strict_match'])
        extra = {**expected, 'findings': expected['findings'] + [{**expected['findings'][0], 'link_id': 'L2'}]}
        self.assertFalse(b.assess(expected, extra)['strict_match'])

    def test_first_five_and_repaired_configs(self):
        folders = sorted((b.DATA / 'team_cases').glob('lab_*'))
        self.assertEqual(len(folders), 5)
        for folder in folders:
            with self.subTest(case=folder.name):
                case = b.read(folder / 'input.json')
                self.assertNotIn('expected', case)
                self.assertTrue(b.assess(b.read(folder / 'expected.json'), diagnose(case))['strict_match'])
                repaired = diagnose(b.read(folder / 'repaired.json'))
                self.assertEqual(repaired['status'], 'awaiting_verification')
                self.assertEqual(repaired['findings'], [])

    def test_area_case_changes_interlink_only(self):
        from labagent.parser import ospf_bindings, parse_config
        case = b.read(b.DATA / 'team_cases/lab_02/input.json')
        device = parse_config('R2', case['configs']['R2'])
        self.assertEqual(ospf_bindings(device, device.interfaces['ge0/0/0'])[0][1], '0.0.0.1')
        self.assertEqual(ospf_bindings(device, device.interfaces['ge0/0/2'])[0][1], '0.0.0.0')
