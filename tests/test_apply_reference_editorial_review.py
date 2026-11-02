"""Editor decisions change the queue without falsifying graph edges."""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_reference_editorial_review as flow


class EditorialReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ['data', 'docs']:
            (self.root / name).mkdir()
        self.sources = []
        edges = []
        for sid in ['1', '2']:
            ref = {'target_id': '9', 'target_title': '目标', 'recommended_zhihu_url': 'https://zhuanlan.zhihu.com/p/9',
                   'target_archive_url': 'https://example.org/9.html', 'original_urls': ['https://example.org/original'],
                   'original_hosts': ['example.org'], 'link_texts': ['原文'], 'occurrences': 1,
                   'match_quality': ['exact'], 'confidence': 'high', 'category': 'content', 'review_reason': '旧说明'}
            source = {'source_id': sid, 'source_title': '来源' + sid, 'source_zhihu_url': 'https://zhuanlan.zhihu.com/p/' + sid,
                      'missing_zhihu_reference_count': 1, 'references': [ref], 'reference_live_status': 'verified', 'reference_checked_at': 'date'}
            self.sources.append(source)
            edges.append({'source_id': sid, 'source_title': source['source_title'], 'source_zhihu_url': source['source_zhihu_url'],
                          **copy.deepcopy(ref), 'reference_live_status': 'verified', 'reference_checked_at': 'date'})
        edges.append({'source_id': '7', 'target_id': '9'})  # Previously resolved stale flat edge.
        candidates = {'metadata': {'candidate_edges': 2, 'source_articles': 2, 'top_targets': [],
                                   'candidate_recheck': {'remaining_candidate_edges': 2}},
                      'sources': self.sources, 'edges': edges}
        self.write_json('data/missing-zhihu-references.json', candidates)
        previous = (self.root / 'data/missing-zhihu-references.json').read_bytes()
        self.decisions = {'metadata': {'source_candidate_sha256': hashlib.sha256(previous).hexdigest()},
                          'decisions': [{'source_id': '1', 'target_id': '9', 'status': 'pending', 'reason': '范围仍需判断'},
                                        {'source_id': '2', 'target_id': '9', 'status': 'excluded', 'reason': '已有等价出处'}]}
        self.write_json('data/reference-editorial-decisions.json', self.decisions)
        graph_meta = {'missing_zhihu_reference_candidates': 2, 'articles_with_missing_zhihu_references': 2,
                      'candidate_recheck': {'remaining_candidate_edges': 2}}
        graph = {'metadata': graph_meta, 'edges': [{'source_id': sid, 'target_id': '9', 'via': ['original']} for sid in ['1', '2']]}
        self.write_json('data/references.json', graph)
        articles = [{'id': sid, 'summary': '必须保留的总结', 'missing_zhihu_reference_count': 1,
                     'references': [{'id': '9', 'title': '目标', 'via': ['original']}], 'referenced_by': []} for sid in ['1', '2']]
        articles.append({'id': '9', 'summary': '目标总结', 'missing_zhihu_reference_count': 0, 'references': [], 'referenced_by': []})
        catalog = {'metadata': {'references': graph_meta}, 'articles': articles}
        self.write_json('data/catalog.json', catalog)
        self.write_json('data/validation.json', {'references': graph_meta})
        self.write_json('data/missing-zhihu-reference-review-method.json', {'input': {}, 'counts': {}, 'validation': {}})
        rows = [{'source_id': sid, 'target_id': '9', 'confidence': 'high', 'review_reason': '旧说明'} for sid in ['1', '2']]
        (self.root / 'data/missing-zhihu-references.csv').write_text(flow.csv_text(rows, ['source_id', 'target_id', 'confidence', 'review_reason']), encoding='utf-8')
        (self.root / 'README.md').write_text('- `missing-zhihu-reference-review-method.json`：候选的置信分级、类别规则与抽样审计\n\n2026-11-02 完成两轮本地备份复核。旧信息。\n', encoding='utf-8')
        payload = {'records': articles, 'report': catalog['metadata'], 'answerIndex': {'untouched': 1}}
        (self.root / 'index.html').write_text('<script id="catalog-data" type="application/json">' + json.dumps(payload, ensure_ascii=False) + '</script>\n', encoding='utf-8')

    def write_json(self, name, value):
        (self.root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def prepare(self):
        return flow.prepare_updates(self.root, self.root / 'data/reference-editorial-decisions.json')

    def test_filters_queue_without_changing_graph_or_summaries(self):
        updates = self.prepare()
        candidates = json.loads(updates['data/missing-zhihu-references.json'])
        self.assertEqual([(x['source_id'], x['target_id']) for x in candidates['edges']], [('1', '9')])
        self.assertEqual(len(candidates['sources']), 1)
        self.assertEqual(candidates['sources'][0]['references'][0]['review_reason'], '范围仍需判断')
        self.assertEqual(candidates['edges'][0]['review_reason'], '范围仍需判断')
        self.assertEqual(candidates['edges'][0]['confidence'], 'review')
        self.assertEqual(candidates['edges'][0]['initial_confidence'], 'high')
        graph = json.loads(updates['data/references.json'])
        self.assertEqual(len(graph['edges']), 2)
        self.assertEqual(graph['metadata']['missing_zhihu_reference_candidates'], 2)
        self.assertEqual(graph['metadata']['editorial_pending_candidates'], 1)
        catalog = json.loads(updates['data/catalog.json'])
        self.assertEqual([x['summary'] for x in catalog['articles']], ['必须保留的总结', '必须保留的总结', '目标总结'])
        self.assertEqual([x['missing_zhihu_reference_count'] for x in catalog['articles']], [1, 0, 0])
        self.assertTrue(catalog['articles'][0]['references'][0]['pending_review'])
        self.assertNotIn('pending_review', catalog['articles'][1]['references'][0])
        self.assertEqual(list(csv.DictReader(io.StringIO(updates['data/missing-zhihu-references.csv'])))[0]['review_reason'], '范围仍需判断')
        self.assertIn('留下 2 条原文链接对应关系', updates['README.md'])
        html_payload = json.loads(flow.DATA.search(updates['index.html']).group(2))
        self.assertEqual(html_payload['records'], catalog['articles'])
        self.assertEqual(html_payload['report'], catalog['metadata'])
        self.assertEqual(html_payload['answerIndex'], {'untouched': 1})

    def test_apply_is_idempotent(self):
        for name, text in self.prepare().items():
            (self.root / name).write_text(text, encoding='utf-8')
        self.assertEqual(self.prepare(), {})

    def test_wrong_source_hash_and_decision_coverage_rejected(self):
        self.decisions['metadata']['source_candidate_sha256'] = '0' * 64
        self.write_json('data/reference-editorial-decisions.json', self.decisions)
        with self.assertRaisesRegex(ValueError, 'changed since'):
            self.prepare()
        self.decisions['metadata']['source_candidate_sha256'] = hashlib.sha256((self.root / 'data/missing-zhihu-references.json').read_bytes()).hexdigest()
        self.decisions['decisions'].pop()
        self.write_json('data/reference-editorial-decisions.json', self.decisions)
        with self.assertRaisesRegex(ValueError, 'cover exactly'):
            self.prepare()

    def test_inconsistent_flat_edge_rejected(self):
        candidates = json.loads((self.root / 'data/missing-zhihu-references.json').read_text())
        candidates['edges'][0]['target_title'] = '错误标题'
        self.write_json('data/missing-zhihu-references.json', candidates)
        self.decisions['metadata']['source_candidate_sha256'] = hashlib.sha256((self.root / 'data/missing-zhihu-references.json').read_bytes()).hexdigest()
        self.write_json('data/reference-editorial-decisions.json', self.decisions)
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            self.prepare()


if __name__ == '__main__':
    unittest.main()
