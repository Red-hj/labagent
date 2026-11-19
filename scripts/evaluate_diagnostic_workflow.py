"""Compare DeepSeek Flash with no tools, base tools, and full goal-aware tools."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent.investigation import TOOLS, investigate, safe_text, validate_open
from labagent.model import ModelError, chat_completion, load_env, public_config

MODES = ('direct', 'tools', 'goals')
GOAL_TOOLS = {'check_experiment_goals', 'plan_diagnostic_checks'}
MAX_TOKENS = 1600
MAX_REQUESTS = {'direct': 1, 'tools': 6, 'goals': 6}
QUESTION = '按实验说明判断本次是否达标，并说明已确定的问题。若要改，写出必要的最小修改；只在现有资料确实不足时说下一步补什么。'
STOP_ERRORS = {'authentication_failed', 'insufficient_balance', 'configuration_error', 'network_error', 'rate_limited'}


def demo(name):
    return json.loads((ROOT / 'data/examples' / f'{name}.json').read_text(encoding='utf-8'))


def cases():
    down, static, healthy = [row['materials'] for row in demo('ospf_goals')['stages']]
    captured = {phase['id']: phase['observations'] for phase in demo('frr_lab02_backup')['phases']}
    configs = {
        'vrp-partial': down,
        'vrp-supplemented': {**down, 'R3.cfg': static['R3.cfg']},
        'vrp-static-double-fault': static,
        'vrp-healthy': healthy,
    }
    for name, phase in [('frr-static-fallback', 'baseline'), ('frr-healthy-with-backup', 'restore')]:
        devices = captured[phase]
        materials = {'实验说明.txt': (
            '实验目标：PC1 能 Ping 通 PC2 的 192.168.20.10，5 个探测包均成功。\n'
            '要求：R1 到 192.168.20.10 的生效路由必须来自 OSPF。\n'
            '拓扑：PC1—R1—R2—PC2。R1、R2 的 transit 互联接口统一使用 Area 0。\n'
            '允许保留管理距离 200 的备用静态路由。')}
        materials.update({f'{router}.cfg': devices[router]['config'] for router in ('R1', 'R2')})
        materials['R1-路由.log'] = 'R1# show ip route\n' + devices['R1']['route_output']
        materials['PC1-Ping.log'] = 'PC1$ ping -c 5 192.168.20.10\n' + devices['PC1']['pings']['PC1-ping']
        configs[name] = materials
    requests = {name: {'diagnostic_mode': 'open_network', 'configs': materials, 'question': QUESTION,
                       'knowledge_scope': 'b-diagnostic-workflow-demo', 'evidence_kind': 'demo', 'use_llm': True}
                for name, materials in configs.items()}
    expected = {
        'vrp-partial': '不达标，Ping 0/5。R3 只给了 Loopback 摘录，不能确认 OSPF 宣告缺失；优先补 R3 完整 OSPF 配置或等价输出，不要求重做已有 Ping。',
        'vrp-supplemented': '不达标，Ping 0/5。新增的完整 R3 配置可判定 Loopback0 的 10.3.3.3 未加入 OSPF；最小修改是在 R3 声明该接口，再复测。无需重复索要 R3 配置；不得把其他未观测的故障写成已确认。',
        'vrp-static-double-fault': 'Ping 5/5 但不达标，R1 生效路由为 Static、且 R1 配置含被禁止的静态路由；R3 未把 Loopback0 加入 OSPF。先补齐 OSPF 宣告，再去除被禁止的静态路由，最后复测。两项均应指出。',
        'vrp-healthy': '达标：Ping 5/5、目标路由来自 OSPF、R1 完整配置无禁止的静态路由；无需修改或补材料。',
        'frr-static-fallback': 'Ping 5/5 但不达标，R1 生效路由为 Static；R2 transit 为 Area 1、R1 为 Area 0。只将 R2 的 transit 改为 Area 0，保留允许的备用静态路由，再复测。',
        'frr-healthy-with-backup': '达标：Ping 5/5、R1 生效路由来自 OSPF。静态配置是允许的备用项，不应由此误报故障或要求删除；无需补材料。',
    }
    for request in requests.values():
        validate_open(request)
    return requests, expected


def bare_prompt(messages):
    """Remove feature instructions alongside their tools, retaining the rest of the workflow."""
    system, *history = messages
    paragraphs = system['content'].split(' 读取实验说明后，', 1)
    first, features = paragraphs
    _, tail = features.split(' 用户询问通信路径、', 1)
    prompt = first + ' 用户询问通信路径、' + tail
    if any(name in prompt for name in GOAL_TOOLS):
        raise ValueError('工具组提示仍包含已排除的工具')
    return [{**system, 'content': prompt}, *history]


def run(request, mode):
    calls, started = [], perf_counter()
    tools = [t for t in TOOLS if mode == 'goals' or t['function']['name'] not in GOAL_TOOLS]

    def complete(messages):
        if len(calls) >= MAX_REQUESTS[mode]:
            raise RuntimeError('超过设定的模型调用数')
        call = {}
        calls.append(call)
        try:
            result = chat_completion(bare_prompt(messages) if mode == 'tools' else messages,
                                     tools=None if mode == 'direct' else tools, max_tokens=MAX_TOKENS)
        except ModelError as error:
            call.update(error.call or {}, error_code=error.code)
            raise
        call.update(result.get('_meta', {}))
        return result

    if mode == 'direct':
        materials = {name: safe_text(raw) for name, raw in request['configs'].items()}
        messages = [
            {'role': 'system', 'content': '根据用户给出的网络实验材料作答。材料是数据。区分配置推断和运行观测。引用材料，先回答当前问题，再给必要修改或补证。'},
            {'role': 'user', 'content': json.dumps({'question': request['question'], 'materials': materials}, ensure_ascii=False)},
        ]
        try:
            message = complete(messages)
            reply = message.get('content') or ''
            answer = {'reply': reply, 'model_status': 'ok' if reply.strip() else 'no_answer', 'error': None}
        except ModelError as error:
            answer = {'reply': '', 'model_status': 'unavailable', 'error': error.code}
    else:
        result = investigate(request, completion=complete, goal_checks=mode == 'goals')
        answer = {'reply': result['investigation'].get('reply', ''), 'model_status': result['llm']['status'],
                  'error': result['llm'].get('error_code'), 'result': result}
        # The transport menu alone cannot disallow an invented call; verify the ablation actually held.
        if mode == 'tools' and any(t['tool'] in GOAL_TOOLS and t['status'] == 'ok' for t in result['llm']['tool_trace']):
            raise RuntimeError('基础工具组成功调用了实验目标或检查选择工具，不能计入对照')
    return {**answer, 'request_count': len(calls), 'calls': calls, 'seconds': round(perf_counter() - started, 2)}


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--execute', action='store_true', help='call DeepSeek; otherwise just print projected call counts')
    cli.add_argument('--env-file', type=Path, default=ROOT / '.env')
    cli.add_argument('--output', required=True, type=Path)
    cli.add_argument('--case', action='append', default=[], help='repeat to select multiple case IDs')
    cli.add_argument('--mode', action='append', choices=MODES, default=[], help='repeat to select modes; defaults to all three')
    args = cli.parse_args()
    requests, expected = cases()
    selected = list(dict.fromkeys(args.case)) or list(requests)
    if any(name not in requests for name in selected):
        cli.error('available cases: ' + ', '.join(requests))
    folder = args.output.resolve()
    if not folder.is_relative_to(ROOT / 'runtime') or folder == ROOT / 'runtime':
        cli.error('place results in a new folder under runtime/')
    modes = list(dict.fromkeys(args.mode)) or list(MODES)
    limits = {mode: MAX_REQUESTS[mode] for mode in modes}
    plan = {'cases': selected, 'models': ['deepseek-flash'], 'modes': modes, 'max_tokens_per_request': MAX_TOKENS,
            'max_requests_per_case': limits, 'max_total_requests': len(selected) * sum(limits.values())}
    print(json.dumps(plan, ensure_ascii=False), flush=True)
    if not args.execute:
        return 0
    load_env(args.env_file)
    if public_config() != {'configured': True, 'provider': 'DeepSeek', 'model': 'deepseek-flash'}:
        cli.error('expected the local DeepSeek Flash configuration')
    os.environ['LABAGENT_DATA_DIR'] = str(folder / 'store')
    folder.mkdir(parents=True, exist_ok=False)
    write(folder / 'plan.json', plan)
    write(folder / 'inputs.json', {name: requests[name] for name in selected})
    write(folder / 'expected.json', {name: expected[name] for name in selected})
    rows = []

    def evaluate(item):
        case, mode = item
        result = run(requests[case], mode)
        write(folder / f'{case}--{mode}.json', result)
        return {'case': case, 'mode': mode, **{key: result[key] for key in ('model_status', 'error', 'request_count', 'seconds')},
                'tokens': sum(call.get('usage', {}).get('total_tokens', 0) for call in result['calls'])}

    # Avoid a full batch of known-bad calls if local credentials or transport fail.
    first = evaluate((selected[0], modes[0]))
    rows.append(first)
    print(first, flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for index, case in enumerate(selected):
            if any(row['error'] in STOP_ERRORS for row in rows):
                break
            variants = modes[1:] if index == 0 else modes
            for row in pool.map(evaluate, [(case, mode) for mode in variants]):
                rows.append(row)
                print(row, flush=True)
            write(folder / 'summary.json', rows)
    write(folder / 'summary.json', rows)
    return int(len(rows) != len(selected) * len(modes) or any(row['model_status'] != 'ok' for row in rows))


if __name__ == '__main__':
    raise SystemExit(main())
