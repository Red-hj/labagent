"""Check raw captures from run_b_frr_lab.sh and write an uploadable run record."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.connectivity import parse_ping_records
from labagent.repair_lab import RUN_KIND, compare_run, evaluate_phase, interface_admin_up, route_to


ROUTERS = ('R1', 'R2')
PEERS = {
    'R1': ('2.2.2.2', '10.12.0.2', '192.168.20.0/24'),
    'R2': ('1.1.1.1', '10.12.0.1', '192.168.10.0/24'),
}
PINGS = (
    ('PC1', 'PC1-ping', '192.168.20.10', 'PC1 → PC2 端到端 Ping'),
    ('PC2', 'PC2-ping', '192.168.10.10', 'PC2 → PC1 端到端 Ping'),
    ('R1', 'R1-direct-ping', '10.12.0.2', 'R1 → R2 直连接口 Ping（对照）'),
)
ENABLE_TRANSIT = 'ip link set dev transit up'
RESTORE_AREA = ['interface transit', 'no ip ospf area 0.0.0.1', 'ip ospf area 0.0.0.0']
CASES = {
    'lab_02': ('OSPF 区域错误', 'R2 互联接口使用 Area 1', '恢复 R2 互联接口至 Area 0',
               RESTORE_AREA),
    'lab_03': ('OSPF 互联接口关闭', 'R2 互联接口被管理关闭', '重新启用 R2 互联接口',
               [ENABLE_TRANSIT]),
    'lab_02_backup': ('OSPF 区域错误与备用静态路由', 'R2 互联接口使用 Area 1，路由切到备用静态', '恢复 R2 互联接口至 Area 0',
                      RESTORE_AREA),
    'lab_02_partial': ('接口关闭与 OSPF 区域错误', 'R2 互联接口关闭且使用 Area 1', '启用接口并恢复 R2 互联接口至 Area 0',
                       [ENABLE_TRANSIT, *RESTORE_AREA]),
}


def text(folder, name):
    value = (folder / name).read_text(encoding='utf-8').strip()
    if not value:
        raise ValueError(f'{folder.name}/{name} 为空')
    return value + '\n'


def stamp(folder, name):
    value = datetime.fromisoformat(text(folder, name).strip())
    if value.utcoffset() is None:
        raise ValueError(f'{name} 缺少时区')
    return value


def experiment_goals(case_id):
    goals = [{'id': key, 'device': device, 'target': target, 'title': title, 'kind': 'ping', 'count': 5}
             for device, key, target, title in PINGS]
    for router, (peer_id, peer_ip, prefix) in PEERS.items():
        goals.extend([
            {'id': f'route_{router}', 'kind': 'route_protocol', 'device': router, 'target': prefix,
             'expected': 'ospf', 'title': f'{router} 学到远端 OSPF 路由 {prefix}'},
            {'id': f'neighbor_{router}', 'kind': 'ospf_neighbor', 'device': router, 'target': peer_id,
             'peer_address': peer_ip, 'title': f'{router} 观察到 {peer_id} 的 Full 邻接'},
        ])
    if case_id in {'lab_03', 'lab_02_partial'}:
        goals.append({'id': 'R2_transit_admin', 'kind': 'interface_admin_up', 'device': 'R2', 'target': 'transit',
                      'title': 'R2 互联接口管理状态为 UP'})
    return goals


def phase(folder, case_id):
    observations = {router: {} for router in ROUTERS}
    fault = folder.name == 'fault'
    partial = folder.name == 'partial'
    backup = case_id == 'lab_02_backup'
    area_fault = partial or (fault and case_id in {'lab_02', 'lab_02_backup', 'lab_02_partial'})
    for router in ROUTERS:
        config = text(folder, f'{router}-config.txt')
        transit = re.search(r'^interface transit\n((?: +[^\n]+\n)+)', config, re.M)
        area = '0.0.0.1' if area_fault and router == 'R2' else '0.0.0.0'
        if not transit or f'ip ospf area {area}' not in {line.strip() for line in transit[1].splitlines()}:
            raise ValueError(f'{folder.name}/{router} 的互联接口区域与预期不符')
        interface_output = text(folder, f'{router}-transit-link.txt').splitlines()[0]
        expected_up = not (fault and case_id in {'lab_03', 'lab_02_partial'} and router == 'R2')
        if interface_admin_up(interface_output, 'transit') != expected_up:
            raise ValueError(f'{folder.name}/{router} 的接口管理状态与预期不符')
        neighbors = json.loads(text(folder, f'{router}-neighbors.json'))
        routes = json.loads(text(folder, f'{router}-routes.json'))
        if not isinstance(neighbors.get('neighbors'), dict) or not routes:
            raise ValueError(f'{folder.name}/{router} 缺少可核对的邻居或路由表')
        if backup:
            _, _, target = PEERS[router]
            expected = 'static' if fault else 'ospf'
            installed = {row.get('protocol') for row in route_to(routes, target) if row.get('selected') and row.get('installed')}
            if installed != {expected} or (fault and neighbors['neighbors']):
                raise ValueError(f'{folder.name}/{router} 未观察到预期的路由来源或邻居状态')
        config = re.sub(r'^(?:hostname (?!R[12]$)|domainname ).*$', '! host identity omitted', config, flags=re.M)
        observations[router] = {
            'config': config,
            'interfaces': {'transit': interface_output},
            'neighbors': neighbors,
            'routes': {prefix: [{key: row[key] for key in ('protocol', 'selected', 'installed') if key in row}
                                for row in rows] for prefix, rows in routes.items()},
            'route_output': text(folder, f'{router}-all-routes.txt' if backup else f'{router}-ospf-routes.txt'),
            'kernel_route_output': (folder / f'{router}-kernel-routes.txt').read_text(encoding='utf-8'),
        }
    for device, key, target, _ in PINGS:
        output = text(folder, f'{key}.txt')
        matches = [row for row in parse_ping_records(output.splitlines()) if row['target'] == target]
        direct_available = (case_id == 'lab_02' or partial) and key == 'R1-direct-ping'
        should_pass = not (fault or partial) or backup or direct_available
        exit_code = int(text(folder, f'{key}.txt.exit'))
        if len(matches) != 1 or (should_pass and (matches[0]['sent'], matches[0]['received'], exit_code) != (5, 5, 0)) or (
            not should_pass and (matches[0]['received'] != 0 or exit_code == 0)
        ):
            raise ValueError(f'{folder.name}/{key} 的收发统计与实验预期不符')
        observations.setdefault(device, {'config': ''}).setdefault('pings', {})[key] = output
    return {'captured_at': stamp(folder, 'captured-at.txt').isoformat(), 'observations': observations}


def export_capture(folder):
    case_id = text(folder, 'case.txt').strip()
    if case_id not in CASES:
        raise ValueError('采集用例应为 ' + '、'.join(CASES))
    title, fault_title, repair_title, commands = CASES[case_id]
    started = stamp(folder, 'started-at.txt')
    completed = stamp(folder, 'completed-at.txt')
    stages = ['baseline', 'fault']
    if case_id == 'lab_02_partial':
        stages.append('partial')
    stages.append('repaired')
    captures = {name: phase(folder / name, case_id) for name in stages}
    times = [datetime.fromisoformat(item['captured_at']) for item in captures.values()]
    if not started <= times[0] or not times[-1] <= completed or any(left >= right for left, right in zip(times, times[1:])):
        raise ValueError('采集时间顺序有误')
    healthy, fault, repaired = (captures[name] for name in ('baseline', 'fault', 'repaired'))
    goals = experiment_goals(case_id)
    if not all(check['passed'] for check in evaluate_phase(goals, healthy)):
        raise ValueError('故障注入前的正常基线未达到实验目标')
    version = text(folder, 'frr-version.txt').splitlines()[0].removeprefix('ospfd version ')
    run = {
        'kind': RUN_KIND, 'schema_version': 1, 'run_id': f'{case_id}-frr-{started:%Y%m%dT%H%M%S}',
        'title': f'B 组实测：{title}与修复', 'started_at': started.isoformat(),
        'environment': {'engine': f'FRRouting {version}', 'platform': text(folder, 'platform.txt').strip(),
                        'source': 'B 组隔离 Linux 网络命名空间实测', 'healthy_baseline_checked_at': healthy['captured_at']},
        'goals': goals,
        'phases': [{**fault, 'id': 'baseline', 'title': f'故障：{fault_title}', 'commands': {}},
                   {**repaired, 'id': 'restore', 'title': repair_title, 'commands': {'R2': commands}}],
    }
    fault_count = {'lab_02': 1, 'lab_03': 0, 'lab_02_backup': 3, 'lab_02_partial': 0}[case_id]
    expected_steps = [fault_count, len(goals)]
    if case_id == 'lab_02_partial':
        run['phases'].insert(1, {**captures['partial'], 'id': 'partial', 'title': '启用 R2 互联接口，保留 Area 1',
                                 'commands': {'R2': [ENABLE_TRANSIT]}})
        expected_steps.insert(1, 2)
    comparison = compare_run(run)
    if [item['passed_count'] for item in comparison['phases']] != expected_steps:
        raise ValueError('故障或修复阶段未得到预期的邻居、路由和 Ping 对照结果')
    return run, comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        run, comparison = export_capture(args.capture)
        with args.output.open('x', encoding='utf-8') as file:
            json.dump(run, file, ensure_ascii=False, indent=2)
            file.write('\n')
    except (KeyError, OSError, ValueError) as error:
        parser.error(str(error))
    print(' → '.join(f"{row['passed_count']}/{row['total']}" for row in comparison['phases']))


if __name__ == '__main__':
    main()
