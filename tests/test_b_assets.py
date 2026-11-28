import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
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

    def test_parsed_link_evidence_uses_endpoint_configuration_and_preserves_the_base_request(self):
        data = b.read(b.DATA / 'cases/15_multiarea.json')
        cases = {'case': {key: data[key] for key in ('configs', 'topology', 'requirements')}}
        base, = b.model_requests(cases, 'model-direct')
        enriched, = b.model_requests(cases, 'model-direct', link_evidence='parsed')
        evidence = json.loads(b.make_messages(enriched)[1]['content'])['parsed_links']
        self.assertEqual({key: enriched[key] for key in base}, base)
        self.assertNotIn('parsed_links', json.loads(b.make_messages(base)[1]['content']))
        checked, = b.model_requests(cases, 'model-direct', link_evidence='checked')
        self.assertEqual(b.make_messages(checked)[1], b.make_messages(enriched)[1])
        self.assertEqual(checked['instruction'], base['instruction'] + b.LINK_EVIDENCE_CHECKS)
        self.assertEqual([link.get('expected_ospf_area') for link in evidence], [None, '0.0.0.1'])
        policy, = b.parsed_link_evidence(b.read(b.DATA / 'cases/12_policy.json'))
        self.assertEqual(policy['expected_ospf_area'], '0.0.0.0')
        self.assertEqual([end['ospf_bindings'][0]['area'] for end in evidence[1]['endpoints']],
                         ['0.0.0.1', '0.0.0.1'])
        for end in evidence[1]['endpoints']:
            for row in end['config_lines'] + end['ospf_bindings'][0]['config_lines']:
                self.assertEqual(data['configs'][row['device']].splitlines()[row['line'] - 1].strip(), row['text'])

        unsupported = b.read(b.DATA / 'cases/11_unsupported.json')
        unknown = b.parsed_link_evidence(unsupported)[0]['endpoints'][1]
        self.assertEqual(len(unknown['unparsed_ospf_line_numbers']), 1)
        self.assertNotIn('DEMO_ONLY', json.dumps(unknown))
        missing = b.parsed_link_evidence(b.read(b.DATA / 'cases/10_missing.json'))[0]['endpoints'][1]
        self.assertEqual(missing['config_coverage'], 'device_config_missing')

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
        old_evidence = {'origin': 'configured_chat_completion', 'parsed_links': True}
        saved_model = {**model, 'external_prediction_source': old_evidence}
        knowledge_model = {**saved_model, 'mode': 'model-knowledge',
                           'external_prediction_source': {**old_evidence, 'link_evidence': 'parsed'}}
        self.assertIn('状态吻合', b.comparison([saved_model, knowledge_model]))
        knowledge_model['external_prediction_source']['link_evidence'] = 'checked'
        with self.assertRaisesRegex(ValueError, '链路证据选项'):
            b.comparison([saved_model, knowledge_model])

    def test_paired_comparison_reports_swapped_results_by_case_id(self):
        metrics = {key: b.score(1, 3) for key in (
            'state_agreement', 'strict_case_agreement', 'fault_location', 'normal_false_alarm', 'unknown_handling')}
        expected = {'status': 'awaiting_verification', 'findings': [], 'unknowns': [],
                    'family': 'test', 'condition': 'healthy', 'case_type': 'normal'}
        wrong = {**expected, 'status': 'fault_found'}
        def row(case_id, answer):
            return {'case_id': case_id, 'case_type': 'normal', **b.assess(expected, answer)}
        model = {'origin': 'configured_chat_completion', 'provider': 'test', 'model': 'test-model',
                 'base_inputs_sha256': 'cases', 'temperature': 0.1, 'max_tokens_per_call': 800}
        baseline = {'mode': 'model-knowledge', 'version': 'v1', 'manifest_sha256': 'inputs',
                    'labels_sha256': 'labels', 'candidate_count': 3, 'metrics': metrics,
                    'external_prediction_source': {**model, 'parsed_links': True},
                    'records': [row('b001', wrong), row('b002', expected), row('b003', None)]}
        variant = {**baseline, 'external_prediction_source': {**model, 'link_evidence': 'checked', 'claim_review': True},
                   'records': [row('b003', None), row('b002', wrong), row('b001', expected)]}
        text = b.paired_comparison(baseline, variant)
        self.assertIn('| 严格匹配 | 1/3（33.3%） | 1/3（33.3%） |', text)
        self.assertIn('由错变对 1 例，由对变错 1 例，均匹配 0 例，均未匹配 1 例', text)
        self.assertIn('| b001 | 正常 | 发现故障（状态不符） | 严格匹配 | 由错变对 |', text)
        self.assertIn('| b002 | 正常 | 严格匹配 | 发现故障（状态不符） | 由对变错 |', text)
        self.assertIn('| b003 | 正常 | 无有效预测 | 无有效预测 | 均未匹配 |', text)
        self.assertIn('| 链路证据 | 解析后的链路证据 | 链路证据与核对提示 |', text)
        self.assertIn('| 复核 | 无 | 回复复核 |', text)
        with self.assertRaisesRegex(ValueError, '输入子集'):
            b.paired_comparison(baseline, {**variant, 'external_prediction_source': {**model, 'base_inputs_sha256': 'other'}})
        with self.assertRaisesRegex(ValueError, '标签版本'):
            b.paired_comparison(baseline, {**variant, 'labels_sha256': 'other'})
        with self.assertRaisesRegex(ValueError, '相同模式'):
            b.paired_comparison(baseline, {**variant, 'mode': 'model-direct'})
        with self.assertRaisesRegex(ValueError, '重复'):
            b.paired_comparison(baseline, {**variant, 'records': [variant['records'][0]] * 3})
        with self.assertRaisesRegex(ValueError, '题号'):
            b.paired_comparison(baseline, {**variant, 'records': [row('b004', None), *variant['records'][1:]]})

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

    def test_independent_manifest_prepares_requests_and_scores_with_separate_labels(self):
        runtime = ROOT / 'runtime'
        runtime.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=runtime) as directory:
            folder = Path(directory)
            pack = folder / 'input-pack'
            (pack / 'inputs').mkdir(parents=True)
            case_path = pack / 'inputs/b001.json'
            source = b.read(b.DATA / 'cases/02_static_ok.json')
            payload = {**{key: source[key] for key in b.REQUEST_FIELDS if key != 'use_llm'}, 'use_llm': False}
            b.write(case_path, payload)
            b.write(pack / 'manifest.json', {
                'version': 'separate-v1', 'origin': 'public configuration fixture',
                'device_verified': 0, 'review_status': 'unittest',
                'cases': [{'id': 'b001', 'file': 'inputs/b001.json', 'sha256': b.digest(case_path)}],
            })
            requests = folder / 'requests.jsonl'
            b.prepare_model(requests, 'model-direct', benchmark_dir=pack)
            request = json.loads(requests.read_text(encoding='utf-8'))
            self.assertEqual(request['input'], payload)
            self.assertEqual(request['case_id'], 'b001')
            labels = folder / 'labels.json'
            label = {'status': 'awaiting_verification', 'findings': [], 'unknowns': [],
                     'family': 'linear', 'condition': 'healthy', 'case_type': 'normal'}
            output = folder / 'report'
            with self.assertRaisesRegex(ValueError, '--labels'):
                b.benchmark(output, 'rules', benchmark_dir=pack)
            b.write(labels, {'version': 'different', 'cases': {'b001': label}})
            with self.assertRaisesRegex(ValueError, '标签版本'):
                b.benchmark(output, 'rules', benchmark_dir=pack, labels_path=labels)
            self.assertFalse(output.exists())
            b.write(labels, {'version': 'separate-v1', 'cases': {'b001': label}})
            b.benchmark(output, 'rules', benchmark_dir=pack, labels_path=labels)
            report = b.read(output / 'evaluation.json')
            self.assertEqual(report['candidate_count'], 1)
            self.assertEqual(report['metrics']['strict_case_agreement'], b.score(1, 1))
            self.assertEqual(report['manifest_sha256'], b.digest(pack / 'manifest.json'))
            self.assertEqual(report['labels_sha256'], b.digest(labels))

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
