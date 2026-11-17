"""Run small DeepSeek Flash checks for course references and missing OSPF evidence."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.investigation import investigate
from labagent.model import load_env, public_config


SCOPE = 'b-evidence-gap-v1'
BASE = ROOT / 'data/team_cases/lab_02'
QUESTION = 'PC1 ping 不到 PC2。请判断 R1 与 R2 互联口的 OSPF Area 是否确实配错，能确认的话说出两端区域号，需要什么材料也请直接告诉我。'
REFERENCE = {
    'scope': SCOPE, 'title': 'OSPF 区域检查对照题', 'version': 'v1',
    'source_name': 'LabAgent 自编测试资料', 'reference_url': '',
    'content': ('# OSPF 区域不一致\n\n互联接口的 OSPF 区域必须一致。'
                '讲义里有一个独立示例：R1 的互联口属于 Area 0，R2 的互联口属于 Area 17。'
                '该示例的具体数字只对应示例拓扑。请从当前配置或接口输出获取实际链路的区号。'),
}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def prepare_cases(ask_reference):
    fault = read(BASE / 'input.json')
    fixed = read(BASE / 'repaired.json')['configs']
    # Hide the expected Area in all cases. The topology still identifies the same link.
    topology = dict(fault['topology'], links=[{key: value for key, value in link.items() if key != 'expected_area'}
                                            for link in fault['topology']['links']])
    # Public snapshots replace the validation records; no student configuration is sent.
    symptom = {'学生记录.txt': '实验连接：R1 与 R2 直接相连，PC1 在 R1 侧，PC2 在 R2 侧。\nPC1 到 PC2 的 ping 失败，尚未收集互联接口配置。'}
    partial = {'R1': fault['configs']['R1']}
    cases = {
        'symptom-with-course': (symptom, True, 'request_both_without_specific_area'),
        'one-sided-with-course': (partial, True, 'request_r2_without_specific_area'),
        'one-sided-no-course': (partial, False, 'request_r2_without_specific_area'),
        'matching-with-course': (fixed, True, 'match_area_0_exclude_mismatch_keep_open'),
        'mismatch-with-course': (fault['configs'], True, 'cite_both_area_0_and_1_request_planned_area'),
        'mismatch-planned-area-0': (fault['configs'], True, 'fix_r2_to_area_0_then_verify'),
    }
    requests, expected = {}, {}
    for case, (configs, with_course, label) in cases.items():
        if ask_reference and not with_course:
            continue
        expected[case] = label
        question = ('请先检索当前选定的 OSPF 课程讲义并核对区域规则。' if ask_reference else '') + QUESTION
        if case == 'mismatch-planned-area-0':
            question += '实验明确要求 R1 和 R2 的互联接口都在 Area 0。请给出修正方向和复测方式。'
        requests[case] = {'diagnostic_mode': 'open_network', 'question': question, 'configs': configs, 'use_llm': True,
                          'knowledge_scope': SCOPE, 'course_documents': [REFERENCE] if with_course else [],
                          'evidence_kind': 'demo', 'topology': topology}
    return requests, expected


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def run_case(folder, item):
    case, request = item
    result = investigate(request)
    write(folder / f'{case}.json', result)
    llm = result['llm']
    return {'case': case, 'status': result['status'], 'model_status': llm['status'], 'model': llm.get('model'),
            'error': llm.get('error_code'), 'requests': len(result['next_steps']), 'findings': len(result['findings']),
            'retrievals': sum(t['tool'] == 'retrieve_reference' and t['status'] == 'ok' for t in llm['tool_trace']),
            'course_returned': any(doc['title'] == REFERENCE['title'] for doc in result['knowledge']),
            'calls': len(llm.get('calls', [])),
            'tokens': sum(c.get('usage', {}).get('total_tokens', 0) for c in llm.get('calls', []))}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--ask-reference', action='store_true', help='explicitly request the course notes in each question')
    cli.add_argument('--output', required=True, type=Path, help='new results folder under runtime/')
    cli.add_argument('--env-file', type=Path, default=ROOT / '.env', help='local DeepSeek Flash settings')
    cli.add_argument('--case', action='append', default=[], help='select a case ID, repeat to select more cases')
    args = cli.parse_args()
    folder = args.output.resolve()
    if not folder.is_relative_to(ROOT / 'runtime') or folder == ROOT / 'runtime':
        cli.error('place the results in a new folder under runtime/')
    requests, expected = prepare_cases(args.ask_reference)
    cases = list(dict.fromkeys(args.case)) or list(requests)
    if any(case not in requests for case in cases):
        cli.error('available cases: ' + ', '.join(requests))
    load_env(args.env_file)
    if public_config() != {'configured': True, 'provider': 'DeepSeek', 'model': 'deepseek-flash'}:
        cli.error('expected the local DeepSeek Flash configuration')
    folder.mkdir(parents=True, exist_ok=False)
    write(folder / 'requests.json', {case: requests[case] for case in cases})
    write(folder / 'expected.json', {case: expected[case] for case in cases})
    items = [(case, requests[case]) for case in cases]
    first = run_case(folder, items[0])
    print(first, flush=True)
    if first['error'] in {'authentication_failed', 'insufficient_balance', 'configuration_error', 'network_error'}:
        write(folder / 'summary.json', [first])
        return 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = [first]
        for report in pool.map(lambda item: run_case(folder, item), items[1:]):
            rows.append(report)
            print(report, flush=True)
    write(folder / 'summary.json', rows)
    return int(any(row['model_status'] != 'ok' for row in rows))


if __name__ == '__main__':
    raise SystemExit(main())
