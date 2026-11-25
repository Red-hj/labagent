"""Build an original, configuration-grounded OSPF diagnosis SFT seed."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.engine import diagnose, fingerprint
from scripts.evaluate_b import assess, digest, read, write


VERSION = 'ospf-diagnosis-sft-seed-1'
TOPOLOGIES = {
    'train': {
        'chain': ((1, 2), (2, 3), (3, 4), (4, 5), (5, 6)),
        'branch': ((1, 2), (2, 3), (2, 4), (4, 5), (5, 6)),
        'ring': ((1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 1)),
    },
    'validation': {'ladder': ((1, 2), (2, 3), (4, 5), (5, 6), (1, 4), (2, 5), (3, 6))},
}
SCENARIOS = ('normal', 'valid_multiarea', 'area_mismatch', 'area_policy', 'shutdown', 'network',
             'subnet', 'missing_device', 'missing_interface', 'missing_ip')
QUESTIONS = ('根据拓扑和配置核对实验，指出问题、原文证据和下一步。',
             '请检查每条互联链路的 OSPF 条件，定位异常或指出缺少的材料。',
             '给出配置诊断、对应链路和可执行的复测建议。')
SYSTEM_PROMPT = (
    '你负责分析隔离的 OSPF 实验配置。只根据本题材料回答。返回 JSON 对象，包含 case_id、status、findings、'
    'unknowns、evidence、next_steps。status 取 fault_found、needs_information 或 awaiting_verification。'
    'findings 和 unknowns 每项含 code、link_id、devices、message。判断区域冲突时比较同一链路两端，'
    '实验要求的区域单独核对。evidence 每项提供 device、line 与配置原文 text，行号从 1 开始。'
    '缺失材料时说明需要补交的内容，配置通过时仍需复测。禁止虚构运行结果。'
)
RETEST = '提交互联两端的 OSPF 邻居、去回程远端路由和双向 Ping 原始输出。'
MASK = '255.255.255.252'


def baseline(edges, address_block, multi_area):
    links, ports, address_plan = [], defaultdict(list), {}
    for number, pair in enumerate(edges, 1):
        endpoints, plan = [], {}
        area = '1' if multi_area and number == 1 else '0'
        link_id = f'link-{number:02d}'
        for host, device_number in enumerate(pair, 1):
            device = f'R{device_number}'
            ifname = f'GE0/0/{len(ports[device])}'
            address = f'10.{address_block}.{number}.{host}'
            ports[device].append({'interface': ifname, 'address': address, 'network': address, 'area': area,
                                  'link': link_id, 'shutdown': False, 'missing': False})
            endpoints.append({'device': device, 'interface': ifname})
            plan[device] = f'{address}/30'
        links.append({'id': link_id, 'endpoints': endpoints, 'expected_area': area, 'ospf': True})
        address_plan[link_id] = plan
    return links, dict(ports), address_plan


def render(device, ports):
    lines = [f'sysname {device}', '#']
    advertisements = defaultdict(list)
    for port in ports:
        if not port['missing']:
            slot = port['interface'].rsplit('/', 1)[-1]
            lines.append(f'interface GigabitEthernet0/0/{slot}')
            if port['address']:
                lines.append(f" ip address {port['address']} {MASK}")
            lines.extend((' shutdown' if port['shutdown'] else ' undo shutdown', '#'))
        advertisements[port['area']].append(port['network'])
    node = int(device[1:])
    lines.append(f'ospf {node + 10} router-id 10.255.0.{node}')
    for area, addresses in sorted(advertisements.items()):
        lines.append(f' area {area}')
        lines.extend(f'  network {address} 0.0.0.0' for address in addresses)
    return '\n'.join([*lines, '#', 'return', ''])


def expected_row(code, link_id, *devices):
    return {'code': code, 'link_id': link_id, 'devices': list(devices)}


def inject(scenario, ports, links, index):
    link = links[index % len(links)]
    link_id, area = link['id'], link['expected_area']
    first, second = (endpoint['device'] for endpoint in link['endpoints'])
    left, right = (next(port for port in ports[device] if port['link'] == link_id) for device in (first, second))
    findings, unknowns, actions, supporting_lines = [], [], [], []
    position = f"{second} 的 {right['interface']}"
    if scenario == 'area_mismatch':
        right['area'] = '3'
        findings = [expected_row('AREA_MISMATCH', link_id, first, second),
                    expected_row('AREA_POLICY_VIOLATION', link_id, second)]
        actions = [f"将 {position} 对应的 network 宣告从区域 3 移到实验要求的区域 {area}。"]
    elif scenario == 'area_policy':
        left['area'] = right['area'] = '3'
        findings = [expected_row('AREA_POLICY_VIOLATION', link_id, device) for device in (first, second)]
        actions = [f"两端当前区域一致。按实验要求把 {link_id} 两端的 network 宣告移到区域 {area}。"]
    elif scenario == 'shutdown':
        right['shutdown'] = True
        findings = [expected_row('INTERFACE_SHUTDOWN', link_id, second)]
        actions = [f"在 {position} 执行 undo shutdown，检查接口运行状态。"]
    elif scenario == 'network':
        right['network'] = right['address'].rsplit('.', 1)[0] + '.101'
        findings = [expected_row('OSPF_NOT_ENABLED', link_id, second)]
        supporting_lines = [(second, f"  network {right['network']} 0.0.0.0")]
        actions = [f"修正 {second} 区域 {area} 的互联 network 宣告，使其准确匹配 {right['address']}。"]
    elif scenario == 'subnet':
        original = right['address']
        prefix, block, third, host = original.split('.')
        right['address'] = right['network'] = f'{prefix}.{block}.{100 + int(third)}.{host}'
        findings = [expected_row('SUBNET_MISMATCH', link_id, first, second)]
        actions = [f"按地址表把 {position} 恢复为 {original}/30，并同步更新 OSPF 的主机地址匹配。"]
    elif scenario == 'missing_device':
        del ports[second]
        unknowns = [expected_row('MISSING_DEVICE', row['id'], second) for row in links
                    if any(endpoint['device'] == second for endpoint in row['endpoints'])]
        actions = [f'补交 {second} 的完整配置，包括互连接口和 OSPF 进程。']
    elif scenario == 'missing_interface':
        right['missing'] = True
        unknowns = [expected_row('MISSING_INTERFACE', link_id, second)]
        actions = [f'补交 {position} 的配置段，确认拓扑标注的接口名称。']
    elif scenario == 'missing_ip':
        right['address'] = None
        unknowns = [expected_row('MISSING_IP', link_id, second)]
        supporting_lines = [(second, f"interface GigabitEthernet0/0/{right['interface'].rsplit('/', 1)[-1]}")]
        actions = [f'补交 {position} 的主 IPv4 地址与掩码或接口原始输出。']
    status = 'fault_found' if findings else 'needs_information' if unknowns else 'awaiting_verification'
    return ({'status': status, 'findings': findings, 'unknowns': unknowns},
            actions + ([] if unknowns else [RETEST]), supporting_lines)


def topology_signature(payload):
    degrees = Counter(endpoint['device'] for link in payload['topology']['links'] for endpoint in link['endpoints'])
    return tuple(sorted(degrees.values()))


def target_evidence(payload, result, supporting_lines):
    relevant = result['findings'] + result['unknowns']
    if not relevant:
        relevant = [next(row for row in result['checks'] if row['check'] == check)
                    for check in ('interface_subnet', 'ospf_area')]
    evidence = {}
    for row in relevant:
        for item in row['evidence']:
            device, line, text = (item[key] for key in ('device', 'line', 'text'))
            lines = payload['configs'][device].splitlines()
            if not 1 <= line <= len(lines) or text.strip() != lines[line - 1].strip():
                raise ValueError('证据行与本条输入不一致')
            evidence[device, line] = {'device': device, 'line': line, 'text': lines[line - 1]}
    for device, text in supporting_lines:
        matching_lines = [number for number, line in enumerate(payload['configs'][device].splitlines(), 1) if line == text]
        if len(matching_lines) != 1:
            raise ValueError('补充证据必须唯一出现在配置中')
        number, = matching_lines
        evidence[device, number] = {'device': device, 'line': number, 'text': text}
    return list(evidence.values())


def label(payload, clean, expected, actions, supporting_lines):
    result = diagnose(payload, _explain=False)
    if not assess(expected, result)['strict_match']:
        raise ValueError(f"生成标注和规则检查不一致：{payload['case_id']}")
    if diagnose(clean, _explain=False)['status'] != 'awaiting_verification':
        raise ValueError(f"恢复基线未通过静态检查：{payload['case_id']}")
    return {'case_id': payload['case_id'], 'status': expected['status'],
            **{field: [{key: item[key] for key in ('code', 'link_id', 'devices', 'message')} for item in result[field]]
               for field in ('findings', 'unknowns')},
            'evidence': target_evidence(payload, result, supporting_lines), 'next_steps': actions}


def build_dataset():
    rng = random.Random(618)
    identifiers = iter(rng.sample(range(1000, 9999), len(SCENARIOS) * sum(map(len, TOPOLOGIES.values()))))
    batches, metadata, fingerprints, splits = {}, [], set(), defaultdict(set)
    block = 121
    for split, topologies in TOPOLOGIES.items():
        batches[split] = []
        for topology, edges in topologies.items():
            for index, scenario in enumerate(SCENARIOS):
                case_id = f'seed-{next(identifiers)}'
                links, ports, plan = baseline(edges, block, scenario == 'valid_multiarea')
                block += 1
                common = {'case_id': case_id, 'question': rng.choice(QUESTIONS), 'topology': {'links': links},
                          'requirements': {'address_plan': plan}}
                clean = {**common, 'configs': {device: render(device, values) for device, values in ports.items()}}
                faulty_ports = deepcopy(ports)
                expected, actions, supporting_lines = inject(scenario, faulty_ports, links, index + block)
                sample = {**common, 'configs': {device: render(device, values) for device, values in faulty_ports.items()}}
                response = label(sample, clean, expected, actions, supporting_lines)
                digest = fingerprint(sample)
                if digest in fingerprints:
                    raise ValueError(f'发现重复配置和拓扑：{case_id}')
                fingerprints.add(digest)
                splits[split].add(topology_signature(sample))
                metadata.append({'case_id': case_id, 'split': split, 'topology': topology, 'scenario': scenario,
                                 'status': response['status'], 'source': 'labagent_synthetic_vrp'})
                batches[split].append({'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user', 'content': json.dumps(sample, ensure_ascii=False)},
                    {'role': 'assistant', 'content': json.dumps(response, ensure_ascii=False)}]})
        rng.shuffle(batches[split])
    if splits['train'] & splits['validation']:
        raise ValueError('训练和验证的拓扑度序列重叠')
    return batches, metadata, fingerprints, splits


def check_holdout(folder, fingerprints, signature_groups):
    candidates = [read(path) for path in sorted(folder.glob('b*.json'))]
    if not candidates:
        raise ValueError('评测输入目录为空')
    if any(fingerprint(payload) in fingerprints or topology_signature(payload) in signature_groups for payload in candidates):
        raise ValueError('训练包与独立评测存在重复配置或拓扑度序列')
    return len(candidates)


def write_config_benchmark(output, examples, metadata, *, split='validation'):
    version = VERSION + '-' + split
    pack = output / 'inputs-pack'
    (pack / 'inputs').mkdir(parents=True)
    by_case = {row['case_id']: row for row in metadata if row['split'] == split}
    cases, labels = [], {}
    for index, example in enumerate(examples, 1):
        source, answer = (json.loads(message['content']) for message in example['messages'][1:])
        case_id = f'b{index:03d}'
        details = by_case[source['case_id']]
        relative = f'inputs/{case_id}.json'
        request = {key: source[key] for key in ('question', 'configs', 'topology', 'requirements')}
        write(pack / relative, {**request, 'hint_level': 1, 'use_llm': False, 'evidence_kind': 'demo'})
        cases.append({'id': case_id, 'file': relative, 'sha256': digest(pack / relative)})
        labels[case_id] = {
            **{key: answer[key] for key in ('status', 'findings', 'unknowns')},
            'family': details['topology'], 'condition': details['scenario'],
            'case_type': {'fault_found': 'fault', 'needs_information': 'unknown',
                          'awaiting_verification': 'normal'}[answer['status']],
        }
    write(pack / 'manifest.json', {
        'version': version, 'origin': VERSION, 'device_verified': 0,
        'review_status': 'generated_faults_checked_against_rules', 'cases': cases,
    })
    write(output / 'labels.json', {'version': version, 'cases': labels})
    return len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='项目 runtime 内的新目录')
    parser.add_argument('--format', choices=('sft', 'benchmark'), default='sft',
                        help='benchmark: export one topology split as separate inputs and labels')
    parser.add_argument('--benchmark-split', choices=('train', 'validation'),
                        help='seed split to export in benchmark format, default validation')
    parser.add_argument('--holdout-inputs', type=Path, help='可选的独立诊断评测输入目录，仅检查配置及拓扑重复')
    args = parser.parse_args()
    if args.benchmark_split and args.format != 'benchmark':
        parser.error('--benchmark-split 适用于 --format benchmark')
    try:
        root = (ROOT / 'runtime').resolve()
        output = args.output.resolve()
        if output == root or not output.is_relative_to(root):
            raise ValueError('输出应位于项目 runtime 的子目录')
        batches, metadata, fingerprints, splits = build_dataset()
        held_out = check_holdout(args.holdout_inputs, fingerprints, set.union(*splits.values())) if args.holdout_inputs else None
        output.mkdir(parents=True, exist_ok=False)
        if args.format == 'benchmark':
            split = args.benchmark_split or 'validation'
            count = write_config_benchmark(output, batches[split], metadata, split=split)
            print(f'导出 {count} 例配置及独立标签到 {output}。')
            return
        for split, rows in batches.items():
            with (output / f'{split}.jsonl').open('x', encoding='utf-8', newline='\n') as file:
                for row in rows:
                    file.write(json.dumps(row, ensure_ascii=False) + '\n')
        write(output / 'manifest.json', {
            'version': VERSION, 'format': 'sft_messages_jsonl', 'split_by': 'topology',
            'splits': {split: len(rows) for split, rows in batches.items()},
            'status_by_split': {split: dict(Counter(row['status'] for row in metadata if row['split'] == split)) for split in batches},
            'diagnosis_evaluation_inputs_checked': held_out, 'cases': metadata,
        })
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    print(f"训练 {len(batches['train'])} 条，验证 {len(batches['validation'])} 条，核对独立评测输入 {held_out if held_out is not None else '未提供'}。")


if __name__ == '__main__':
    main()
