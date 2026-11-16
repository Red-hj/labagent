"""Check collected before/after evidence for the five two-router OSPF labs."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.engine import diagnose, fingerprint, validate
from labagent.runtime_parser import normalize_outputs, parse_neighbors, parse_ping, parse_routes
from scripts.evaluate_b import assess

CASES = tuple(f'lab_{i:02d}' for i in range(1, 6))
STAGES = ('before', 'after')
ROUTERS = ('R1', 'R2')
TARGETS = {'PC1': '192.168.20.10', 'PC2': '192.168.10.10'}
ROUTE_TARGETS = {'R1': (TARGETS['PC1'], '192.168.20.0/24'), 'R2': (TARGETS['PC2'], '192.168.10.0/24')}
RAW_FILES = tuple(f'{router}-{suffix}.txt' for router in ROUTERS for suffix in ('config', 'ospf', 'routes')) + tuple(
    f'{pc}-ping.txt' for pc in TARGETS
)
REAL_SOURCES = ('vrp_device', 'vrp_simulator')


def capture_time(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.utcoffset() is not None else None


def reports_zero_peers(text):
    """A blank/truncated dump is not evidence of zero OSPF peers."""
    return bool(re.search(r'(?im)^\s*total number of peer(?:\(s\)|s)?\s*:\s*0\s*$', text))


def check_stage(template, expected, files, *, healthy):
    payload = {**template, 'configs': {r: files[f'{r}-config.txt'] for r in ROUTERS}, 'use_llm': False}
    static = diagnose(payload, _explain=False)
    errors = []
    config_match = assess(expected, static)
    if not config_match['strict_match']:
        errors.append('实采配置的静态诊断与本阶段预期不一致。')

    commands = []
    for router in ROUTERS:
        neighbor_text = files[f'{router}-ospf.txt']
        route_text = files[f'{router}-routes.txt']
        if not (parse_neighbors(neighbor_text) or reports_zero_peers(neighbor_text)):
            errors.append(f'{router} 邻居输出缺少可识别的详细记录或明确的零邻居汇总。')
        try:
            routes = parse_routes(route_text)
        except ValueError:
            routes = None
        if not routes:
            errors.append(f'{router} 路由表缺少可解析的条目，请提交完整原文。')
        commands += [
            {'device': router, 'command': 'display ospf peer', 'text': neighbor_text},
            {'device': router, 'command': 'display ip routing-table', 'text': route_text, 'complete': True},
        ]

    pings = {}
    for source, target in TARGETS.items():
        text = files[f'{source}-ping.txt']
        try:
            ping = parse_ping(text)
        except ValueError:
            ping = None
        pings[source] = ping
        if not ping or ping['target'] != target or ping['sent'] <= 0:
            errors.append(f'{source} 缺少发往 {target} 的唯一可解析 Ping 记录。')
        elif (healthy and ping['received'] != ping['sent']) or (not healthy and ping['received'] != 0):
            errors.append(f'{source} → {target} 的收发数与本阶段预期不一致。')
        commands.append({'device': source, 'command': f'ping {target}', 'text': text})

    payload['routing_queries'] = [{'device': device, 'target': target} for device, (target, _) in ROUTE_TARGETS.items()]
    payload['topology'] = {**payload['topology'], 'ping_targets': [{'source': s, 'target': t} for s, t in TARGETS.items()]}
    digest = fingerprint(payload)
    payload['command_outputs'] = [{**item, 'config_fingerprint': digest} for item in commands]
    validate(payload)
    prepared, runtime = normalize_outputs(payload)
    result = diagnose(prepared, _runtime=runtime, _explain=False)
    neighbors = prepared['observations'].get('neighbors', [])
    mapped = [row for item in neighbors if item['link_id'] == 'r1-r2' for row in item['evidence']]
    full = {row['device'] for row in mapped if row['state'].lower() == 'full'}
    if healthy and (len(mapped) != 2 or full != set(ROUTERS)):
        errors.append('恢复阶段需要 R1、R2 各一条与互联链路匹配的 Full 邻居。')
    if not healthy and full:
        errors.append('故障阶段仍观察到预期链路的 Full 邻居。')
    if not healthy:
        for router in ROUTERS:
            if not any(row['device'] == router for row in mapped) and not reports_zero_peers(files[f'{router}-ospf.txt']):
                errors.append(f'{router} 未提交预期链路的邻居详情或明确的零邻居汇总。')

    selected = {row['device']: row['route'] for row in runtime['runtime_checks'] if row['check'] == 'route_lookup'}
    for router, (_, prefix) in ROUTE_TARGETS.items():
        route = selected.get(router)
        if router not in selected:
            errors.append(f'{router} 的远端路由未能核对。')
        elif healthy and (not route or route['network'] != prefix or route['protocol'].lower() != 'ospf'):
            errors.append(f'{router} 未选中预期的 OSPF 路由 {prefix}。')
        elif not healthy and route:
            errors.append(f'{router} 在故障阶段仍有目标路由；请检查是否有替代路径。')
    if healthy and result['status'] != 'verified_from_observations':
        errors.append('当前观测未达到 LabAgent 复测通过条件。')
    return {'config_fingerprint': digest, 'config_match': config_match, 'diagnosis_status': result['status'],
            'full_neighbor_sources': sorted(full), 'selected_routes': selected, 'pings': pings, 'issues': errors}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def check_capture(folder):
    meta = read_json(folder / 'record.json')
    case_id = meta.get('case_id')
    if case_id not in CASES:
        raise ValueError('record.json 的 case_id 应为 lab_01 至 lab_05')
    report = {'case_id': case_id, 'source': meta.get('source'), 'environment': meta.get('environment'),
              'stages': {}, 'status': 'pending', 'issues': []}
    if report['source'] not in (*REAL_SOURCES, 'fixture'):
        report['issues'].append('请填写采集来源：vrp_device、vrp_simulator 或 fixture。')
    if not isinstance(report['environment'], str) or not report['environment'].strip():
        report['issues'].append('请填写设备/仿真器型号与版本。')
    directory = ROOT / 'data/team_cases' / case_id
    fault_expected = read_json(directory / 'expected.json')
    normal_expected = {'status': 'awaiting_verification', 'findings': [], 'unknowns': []}
    times = []
    for phase in STAGES:
        stage_meta = meta.get('stages', {}).get(phase, {})
        stamp = capture_time(stage_meta.get('captured_at'))
        times.append(stamp)
        if stamp is None:
            report['issues'].append(f'{phase} 缺少带时区的采集时间。')
        if stage_meta.get('complete_route_tables') is not True:
            report['issues'].append(f'{phase} 尚未确认两份路由表均为完整输出。')
        files = {}
        for name in RAW_FILES:
            path = folder / phase / name
            if path.is_file():
                text = path.read_text(encoding='utf-8-sig')
                if text.strip():
                    files[name] = text
        missing = sorted(set(RAW_FILES) - set(files))
        if missing:
            report['issues'].extend(f'待采集：{phase}/{name}' for name in missing)
        elif stamp is not None and stage_meta.get('complete_route_tables') is True:
            template = read_json(directory / ('input.json' if phase == 'before' else 'repaired.json'))
            healthy = phase == 'after' or case_id == 'lab_01'
            report['stages'][phase] = {'captured_at': stamp.isoformat(), **check_stage(
                template, normal_expected if healthy else fault_expected, files, healthy=healthy)}
    if all(times) and times[0] >= times[1]:
        report['issues'].append('after 的采集时间必须晚于 before。')
    if not report['issues']:
        bad = any(stage['issues'] for stage in report['stages'].values())
        report['status'] = 'mismatch' if bad else 'fixture_pass' if report['source'] == 'fixture' else 'recorded_observations_match'
    return report


def local_capture(path):
    path = path.resolve()
    runtime = (ROOT / 'runtime').resolve()
    if path == runtime or not path.is_relative_to(runtime):
        raise ValueError('采集目录须放在项目 runtime/ 下，例如 runtime/b-evidence/lab_02')
    return path


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest='task', required=True)
    init = commands.add_parser('init', help='建立空的采集目录')
    init.add_argument('case', choices=CASES)
    init.add_argument('directory', type=Path)
    check = commands.add_parser('check', help='核验实际采集文件并输出 verification.json')
    check.add_argument('directory', type=Path)
    args = cli.parse_args()
    folder = local_capture(args.directory)
    if args.task == 'init':
        folder.mkdir(parents=True, exist_ok=False)
        for phase in STAGES:
            (folder / phase).mkdir()
        record = {'case_id': args.case, 'source': None, 'environment': '', 'stages': {
            phase: {'captured_at': None, 'complete_route_tables': False} for phase in STAGES}}
        (folder / 'record.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'采集目录：{folder}\nbefore 和 after 各保存：\n' + '\n'.join(RAW_FILES))
        return 0
    report = check_capture(folder)
    target = folder / 'verification.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{report["case_id"]}: {report["status"]} → {target}')
    for item in report['issues']:
        print(f'- {item}')
    for phase, stage in report['stages'].items():
        for item in stage['issues']:
            print(f'- {phase}：{item}')
    return {'pending': 2, 'mismatch': 1}.get(report['status'], 0)


if __name__ == '__main__':
    raise SystemExit(main())
