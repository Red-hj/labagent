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
from labagent.experiment_goals import check_route_selection, evidence_blocks
from labagent.investigation import safe_text, source_bundle
from labagent.operation_history import prompt_info
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
TERMINAL_COMMANDS = {'display current-configuration': 'config', 'display ospf peer': 'ospf',
                     'display ip routing-table': 'routes'}
REPAIR_QUESTIONS = {
    'before': '请按实验要求核对故障阶段，指出是哪台设备的接口区域不符，并引用配置、邻居、路由表或 Ping 原文。',
    'after': '这是同一实验修改后新采集的材料。请按实验要求复测，分别核对双端邻居、路由来源和双向 Ping。',
}
LAB02_INSTRUCTIONS = ('拓扑：PC1 — R1 — R2 — PC2。R1 和 R2 通过 GE0/0/0 互联。\n'
                      'R1、R2 的 GE0/0/0 互联接口统一使用 Area 0。\n'
                      'PC1 必须 Ping 通 PC2 的 192.168.20.10；PC2 必须 Ping 通 PC1 的 192.168.10.10。\n'
                      'R1 到 192.168.20.0/24 的路由必须来自 OSPF；R2 到 192.168.10.0/24 的路由必须来自 OSPF。\n'
                      '检查两端的互联 OSPF 邻居是否为 Full。\n')


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


def check_stage(template, expected, files, *, healthy, require_route_evidence=False):
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
    if require_route_evidence:
        # Use the open-diagnosis verifier for actual captures: only installed entries count,
        # and a missing route counts only if the public table is demonstrably complete.
        selected = {}
        for router, (destination, prefix) in ROUTE_TARGETS.items():
            try:
                materials = {f'{router}-routes.log': observation_material(router, 'display ip routing-table', files[f'{router}-routes.txt'])}
            except ValueError as error:
                errors.append(str(error))
                continue
            blocks = evidence_blocks(source_bundle({'configs': materials})[0])
            lookup = check_route_selection(router, destination, blocks)
            if lookup['status'] == 'no_match':
                selected[router] = None
            elif lookup['status'] == 'observed':
                rows = lookup['routes']
                selected[router] = rows[0]
                if healthy and any(row['network'] != prefix or row['protocol'].lower() != 'ospf' for row in rows):
                    errors.append(f'{router} 的等价选路中有不符合实验要求的路由。')
            else:
                errors.append(f'{router} 路由表未证明当前选路，请保留命令、Public 表标记和完整计数。')
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


def observation_material(device, command, raw):
    # Preserve a real prompt (including a filtered command) and supply one for standalone bodies.
    text = safe_text(raw).rstrip()
    prompts = [info for line in text.splitlines() if (info := prompt_info(line)) and info[4] == 'vrp' and info[3].strip()]
    if any(info[0].casefold() != device.casefold() for info in prompts):
        raise ValueError(f'{device} 的 {command} 文件包含其他设备提示符，请核对来源。')
    pc_prompt = None
    if device in TARGETS and text and (first := prompt_info(text.splitlines()[0])) and first[4] == 'ios':
        if first[0].casefold() not in {device.casefold(), 'pc'}:
            raise ValueError(f'{device} 的 Ping 文件包含其他设备提示符，请核对来源。')
        pc_prompt = first
    if pc_prompt and pc_prompt[0].casefold() == 'pc':
        text = f'<{device}>{command}\n' + '\n'.join(text.splitlines()[1:])
    elif not prompts and not pc_prompt and not any(line.lstrip().startswith(f'<{device}>') for line in text.splitlines()):
        text = f'<{device}>{command}\n{text}'
    return text + '\n'


def split_terminal(device, transcript):
    """Extract named command blocks from one device's saved terminal text."""
    required = (TERMINAL_COMMANDS if device in ROUTERS else {f'ping {TARGETS[device]}': 'ping'})
    blocks = {}
    active, lines = None, []

    def save():
        if active is None:
            return
        if active in blocks:
            raise ValueError(f'{device} 的 {active} 有多次输出；请保留本阶段对应的一次。')
        if not any(line.strip() for line in lines[1:]):
            raise ValueError(f'{device} 的 {active} 缺少命令输出。')
        blocks[active] = '\n'.join(lines).strip('\n') + '\n'

    for line in transcript.splitlines():
        info = prompt_info(line)
        if info:
            name, _, _, command, style = info
            # Lab PC consoles may use the generic prompt PC> in each device's own log.
            valid = name.casefold() == device.casefold() or device in TARGETS and style == 'ios' and name.casefold() == 'pc'
            command = ' '.join(command.lower().split())
            if not valid and not command:
                info = None  # A bracketed output label is not a change of device.
        if not info:
            if active is not None:
                lines.append(line)
            continue
        save()
        if not valid:
            raise ValueError(f'{device} 的终端原文包含其他设备命令，请按设备分别保存。')
        active = required.get(command)
        lines = [line]
    save()
    missing = [command for command, kind in required.items() if kind not in blocks]
    if missing:
        raise ValueError(f'{device} 缺少：' + '、'.join(missing) + '。请保留命令所在行和完整输出。')
    return {f'{device}-{kind}.txt': text for kind, text in blocks.items()}


def import_terminals(folder, phase, terminal_folder):
    """Import one phase from four terminal transcripts without altering the originals."""
    if phase not in STAGES:
        raise ValueError('请选择 before 或 after。')
    meta = read_json(folder / 'record.json')
    if meta.get('case_id') != 'lab_02':
        raise ValueError('整段终端记录导入当前用于 lab_02。')
    destination = folder / phase
    files = {}
    for device in (*ROUTERS, *TARGETS):
        source = terminal_folder / f'{device}.txt'
        if not source.is_file():
            raise ValueError(f'缺少 {source.name}；请分别保存 R1、R2、PC1、PC2 的原始终端记录。')
        files.update(split_terminal(device, source.read_text(encoding='utf-8-sig')))
    if any((destination / name).exists() for name in files):
        raise ValueError(f'{phase} 已有采集文件；请使用新的采集目录，原有文件不会覆盖。')
    if terminal_folder == destination:
        raise ValueError('原始终端记录与拆分输出须放在不同目录。')
    destination.mkdir(exist_ok=True)
    for name, text in files.items():
        with (destination / name).open('x', encoding='utf-8') as output:
            output.write(text)
    return len(files)


def check_capture(folder, *, snapshots=None):
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
                template, normal_expected if healthy else fault_expected, files, healthy=healthy,
                require_route_evidence=report['source'] in REAL_SOURCES)}
            if snapshots is not None:
                snapshots[phase] = files
    if all(times) and times[0] >= times[1]:
        report['issues'].append('after 的采集时间必须晚于 before。')
    if not report['issues']:
        bad = any(stage['issues'] for stage in report['stages'].values())
        report['status'] = 'mismatch' if bad else 'fixture_pass' if report['source'] == 'fixture' else 'recorded_observations_match'
    return report


def export_capture(folder, output):
    """Create two upload-ready material sets after rechecking the original capture."""
    snapshots = {}
    report = check_capture(folder, snapshots=snapshots)
    if report['case_id'] != 'lab_02' or report['status'] != 'recorded_observations_match':
        raise ValueError('仅导出通过核验的 lab_02 华为设备或仿真实采。夹具与待采集目录不能导出。')
    # Do not place expected.json, answers, or raw originals in the website bundle.
    phases = {}
    for phase in STAGES:
        materials = {'实验说明.txt': LAB02_INSTRUCTIONS}
        for router in ROUTERS:
            files = {part: snapshots[phase][f'{router}-{part}.txt']
                     for part in ('config', 'ospf', 'routes')}
            materials[f'{router}.cfg'] = observation_material(router, 'display current-configuration', files['config'])
            materials[f'{router}-ospf.log'] = observation_material(router, 'display ospf peer', files['ospf'])
            materials[f'{router}-routes.log'] = observation_material(router, 'display ip routing-table', files['routes'])
        for device, target in TARGETS.items():
            raw = snapshots[phase][f'{device}-ping.txt']
            materials[f'{device}-ping.log'] = observation_material(device, f'ping {target}', raw)
        phases[phase] = materials
    if output.is_relative_to(folder):
        raise ValueError('导出目录须与原始采集目录分开。')
    output.mkdir(parents=True, exist_ok=False)
    for phase, materials in phases.items():
        destination = output / phase
        destination.mkdir()
        for name, content in materials.items():
            (destination / name).write_text(content, encoding='utf-8')
    (output / '上传说明.txt').write_text(
        '确认材料允许共享，并人工检查其中的账号、地址和设备信息后再上传。\n'
        f'新建同一实验会话，上传 before/ 内全部材料，提问：{REPAIR_QUESTIONS["before"]}\n'
        f'在同一会话补充 after/ 同名材料，核对这一步，提问：{REPAIR_QUESTIONS["after"]}\n', encoding='utf-8')


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
    ingest = commands.add_parser('import-terminals', help='将某阶段的 4 份完整终端文本拆成待核验材料')
    ingest.add_argument('directory', type=Path)
    ingest.add_argument('phase', choices=STAGES)
    ingest.add_argument('terminals', type=Path, help='含 R1.txt、R2.txt、PC1.txt、PC2.txt 的本地目录')
    export = commands.add_parser('export', help='核验 lab_02 实采并导出首页材料')
    export.add_argument('directory', type=Path)
    export.add_argument('output', type=Path)
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
    if args.task == 'export':
        try:
            output = local_capture(args.output)
            export_capture(folder, output)
        except (ValueError, OSError) as error:
            cli.error(str(error))
        print(f'首页材料：{output}')
        return 0
    if args.task == 'import-terminals':
        try:
            count = import_terminals(folder, args.phase, args.terminals.resolve())
        except (ValueError, OSError) as error:
            cli.error(str(error))
        print(f'{args.phase}：已拆分 {count} 份材料。请填写实际采集时间和来源，再运行 check。')
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
