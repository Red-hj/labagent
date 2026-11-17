import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from labagent.engine import validate
from labagent.investigation import validate_open

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rag_evidence_cases', ROOT / 'scripts/verify_rag_evidence.py')
cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cases)


class RagEvidenceCaseTests(unittest.TestCase):
    def test_saved_inputs_use_the_public_open_diagnosis_contract(self):
        requests, expected = cases.prepare_cases(False)
        self.assertEqual(set(requests), set(expected))
        self.assertEqual(len(requests), 6)
        with patch('labagent.investigation.validate_open', wraps=validate_open) as check_open:
            for name, payload in requests.items():
                with self.subTest(case=name):
                    check_open.reset_mock()
                    validate(payload)
                    check_open.assert_called_once_with(payload)

    def test_missing_device_and_unspecified_area_are_not_leaked_into_case_materials(self):
        requests, _ = cases.prepare_cases(False)
        partial = requests['one-sided-with-course']
        self.assertEqual(set(partial['configs']), {'R1'})
        self.assertTrue(partial['course_documents'])
        self.assertFalse(requests['one-sided-no-course']['course_documents'])
        for name, payload in requests.items():
            with self.subTest(case=name):
                self.assertNotIn('expected_area', payload.get('requirements', {}))
                self.assertFalse(any('expected_area' in link for link in payload['topology']['links']))
        explicit, _ = cases.prepare_cases(True)
        self.assertNotIn('one-sided-no-course', explicit)
        self.assertIn('都在 Area 0', explicit['mismatch-planned-area-0']['question'])


if __name__ == '__main__':
    unittest.main()
