import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.evaluate_b import load_candidate_inputs, read
from scripts.prepare_ospf_training import ROOT, build_dataset, topology_signature, write_validation_benchmark


class OspfTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batches, cls.metadata, cls.fingerprints, _ = build_dataset()
        cls.examples = {}
        for rows in cls.batches.values():
            for row in rows:
                messages = row['messages']
                inputs, answer = (json.loads(message['content']) for message in messages[1:])
                cls.examples[inputs['case_id']] = (inputs, answer)

    def test_splits_have_nonoverlapping_topologies_and_cases(self):
        self.assertEqual({key: len(rows) for key, rows in self.batches.items()}, {'train': 30, 'validation': 10})
        self.assertEqual(len(self.fingerprints), len(self.examples))
        signatures = {split: {topology_signature(self.examples[row['case_id']][0])
                              for row in self.metadata if row['split'] == split} for split in self.batches}
        self.assertFalse(signatures['train'] & signatures['validation'])

    def test_validation_benchmark_keeps_labels_outside_model_inputs(self):
        runtime = ROOT / 'runtime'
        runtime.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=runtime) as directory:
            output = Path(directory)
            count = write_validation_benchmark(output, self.batches['validation'], self.metadata)
            manifest, inputs = load_candidate_inputs(output / 'inputs-pack')
            labels = read(output / 'labels.json')
            self.assertEqual(count, 10)
            self.assertEqual(set(inputs), set(labels['cases']))
            self.assertEqual(manifest['version'], labels['version'])
            self.assertEqual({row['family'] for row in labels['cases'].values()}, {'ladder'})
            self.assertEqual({topology_signature(row) for row in inputs.values()}, {(2, 2, 2, 2, 3, 3)})

    def test_chat_targets_retain_exact_source_lines(self):
        for rows in self.batches.values():
            for row in rows:
                messages = row['messages']
                self.assertEqual([message['role'] for message in messages], ['system', 'user', 'assistant'])
                inputs, answer = (json.loads(message['content']) for message in messages[1:])
                self.assertEqual(inputs['case_id'], answer['case_id'])
                self.assertTrue(answer['next_steps'])
                for item in answer['evidence']:
                    self.assertEqual(item['text'], inputs['configs'][item['device']].splitlines()[item['line'] - 1])

    def test_area_policy_and_missing_configuration_have_distinct_targets(self):
        for row in self.metadata:
            _, answer = self.examples[row['case_id']]
            scenario = row['scenario']
            if scenario == 'area_policy':
                self.assertEqual([item['code'] for item in answer['findings']], ['AREA_POLICY_VIOLATION'] * 2)
            elif scenario == 'area_mismatch':
                self.assertEqual({item['code'] for item in answer['findings']}, {'AREA_MISMATCH', 'AREA_POLICY_VIOLATION'})
            elif scenario == 'valid_multiarea':
                self.assertEqual(answer['status'], 'awaiting_verification')
            elif scenario.startswith('missing_'):
                self.assertEqual(answer['status'], 'needs_information')
                self.assertFalse(answer['findings'])
