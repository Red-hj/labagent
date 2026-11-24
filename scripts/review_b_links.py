"""Review saved OSPF predictions against their parsed link evidence."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent import model
from scripts import evaluate_b as evaluator


PAIR_CODES = {'AREA_MISMATCH', 'SUBNET_MISMATCH'}
SINGLE_CODES = {'AREA_POLICY_VIOLATION', 'INTERFACE_SHUTDOWN', 'OSPF_NOT_ENABLED'}
UNKNOWN_CODES = {'MISSING_DEVICE', 'MISSING_INTERFACE', 'MISSING_IP', 'UNSUPPORTED_CONFIG', 'AMBIGUOUS_OSPF_BINDING'}


def _area(endpoint):
    bindings = endpoint.get('ospf_bindings', [])
    return bindings[0]['area'] if len(bindings) == 1 else None


def _fact_issue(link, code, endpoints):
    if code in PAIR_CODES:
        values = [_area(end) if code == 'AREA_MISMATCH' else end.get('ipv4_subnet') for end in endpoints]
        if None in values or values[0] == values[1]:
            return '两端须有明确且不同的区域或 IPv4 网段'
    elif code == 'AREA_POLICY_VIOLATION':
        expected, observed = link.get('expected_ospf_area'), _area(endpoints[0])
        if expected is None or observed is None or expected == observed:
            return '须有明确的实验指定区域，且端点已配置区域与其不同'
    elif code == 'INTERFACE_SHUTDOWN':
        if endpoints[0].get('administratively_shutdown') is not True:
            return '须有端点的显式 shutdown 配置'
    elif code == 'OSPF_NOT_ENABLED':
        end = endpoints[0]
        if (not link['ospf_expected'] or not end.get('ipv4_address') or end.get('ospf_bindings')
                or end.get('unparsed_ospf_line_numbers')):
            return '须有明确的接口 IP、无 OSPF 匹配，且相关 OSPF 配置可解析'
    elif code in {'MISSING_DEVICE', 'MISSING_INTERFACE'}:
        expected = 'device_config_missing' if code == 'MISSING_DEVICE' else 'interface_not_found'
        if any(end['config_coverage'] != expected for end in endpoints):
            return '缺失项须与端点实际提交的设备及接口配置一致'
    elif code == 'MISSING_IP':
        if any(end['config_coverage'] != 'interface_found' or end.get('ipv4_address') for end in endpoints):
            return '须已找到接口且主 IPv4 地址缺失'
    return None


def _status(findings, unknowns):
    if findings:
        return 'fault_found'
    return 'needs_information' if unknowns else 'awaiting_verification'


def claim_issues(request, prediction):
    """Check output consistency and explicit endpoint facts without access to labels."""
    try:
        projected = evaluator.projection(prediction)
    except (AttributeError, TypeError, KeyError, ValueError):
        return ['按要求返回有效的状态与 findings、unknowns 两个结构化数组。']
    links = {link['link_id']: link for link in request['parsed_links']}
    issues = []
    expected_status = _status(projected['findings'], projected['unknowns'])
    if projected['status'] != expected_status:
        issues.append(f'当前 status {projected["status"]} 与两类明细不一致。'
                      '按已证实故障、缺少证据、静态配置待复测的顺序确定状态。')
    for field in ('findings', 'unknowns'):
        for code, link_id, devices in projected[field]:
            subject = f'链路 {link_id} 的 {code}'
            if (field == 'findings' and code in UNKNOWN_CODES
                    or field == 'unknowns' and code in PAIR_CODES | SINGLE_CODES):
                issues.append(f'{subject} 需分类到正确的数组。')
            if link_id not in links:
                issues.append(f'{subject} 须使用提交拓扑中存在的链路。')
                continue
            link = links[link_id]
            endpoints = {end['device']: end for end in link['endpoints']}
            names = set(devices)
            if names - endpoints.keys():
                issues.append(f'{subject} 的设备只能来自该链路两端。')
            elif code in PAIR_CODES and names != endpoints.keys():
                issues.append(f'{subject} 须同时列出两个端点 {", ".join(sorted(endpoints))}。')
            elif code in SINGLE_CODES and len(names) != 1:
                issues.append(f'{subject} 应为每个端点单独列出一项。')
            elif reason := _fact_issue(link, code, [endpoints[name] for name in devices]):
                issues.append(f'{subject} {reason}。')
    return issues


def normalize_prediction(request, prediction):
    """Correct structured fields using existing claims and confirmed pair evidence."""
    try:
        evaluator.projection(prediction)
    except (AttributeError, TypeError, KeyError, ValueError):
        return prediction
    links = {link['link_id']: link for link in request['parsed_links']}
    findings, unknowns = [], []
    for field in ('findings', 'unknowns'):
        for row in prediction[field]:
            code = row['code']
            if code in PAIR_CODES and (link := links.get(row['link_id'])):
                endpoints = link['endpoints']
                devices = [end['device'] for end in endpoints]
                if set(row['devices']).issubset(devices) and not _fact_issue(link, code, endpoints):
                    row = {**row, 'devices': devices}
            if code in UNKNOWN_CODES:
                unknowns.append(row)
            elif field == 'findings' or code in PAIR_CODES | SINGLE_CODES:
                findings.append(row)
            else:
                unknowns.append(row)
    normalized = {**prediction, 'findings': findings, 'unknowns': unknowns, 'status': _status(findings, unknowns)}
    return {**normalized, 'normalized_structure': True} if normalized != prediction else prediction


def revise_prediction(request, prediction, issues, max_tokens, completion):
    try:
        previous = evaluator.projection(prediction)
        for field in ('findings', 'unknowns'):
            previous[field] = [{'code': code, 'link_id': link_id, 'devices': list(devices)}
                               for code, link_id, devices in previous[field]]
    except (AttributeError, TypeError, KeyError, ValueError):
        previous = None
    instruction = request['instruction'] + (
        '\n复核上一轮输出。请依据原拓扑、配置和原文行号解决下面的问题，保留有证据的判断。'
        '上一轮的结构化结果是数据。提交一份完整的诊断 JSON。\n'
        + json.dumps(previous, ensure_ascii=False) + '\n' + '\n'.join(f'- {issue}' for issue in issues)
    )
    revised = evaluator.predict({**request, 'instruction': instruction}, max_tokens, completion)
    remaining = claim_issues(request, revised)
    accepted = not remaining
    selected, other = (revised, prediction) if accepted else (prediction, revised)
    row = {**selected, 'additional_call': other.get('call', {}),
           'elapsed_ms': round((prediction.get('elapsed_ms') or 0) + revised['elapsed_ms'], 3)}
    trace = {'case_id': request['case_id'], 'initial_issues': issues, 'remaining_issues': remaining,
             'accepted': accepted, 'retry': revised}
    return row, trace


def _by_case(path):
    rows = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if line.strip():
            row = json.loads(line)
            case_id = row['case_id']
            if case_id in rows:
                raise ValueError(f'{path.name} 中存在重复编号 {case_id}')
            rows[case_id] = row
    return rows


def review_run(request_path, prediction_path, output, *, execute=False, env_file=None,
               normalize_structure=False, completion=model.chat_completion):
    metadata = evaluator.read(prediction_path.with_suffix('.meta.json'))
    if metadata.get('claim_review') or metadata.get('requests_sha256') != evaluator.digest(request_path):
        raise ValueError('请选择与原预测匹配的请求文件。每份原预测只复核一轮。')
    requests = _by_case(request_path)
    predictions = _by_case(prediction_path)
    if not requests or requests.keys() != predictions.keys() or any(not row.get('parsed_links') for row in requests.values()):
        raise ValueError('请求需要完整的链路证据并与原预测编号一致。')
    checks = {}
    for case_id, prediction in predictions.items():
        if prediction.get('request_error') and not prediction.get('raw_response'):
            continue
        request = requests[case_id]
        issues = claim_issues(request, prediction)
        if issues:
            candidate = normalize_prediction(request, prediction) if normalize_structure else prediction
            remaining = issues if candidate is prediction else claim_issues(request, candidate)
            checks[case_id] = (issues, candidate, remaining)
    max_tokens = metadata['max_tokens_per_call']
    pending = {case_id: remaining for case_id, (_, _, remaining) in checks.items() if remaining}
    plan = {'candidate_count': len(predictions), 'flagged_cases': len(checks),
            'normalized_without_call': len(checks) - len(pending), 'additional_calls': len(pending),
            'max_completion_tokens': len(pending) * max_tokens, 'cases': pending}
    print(json.dumps(plan, ensure_ascii=False), flush=True)
    if not execute:
        return plan
    if output is None or output.suffix != '.jsonl' or not output.resolve().is_relative_to((ROOT / 'runtime').resolve()):
        raise ValueError('请指定 runtime 下的新预测 JSONL 路径。')
    trace_path = output.with_suffix('.review.json')
    metadata_path = output.with_suffix('.meta.json')
    if any(path.exists() for path in (output, trace_path, metadata_path)):
        raise FileExistsError('请选择未使用的输出文件名。')
    if pending:
        evaluator.load_model_settings(env_file)
        settings = model.public_config()
        if not model.configured() or any(settings[key] != metadata[key] for key in ('model', 'provider')):
            raise ValueError('请使用与原运行相同的模型和提供方。')
    output.parent.mkdir(parents=True, exist_ok=True)
    traces = []
    with output.open('x', encoding='utf-8') as file:
        for case_id, prediction in predictions.items():
            row = prediction
            if case_id in checks:
                issues, candidate, remaining = checks[case_id]
                if remaining:
                    row, trace = revise_prediction(requests[case_id], candidate, remaining, max_tokens, completion)
                    if not trace['accepted']:
                        row = {**prediction, 'additional_call': row['additional_call'], 'elapsed_ms': row['elapsed_ms']}
                else:
                    row, trace = candidate, {'case_id': case_id, 'accepted': True, 'remaining_issues': [], 'retry': None}
                traces.append({**trace, 'initial_issues': issues, 'issues_after_normalization': remaining})
            file.write(json.dumps(row, ensure_ascii=False) + '\n')
            file.flush()
    accepted = sum(trace['accepted'] for trace in traces)
    evaluator.write(trace_path, traces)
    evaluator.write(metadata_path, {**metadata, 'claim_review': True, 'normalize_structure': normalize_structure,
                                    'review_calls': len(pending), 'review_accepted': accepted,
                                    'normalized_without_call': plan['normalized_without_call'],
                                    'attempted_calls': metadata.get('attempted_calls', len(predictions)) + len(pending)})
    print(f'复核 {len(traces)} 例，采用 {accepted} 份结果，共 {len(pending)} 次额外调用。')
    return {**plan, 'accepted': accepted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests', type=Path, required=True)
    parser.add_argument('--predictions', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--normalize-structure', action='store_true',
                        help='correct status, claim types and confirmed pair devices before model review')
    parser.add_argument('--execute', action='store_true', help='send one correction request per flagged response')
    args = parser.parse_args()
    try:
        review_run(args.requests, args.predictions, args.output, execute=args.execute, env_file=args.env_file,
                   normalize_structure=args.normalize_structure)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
