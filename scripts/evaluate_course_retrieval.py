"""Evaluate course retrieval separately from the diagnostic model and its answers."""

import argparse
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from labagent import knowledge
from labagent.model import load_env
from scripts.evaluate_b import read, score


HIT_FIELDS = ('id', 'chunk_id', 'content', 'char_start', 'char_end', 'line_start', 'line_end')
TOP_K = 3
CHUNK_OVERLAP = 120
BM25_AUDIT_SETTINGS = {'local_bm25_with_fault_tags': {}, 'bm25_ospf_bilingual_terms': {'bilingual_terms': True},
                       f'bm25_overlap_{CHUNK_OVERLAP}': {'chunk_overlap': CHUNK_OVERLAP}}


def load_package(folder):
    manifest, questions = (read(folder / name) for name in ('manifest.json', 'questions.json'))
    if questions['version'] != manifest['version']:
        raise ValueError('题集与资料清单的版本不同')
    questions = questions['cases']
    if len({q['id'] for q in questions}) != len(questions) or not questions:
        raise ValueError('题集为空或存在重复题号')
    for q in questions:
        if set(q) - {'id', 'query', 'category', 'language'}:
            raise ValueError('题目只接受 id、query、category、language；答案须放在独立标注文件中')
        for key in ('id', 'query', 'category', 'language'):
            if not isinstance(q.get(key), str) or not q[key].strip():
                raise ValueError(f'题目的 {key} 须为非空文字')
        if len(q['query']) > 10000:
            raise ValueError('检索问题须为10000字以内')
    scope = knowledge.validate_scope(manifest['scope'])
    if scope in {'default', 'common'}:
        raise ValueError('请为课程评测指定独立资料范围')
    sources = {}
    for item in manifest['documents']:
        path = (folder / item['file']).resolve()
        if not path.is_relative_to(folder.resolve()) or item['key'] in sources:
            raise ValueError('资料路径不在包内或资料代号重复')
        sources[item['key']] = {**{key: item[key] for key in ('title', 'version', 'source_name', 'reference_url')},
                                'scope': scope, 'content': path.read_text(encoding='utf-8-sig')}
    if not sources:
        raise ValueError('课程资料清单为空')
    return manifest['version'], scope, sources, questions


def prepare_corpus(scope, sources):
    ids = {key: knowledge.import_document(source)['document']['id'] for key, source in sources.items()}
    documents, warnings = knowledge.corpus(scope)
    unexpected = [doc['title'] for doc in documents if doc['scope'] != 'common' and doc['id'] not in ids.values()]
    if warnings or unexpected:
        raise ValueError('课程范围包含清单外资料或读取错误，请使用独立的本地资料目录复评')
    return ids, {doc['id']: doc['content'] for doc in documents}


def run_search(version, scope, questions, *, bilingual_terms=False, chunk_overlap=0):
    documents, warnings = knowledge.corpus(scope)
    if warnings:
        raise ValueError('课程资料读取或校验失败，请修复后重新检索')
    options = {}
    if bilingual_terms:
        options['bilingual_terms'] = True
    if chunk_overlap:
        options['chunk_overlap'] = chunk_overlap
    searches = {q['id']: knowledge.rank_documents(q['query'], documents, scope=scope, limit=TOP_K, **options) for q in questions}
    first = next(iter(searches.values()), {})
    result = {'version': version, 'scope': scope, 'method': first.get('method', 'local_bm25_with_fault_tags'), 'predictions': {
        q['id']: [{field: passage[field] for field in HIT_FIELDS}
                  for passage in searches[q['id']]['results']]
        for q in questions
    }}
    result['match_summaries'] = {qid: search['match_summary'] for qid, search in searches.items()}
    result['settings'] = first.get('settings', {})
    result['sources'] = [knowledge.summary(doc) for doc in documents]
    return result


def reference_bounds(content, first, last):
    lines = content.splitlines(keepends=True)
    if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(lines):
        raise ValueError('答案行号超出资料原文范围')
    start = sum(len(line) for line in lines[:first - 1])
    end = sum(len(line) for line in lines[:last])
    support = content[start:end]
    if not support.strip():
        raise ValueError('答案行未包含原文')
    return start + len(support) - len(support.lstrip()), start + len(support.rstrip())


def valid_span(passage, corpus):
    if not isinstance(passage, dict) or not isinstance(passage.get('id'), str):
        return None
    source = corpus.get(passage['id'])
    start, end = passage.get('char_start'), passage.get('char_end')
    if source is None or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source):
        return None
    if (source[start:end] != passage.get('content') or
            type(passage.get('line_start')) is not int or type(passage.get('line_end')) is not int or
            passage.get('line_start') != source.count('\n', 0, start) + 1 or
            passage.get('line_end') != source.count('\n', 0, end - 1) + 1):
        return None
    return passage['id'], start, end


def reference_targets(labels, ids, corpus):
    if not isinstance(labels, list):
        raise ValueError('每题标注须为数组；资料不足题使用空数组')
    targets = []
    for label in labels:
        if not isinstance(label, dict) or set(label) != {'document', 'start_line', 'end_line'}:
            raise ValueError('标注须包含 document、start_line、end_line')
        doc_id = ids[label['document']]
        targets.append((doc_id, *reference_bounds(corpus[doc_id], label['start_line'], label['end_line'])))
    if len(set(targets)) != len(targets):
        raise ValueError('同一题包含重复标注')
    return targets


def covered_targets(span, targets):
    doc, start, end = span
    return {index for index, (target_doc, first, last) in enumerate(targets)
            if doc == target_doc and start <= first and end >= last}


def summarize(rows):
    answerable = [row for row in rows if row['answerable']]
    unanswered = [row for row in rows if not row['answerable']]
    count_passages = sum(row['returned'] for row in rows)
    valid_passages = count_passages - sum(row['invalid_citations'] for row in rows)
    return {'top1': score(sum(row['rank'] == 1 for row in answerable), len(answerable)),
            'top3': score(sum(row['rank'] is not None for row in answerable), len(answerable)),
            'mrr_at_3': round(sum(1 / row['rank'] for row in answerable if row['rank']) / len(answerable), 6) if answerable else None,
            'evidence_coverage': score(sum(row['evidence_found'] for row in answerable), sum(row['evidence_total'] for row in answerable)),
            'unanswerable_returned': score(sum(bool(row['returned']) for row in unanswered), len(unanswered)),
            'unanswerable_empty': score(sum(not row['returned'] for row in unanswered), len(unanswered)),
            'source_alignment': score(valid_passages, count_passages)}


def evaluate(version, scope, questions, labels, predictions, ids, corpus):
    if labels['version'] != version or predictions['version'] != version or predictions['scope'] != scope:
        raise ValueError('答案、预测和评测资料的版本或范围不一致')
    expected, submitted = labels['cases'], predictions['predictions']
    if not questions or len({q['id'] for q in questions}) != len(questions):
        raise ValueError('题集为空或存在重复题号')
    if set(expected) != set(submitted) or set(expected) != {q['id'] for q in questions}:
        raise ValueError('预测、题集和答案的题号必须一一对应')
    rows = []
    for q in questions:
        targets = reference_targets(expected[q['id']], ids, corpus)
        passages = submitted[q['id']]
        if not isinstance(passages, list) or len(passages) > TOP_K:
            raise ValueError(f"{q['id']} 需要 0～{TOP_K} 条候选")
        rank, invalid, found = None, 0, set()
        for number, passage in enumerate(passages, 1):
            span = valid_span(passage, corpus)
            if span is None:
                invalid += 1
                continue
            matches = covered_targets(span, targets)
            if matches:
                found.update(matches)
                rank = rank or number
        rows.append({'id': q['id'], 'category': q['category'], 'language': q['language'], 'answerable': bool(targets),
                     'rank': rank, 'returned': len(passages), 'invalid_citations': invalid,
                     'evidence_found': len(found), 'evidence_total': len(targets)})
    result = {'version': version, 'scope': scope, 'method': predictions['method'], 'questions': len(rows),
              'all': summarize(rows), 'records': rows}
    for field in ('category', 'language'):
        result[f'by_{field}'] = {value: summarize([row for row in rows if row[field] == value])
                                 for value in sorted({row[field] for row in rows})}
    return result


def diagnose_misses(report, questions, labels, ids, corpus, documents):
    """Check if missed evidence can fit in a candidate and where BM25 ranks it."""
    if report['method'] not in BM25_AUDIT_SETTINGS:
        raise ValueError('漏检分析支持本脚本的 BM25、术语扩展及重叠切分预测')
    options = BM25_AUDIT_SETTINGS[report['method']]
    candidates = [valid_span(chunk, corpus) for doc in documents
                  for chunk in knowledge.chunks(doc, overlap=options.get('chunk_overlap', 0))]
    if any(span is None for span in candidates):
        raise ValueError('候选片段无法定位到原文')
    queries = {q['id']: q['query'] for q in questions}
    counts = dict.fromkeys(('outside_chunks', 'rank_4_to_10', 'outside_top_10', 'missing_from_predictions'), 0)
    misses = []
    for row in report['records']:
        if not row['answerable'] or row['rank'] is not None:
            continue
        qid = row['id']
        targets = reference_targets(labels['cases'][qid], ids, corpus)
        rank = None
        if not any(covered_targets(span, targets) for span in candidates):
            cause = 'outside_chunks'
        else:
            ranked = knowledge.rank_documents(queries[qid], documents, scope=report['scope'], limit=10, **options)['results']
            rank = next((i for i, chunk in enumerate(ranked, 1) if covered_targets(valid_span(chunk, corpus), targets)), None)
            cause = 'outside_top_10' if rank is None else 'rank_4_to_10' if rank > TOP_K else 'missing_from_predictions'
        counts[cause] += 1
        misses.append({'id': qid, 'cause': cause, 'rank_at_10': rank})
    return {'candidate_count': len(candidates), 'counts': counts, 'cases': misses}


def main():
    load_env(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('search', 'score'):
        sub = commands.add_parser(command)
        sub.add_argument('package', type=Path, help='包含 manifest.json、questions.json 和原文的目录')
        sub.add_argument('output', type=Path)
        if command == 'search':
            methods = sub.add_mutually_exclusive_group()
            methods.add_argument('--bilingual-terms', action='store_true', help='试用中文 OSPF 术语到英文正文的候选排序')
            methods.add_argument('--overlap-chunks', action='store_true', help='试用同章节片段保留最多 120 字重叠')
        else:
            sub.add_argument('--labels', required=True, type=Path)
            sub.add_argument('--predictions', required=True, type=Path)
            sub.add_argument('--diagnose-misses', action='store_true', help='用当前 BM25 候选定位切分或排序造成的漏检')
    args = parser.parse_args()
    try:
        output_path = args.output.resolve()
        if not output_path.is_relative_to((ROOT / 'runtime').resolve()):
            raise ValueError('输出文件须位于项目 runtime 内')
        if output_path.exists():
            raise ValueError('输出文件已存在，请换用新文件名以保留对照结果')
        version, scope, sources, questions = load_package(args.package)
        ids, corpus = prepare_corpus(scope, sources)
        if args.command == 'search':
            result = run_search(version, scope, questions, bilingual_terms=args.bilingual_terms,
                                chunk_overlap=CHUNK_OVERLAP if args.overlap_chunks else 0)
        else:
            labels = read(args.labels)
            result = evaluate(version, scope, questions, labels, read(args.predictions), ids, corpus)
            if args.diagnose_misses:
                result['miss_analysis'] = diagnose_misses(result, questions, labels, ids, corpus, knowledge.corpus(scope)[0])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open('x', encoding='utf-8') as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write('\n')
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    print(f'{len(questions)} 题 → {args.output}')


if __name__ == '__main__':
    main()
