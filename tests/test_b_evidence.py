"""Synthetic fixtures for the evidence checker; these are not experiment captures."""
import unittest

from scripts.verify_b_evidence import ROOT, capture_time, check_stage, read_json


LAB = ROOT / 'data/team_cases/lab_02'
NORMAL = {'status': 'awaiting_verification', 'findings': [], 'unknowns': []}
HEADER = 'Destination/Mask Proto Pre Cost Flags NextHop Interface\n'


def fixture(healthy):
    case = read_json(LAB / ('repaired.json' if healthy else 'input.json'))
    files = {f'{router}-config.txt': config for router, config in case['configs'].items()}
    for router, local, remote, rid, prefix in (
        ('R1', '10.12.0.1', '10.12.0.2', '2.2.2.2', '192.168.20.0/24'),
        ('R2', '10.12.0.2', '10.12.0.1', '1.1.1.1', '192.168.10.0/24'),
    ):
        files[f'{router}-ospf.txt'] = (f'Area 0.0.0.0 interface {local}\nRouter ID: {rid} Address: {remote}\nState: Full\n'
                                       if healthy else 'OSPF Process 1\nTotal number of peer(s): 0\n')
        files[f'{router}-routes.txt'] = HEADER + f'10.12.0.0/24 Direct 0 0 D {local} GE0/0/0\n'
        if healthy:
            files[f'{router}-routes.txt'] += f'{prefix} OSPF 10 2 D {remote} GE0/0/0\n'
    for pc, target in (('PC1', '192.168.20.10'), ('PC2', '192.168.10.10')):
        received = 5 if healthy else 0
        files[f'{pc}-ping.txt'] = f'PING {target}:\n5 packet(s) transmitted\n{received} packet(s) received\n'
    return case, files


class BEvidenceTests(unittest.TestCase):
    def test_area_fault_and_recovery_evidence(self):
        before, before_files = fixture(False)
        after, after_files = fixture(True)
        failed = check_stage(before, read_json(LAB / 'expected.json'), before_files, healthy=False)
        recovered = check_stage(after, NORMAL, after_files, healthy=True)
        self.assertEqual(failed['issues'], [])
        self.assertEqual(failed['diagnosis_status'], 'fault_found')
        self.assertEqual(recovered['issues'], [])
        self.assertEqual(recovered['diagnosis_status'], 'verified_from_observations')
        self.assertNotEqual(failed['config_fingerprint'], recovered['config_fingerprint'])

    def test_static_route_cannot_substitute_for_ospf(self):
        case, files = fixture(True)
        files['R2-routes.txt'] = files['R2-routes.txt'].replace(' OSPF ', ' Static ')
        result = check_stage(case, NORMAL, files, healthy=True)
        self.assertTrue(any('R2 未选中预期的 OSPF 路由' in item for item in result['issues']))

    def test_wrong_reverse_target_and_neighbor_are_detected(self):
        case, files = fixture(True)
        files['PC2-ping.txt'] = files['PC2-ping.txt'].replace('192.168.10.10', '10.12.0.1')
        files['R2-ospf.txt'] = files['R2-ospf.txt'].replace('Address: 10.12.0.1', 'Address: 10.12.0.9')
        result = check_stage(case, NORMAL, files, healthy=True)
        self.assertTrue(any('PC2 缺少' in item for item in result['issues']))
        self.assertTrue(any('各一条' in item for item in result['issues']))

    def test_unknown_neighbor_text_is_not_proof_of_failure(self):
        case, files = fixture(False)
        files['R2-ospf.txt'] = 'OSPF Process 1\n--- more ---\n'
        result = check_stage(case, read_json(LAB / 'expected.json'), files, healthy=False)
        self.assertTrue(any('R2 邻居输出' in item for item in result['issues']))

    def test_time_requires_zone(self):
        self.assertIsNone(capture_time('2026-11-12T09:30:00'))
        self.assertIsNotNone(capture_time('2026-11-12T09:30:00+08:00'))
