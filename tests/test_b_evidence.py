"""Synthetic fixtures for the evidence checker; these are not experiment captures."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.verify_b_evidence import (ROOT, TERMINAL_COMMANDS, capture_time, check_capture, check_stage,
                                       export_capture, import_terminals, read_json, split_terminal)
from labagent.experiment_goals import check_route_selection, evidence_blocks
from labagent.investigation import source_bundle


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
    def test_terminal_router_requires_full_commands_and_rejects_duplicates_or_wrong_device(self):
        good = '<R1>display current-configuration\n#\nsysname R1\nreturn\n<R1>display ospf peer\nTotal number of peer(s): 0\n<R1>display ip routing-table\nRouting Table : Public\n<R1>\n'
        self.assertEqual(set(split_terminal('R1', good)), {'R1-config.txt', 'R1-ospf.txt', 'R1-routes.txt'})
        for raw, problem in ((good.replace('routing-table', 'routing-table protocol ospf'), '缺少'),
                             (good.replace('<R1>display ospf', '<R2>display ospf'), '其他设备'),
                             (good + '<R1>display ospf peer\nTotal number of peer(s): 0\n', '多次输出')):
            with self.subTest(problem=problem), self.assertRaisesRegex(ValueError, problem):
                split_terminal('R1', raw)

    def test_real_source_mode_requires_proof_for_missing_route(self):
        case, files = fixture(False)
        expected = read_json(LAB / 'expected.json')
        result = check_stage(case, expected, files, healthy=False, require_route_evidence=True)
        self.assertTrue(any('未证明当前选路' in issue for issue in result['issues']))
        for router in ('R1', 'R2'):
            files[f'{router}-routes.txt'] = 'Routing Table : Public\nDestinations : 1 Routes : 1\n' + files[f'{router}-routes.txt']
        checked = check_stage(case, expected, files, healthy=False, require_route_evidence=True)
        self.assertEqual(checked['issues'], [])
        files['R1-routes.txt'] = '<R2>display ip routing-table\n' + files['R1-routes.txt']
        scoped = check_stage(case, expected, files, healthy=False, require_route_evidence=True)
        self.assertTrue(any('其他设备提示符' in issue for issue in scoped['issues']))

    def test_exports_only_checked_capture_and_keeps_phase_materials_separate(self):
        # Use an ephemeral simulation of the real-source file contract, never a shared device record.
        base = ROOT / 'runtime/tests'
        base.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=base) as directory:
            root = Path(directory)
            capture, output = root / 'capture', root / 'upload'
            capture.mkdir()
            meta = {'case_id': 'lab_02', 'source': 'fixture', 'environment': 'local synthetic test', 'stages': {}}
            for phase, healthy, hour in (('before', False, '09'), ('after', True, '10')):
                (capture / phase).mkdir()
                meta['stages'][phase] = {'captured_at': f'2026-11-19T{hour}:00:00+08:00', 'complete_route_tables': True}
                _, files = fixture(healthy)
                for router in ('R1', 'R2'):
                    count = 2 if healthy else 1
                    files[f'{router}-routes.txt'] = f'Routing Table : Public\nDestinations : {count} Routes : {count}\n' + files[f'{router}-routes.txt']
                terminals = root / (phase + '-terminals')
                terminals.mkdir()
                files['R1-config.txt'] += '# sample-password remove-this-value\n'
                for router in ('R1', 'R2'):
                    (terminals / f'{router}.txt').write_text(''.join(
                        f'<{router}>{command}\n' + files[f'{router}-{kind}.txt']
                        for command, kind in TERMINAL_COMMANDS.items()), encoding='utf-8')
                for pc, target in (('PC1', '192.168.20.10'), ('PC2', '192.168.10.10')):
                    (terminals / f'{pc}.txt').write_text(f'PC> ping {target}\n' + files[f'{pc}-ping.txt'], encoding='utf-8')
            record = capture / 'record.json'
            record.write_text(json.dumps(meta), encoding='utf-8')
            for phase in ('before', 'after'):
                terminals = root / (phase + '-terminals')
                self.assertEqual(import_terminals(capture, phase, terminals), 8)
                with self.assertRaisesRegex(ValueError, '已有采集文件'):
                    import_terminals(capture, phase, terminals)
                self.assertIn('PC> ping', (terminals / 'PC1.txt').read_text(encoding='utf-8'))
            self.assertEqual(check_capture(capture)['status'], 'fixture_pass')
            with self.assertRaisesRegex(ValueError, '仅导出'):
                export_capture(capture, output)
            self.assertFalse(output.exists())
            meta['source'] = 'vrp_simulator'
            record.write_text(json.dumps(meta), encoding='utf-8')
            export_capture(capture, output)
            self.assertEqual({p.name for p in (output / 'before').iterdir()}, {p.name for p in (output / 'after').iterdir()})
            self.assertEqual(len(list((output / 'after').iterdir())), 9)
            self.assertNotIn('remove-this-value', (output / 'after/R1.cfg').read_text(encoding='utf-8'))
            for phase, status in (('before', 'no_match'), ('after', 'observed')):
                config = {p.name: p.read_text(encoding='utf-8') for p in (output / phase).iterdir()}
                self.assertTrue(config['PC1-ping.log'].startswith('<PC1>ping 192.168.20.10\n'))
                self.assertEqual(check_route_selection('R1', '192.168.20.10', evidence_blocks(source_bundle({'configs': config})[0]))['status'], status)
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
