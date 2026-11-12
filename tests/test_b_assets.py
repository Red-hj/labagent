import importlib.util
from pathlib import Path
import unittest

from labagent.engine import diagnose


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('b_evaluator', ROOT / 'scripts/evaluate_b.py')
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)


class BEvaluationTests(unittest.TestCase):
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
