"""Apply reviewed editorial decisions to the pending-reference index.

The reference graph continues to record every actual relation.  Only the
editorial queue, its displays and their counts are filtered.  The input
decisions and historical audit remain available for subsequent review.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile


DATA = re.compile(r'(<script id="catalog-data" type="application/json">)(.*?)(</script>)', re.S)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def csv_text(rows, fields, *, bom=False):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return ('\ufeff' if bom else '') + output.getvalue()


def pair(row):
    return str(row['source_id']), str(row['target_id'])


def prepare_updates(root: Path, decisions_path: Path):
    root, decisions_path = Path(root), Path(decisions_path)
    raw = decisions_path.read_bytes()
    decisions_file = json.loads(raw)
    decisions = decisions_file.get('decisions')
    if not isinstance(decisions, list) or not decisions:
        raise ValueError('Editorial decisions are missing')
    by_pair = {}
    for row in decisions:
        if not isinstance(row, dict) or row.get('status') not in {'pending', 'excluded'}:
            raise ValueError('Unknown editorial status')
        if not isinstance(row.get('reason'), str) or not row['reason'].strip():
            raise ValueError('Editorial reason is missing')
        key = pair(row)
        if not all(re.fullmatch(r'[1-9]\d*', value) for value in key) or key in by_pair:
            raise ValueError('Invalid or duplicate editorial pair')
        by_pair[key] = row
    pending = {key for key, row in by_pair.items() if row['status'] == 'pending'}
    if not pending:
        raise ValueError('No pending decisions')

    candidate_path = root / 'data/missing-zhihu-references.json'
    candidates = json.loads(candidate_path.read_bytes())
    sources = candidates.get('sources')
    raw_edges = candidates.get('edges')
    if not isinstance(sources, list) or not isinstance(raw_edges, list):
        raise ValueError('Candidate index is malformed')
    source_pairs = [(str(source['source_id']), str(ref['target_id']))
                    for source in sources for ref in source['references']]
    if len(set(source_pairs)) != len(source_pairs):
        raise ValueError('Duplicate source candidate')
    digest = hashlib.sha256(raw).hexdigest()
    existing = candidates['metadata'].get('editorial_review', {})
    if existing.get('decisions_sha256') == digest:
        edges_pairs = [pair(edge) for edge in raw_edges]
        if set(source_pairs) != pending or set(edges_pairs) != pending or len(edges_pairs) != len(pending):
            raise ValueError('Applied editorial data are inconsistent')
        return {}
    expected_source = decisions_file.get('metadata', {}).get('source_candidate_sha256')
    if not isinstance(expected_source, str) or hashlib.sha256(candidate_path.read_bytes()).hexdigest() != expected_source:
        raise ValueError('Candidate index changed since editorial decisions')
    if set(source_pairs) != set(by_pair):
        raise ValueError('Decisions do not cover exactly the current candidate queue')
    edges = {pair(edge): edge for edge in raw_edges}
    if len(edges) != len(raw_edges) or not set(source_pairs) <= set(edges):
        raise ValueError('Candidate edges are missing or duplicated')

    kept_sources = []
    for source in sources:
        source_id = str(source['source_id'])
        refs = []
        for reference in source['references']:
            key = source_id, str(reference['target_id'])
            if key in pending:
                ref = copy.deepcopy(reference)
                ref['initial_confidence'] = ref['confidence']
                ref['confidence'] = 'review'
                ref['review_reason'] = by_pair[key]['reason']
                refs.append(ref)
        if refs:
            row = copy.deepcopy(source)
            row['references'], row['missing_zhihu_reference_count'] = refs, len(refs)
            kept_sources.append(row)
    kept_edges = []
    for edge in raw_edges:
        key = pair(edge)
        if key in pending:
            row = copy.deepcopy(edge)
            row['initial_confidence'] = row['confidence']
            row['confidence'] = 'review'
            row['review_reason'] = by_pair[key]['reason']
            kept_edges.append(row)
    if len(kept_edges) != len(pending):
        raise ValueError('Pending edges could not be reconstructed')
    nested = {(str(source['source_id']), str(ref['target_id'])): ref
              for source in kept_sources for ref in source['references']}
    for edge in kept_edges:
        ref = nested[pair(edge)]
        if any(edge.get(field) != value for field, value in ref.items()):
            raise ValueError('Pending edge disagrees with source reference')
    candidates['sources'], candidates['edges'] = kept_sources, kept_edges
    refs = [ref for source in kept_sources for ref in source['references']]
    target_counts = Counter(str(ref['target_id']) for ref in refs)
    titles = {str(ref['target_id']): ref['target_title'] for ref in refs}
    metadata = candidates['metadata']
    previous_top = {str(row['id']): n for n, row in enumerate(metadata.get('top_targets', []))}
    review_info = {'decisions_file': decisions_path.name, 'decisions_sha256': digest,
                   'mechanical_candidates': len(by_pair), 'editorially_excluded': len(by_pair) - len(pending),
                   'pending_candidates': len(pending),
                   'scope': 'Historical candidate dispositions, not a new full-source or live-Zhihu scan.'}
    metadata.update({'definition': '待编辑判断的引用：译文含可唯一匹配的原文链接，尚需决定是否补充对应知乎译文。',
                     'candidate_edges': len(refs), 'source_articles': len(kept_sources), 'target_articles': len(target_counts),
                     'original_host_counts': dict(Counter(host for ref in refs for host in set(ref['original_hosts']))),
                     'top_targets': [{'id': tid, 'title': titles[tid], 'missing_incoming_zhihu_links': target_counts[tid]}
                                     for tid in sorted(target_counts, key=lambda x: (-target_counts[x], previous_top.get(x, 1000000), x))[:50]],
                     'confidence_counts': dict(Counter(ref['confidence'] for ref in refs)),
                     'category_counts': dict(Counter(ref['category'] for ref in refs)),
                     'unreviewed_candidate_edges': sum(ref['confidence'] == 'unreviewed' for ref in refs),
                     'verified_candidate_edges': sum(len(source['references']) for source in kept_sources if source['reference_live_status'] == 'verified'),
                     'editorial_review': review_info})
    metadata['unverified_candidate_edges'] = len(refs) - metadata['verified_candidate_edges']

    reference_data = json.loads((root / 'data/references.json').read_bytes())
    graph_pairs = [pair(edge) for edge in reference_data['edges']]
    if len(set(graph_pairs)) != len(graph_pairs) or not pending <= set(graph_pairs):
        raise ValueError('Reference graph does not contain each pending pair')
    graph_metadata = reference_data['metadata']
    graph_metadata['editorial_pending_candidates'] = len(pending)
    graph_metadata['editorial_pending_source_articles'] = len(kept_sources)
    graph_metadata['editorial_review'] = copy.deepcopy(review_info)
    # The pre-existing mechanical graph counts remain facts about the graph;
    # editorial exclusions do not turn original links into Zhihu links.
    if graph_metadata.get('missing_zhihu_reference_candidates') != len(by_pair):
        raise ValueError('Mechanical graph count differs from reviewed queue')

    catalog = json.loads((root / 'data/catalog.json').read_bytes())
    before_catalog = copy.deepcopy(catalog)
    source_counts = Counter(key[0] for key in pending)
    found = set()
    for article in catalog['articles']:
        source_id = str(article['id'])
        article['missing_zhihu_reference_count'] = source_counts[source_id]
        for reference in article.get('references', []):
            key = source_id, str(reference['id'])
            reference.pop('pending_review', None)
            reference.pop('pending_review_reason', None)
            if key in pending:
                reference['pending_review'] = True
                reference['pending_review_reason'] = by_pair[key]['reason']
                found.add(key)
    if found != pending:
        raise ValueError('Catalog outgoing references are missing pending pairs')
    catalog['metadata']['references'] = copy.deepcopy(graph_metadata)
    validation = json.loads((root / 'data/validation.json').read_bytes())
    validation['references'] = copy.deepcopy(graph_metadata)

    method = json.loads((root / 'data/missing-zhihu-reference-review-method.json').read_bytes())
    method['definition'] = metadata['definition']
    method['input']['candidate_edges'] = len(refs)
    method['counts'] = {'confidence': metadata['confidence_counts'], 'category': metadata['category_counts']}
    method['validation'].update({'record_count': len(refs), 'reused_review_rows': len(refs) - metadata['unreviewed_candidate_edges'],
                                 'unreviewed_candidates': metadata['unreviewed_candidate_edges']})
    method['editorial_review'] = copy.deepcopy(review_info)

    csv_path = root / 'data/missing-zhihu-references.csv'
    reader = csv.DictReader(io.StringIO(csv_path.read_text(encoding='utf-8-sig')))
    fields, old_csv = reader.fieldnames, list(reader)
    if not fields or len(old_csv) != len(source_pairs) or {pair(row) for row in old_csv} != set(source_pairs):
        raise ValueError('CSV and candidate sources differ')
    csv_rows = []
    for row in old_csv:
        key = pair(row)
        if key in pending:
            row['confidence'] = 'review'
            row['review_reason'] = by_pair[key]['reason']
            csv_rows.append(row)

    article_fields = ['source_id', 'source_title', 'source_zhihu_url', 'missing_zhihu_reference_count', 'target_titles', 'target_zhihu_urls']
    article_rows = []
    lines = ['# 仍需判断的译文引用', '',
             f'当前有 {len(pending)} 条待判断引用，涉及 {len(kept_sources)} 篇来源文章。此前审阅决定暂不添加的关系不列入本清单；原有引用图谱保留。', '',
             '|来源文章|目标译文|仍需判断的原因|', '|---|---|---|']
    escape = lambda value: str(value).replace('|', '\\|').replace('\n', ' ')
    for source in kept_sources:
        source_id = str(source['source_id'])
        article_rows.append({'source_id': source_id, 'source_title': source['source_title'], 'source_zhihu_url': source['source_zhihu_url'],
                             'missing_zhihu_reference_count': len(source['references']),
                             'target_titles': '；'.join(x['target_title'] for x in source['references']),
                             'target_zhihu_urls': '；'.join(x['recommended_zhihu_url'] for x in source['references'])})
        for ref in source['references']:
            lines.append('|[%s](%s)|[%s](%s)|%s|' % (escape(source['source_title']), source['source_zhihu_url'],
                         escape(ref['target_title']), ref['recommended_zhihu_url'], escape(ref['review_reason'])))
    lines += ['', '本清单反映既有候选的历史审阅决定，不代表已全量复查新文章或实时知乎。', '']

    html = (root / 'index.html').read_text(encoding='utf-8')
    matches = list(DATA.finditer(html))
    if len(matches) != 1:
        raise ValueError('Expected exactly one embedded catalog')
    match = matches[0]
    embedded = json.loads(match.group(2))
    if embedded['records'] != before_catalog['articles'] or embedded['report'] != before_catalog['metadata']:
        raise ValueError('Embedded catalog differs from canonical catalog')
    embedded['records'], embedded['report'] = catalog['articles'], catalog['metadata']
    payload = json.dumps(embedded, ensure_ascii=False).replace('&', r'\u0026').replace('<', r'\u003c').replace('>', r'\u003e')
    html = html[:match.start(2)] + payload + html[match.end(2):]

    readme = (root / 'README.md').read_text(encoding='utf-8')
    old_paragraph = re.compile(r'^2026-11-02 完成两轮本地备份复核。[^\n]+$', re.M)
    replacement = (
        '2026-11-02 的两轮本地备份核验从既有候选中移除 83 条已有知乎引用的关系，'
        f'留下 {len(by_pair)} 条原文链接对应关系。结合此前逐条编辑审阅，'
        f'其中 {len(by_pair) - len(pending)} 条暂不添加，'
        f'网站目前显示 {len(pending)} 条仍需判断的引用、涉及 {len(kept_sources)} 篇文章；'
        '完整审阅决定保存在 [编辑处置记录](data/reference-editorial-decisions.json)。'
        '这不改变原有引用图谱，也不是新文章的全量扫描或知乎实时核验。'
        '历史核验见 [最新复核报告](docs/missing-zhihu-reference-recheck-20261102-1230.md) 和 '
        '[首轮历史报告](docs/missing-zhihu-reference-recheck.md)；'
        '当前队列见 [文章列表](docs/missing-zhihu-reference-articles.md) 和 '
        '[CSV 列表](data/missing-zhihu-reference-articles.csv)。'
    )
    readme, replacements = old_paragraph.subn(lambda _: replacement, readme)
    if replacements != 1:
        raise ValueError('README reference review paragraph missing or duplicated')
    previous_bullet = ('- `missing-zhihu-reference-review-method.json`：候选的置信分级、类别规则与抽样审计')
    if readme.count(previous_bullet) != 1:
        raise ValueError('README reference-data list missing or duplicated')
    readme = readme.replace(previous_bullet, previous_bullet + '\n- `reference-editorial-decisions.json`：既有候选逐对编辑处置及待判断理由')

    return {'data/missing-zhihu-references.json': json_text(candidates),
            'data/missing-zhihu-references.csv': csv_text(csv_rows, fields),
            'data/missing-zhihu-reference-articles.csv': csv_text(article_rows, article_fields, bom=True),
            'data/missing-zhihu-reference-review-method.json': json_text(method),
            'data/references.json': json_text(reference_data), 'data/catalog.json': json_text(catalog),
            'data/validation.json': json_text(validation), 'index.html': html, 'README.md': readme,
            'docs/missing-zhihu-reference-articles.md': '\n'.join(lines)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--decisions', type=Path)
    args = parser.parse_args()
    decisions = args.decisions or args.root / 'data/reference-editorial-decisions.json'
    updates = prepare_updates(args.root, decisions)
    staged = []
    try:
        for name, content in updates.items():
            dest = args.root / name
            fd, temporary = tempfile.mkstemp(prefix='.' + dest.name + '.', dir=dest.parent)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                stream.write(content)
            staged.append((Path(temporary), dest))
        for temporary, dest in staged:
            os.replace(temporary, dest)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
    print(f'Updated {len(updates)} files')


if __name__ == '__main__':
    main()
