"""Regression checks for review gates, history replay and coordinated outputs."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("summary_workflow", Path(__file__).resolve().parents[1] / "scripts/summary_workflow.py")
flow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flow)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.work = self.root / "work"
        self.body = "第一节：学习应关注实际理解。\n第二节：及时反馈可以帮助调整方法。\n" * 5
        self.row = {"id": "1", "title": "测试长文", "publisher": "Thoughts Memo", "publisher_token": "L.M.Sherlock",
                    "url": "https://example.com/1", "archive_url": "https://example.com/archive/1", "source_path": "article/1.json",
                    "source_sha256": "a" * 64, "content_sha256": "b" * 64, "read_chars": len(self.body),
                    "summary": "旧稿只提到了学习。", "model": flow.MODEL, "reasoning_effort": flow.EFFORT,
                    "worker_agent": "initial_writer", "worker_shard": "01", "keywords": ["理解", "反馈"],
                    "references": [{"id": "2"}]}
        self.catalog = {"metadata": {"included_articles": 1, "checks": {"old_check": True}}, "articles": [self.row]}
        flow.write_json(self.root / "data/catalog.json", self.catalog)
        flow.write_json(self.root / "data/validation.json", self.catalog["metadata"])
        embedded = {"records": [self.row], "report": self.catalog["metadata"], "answerIndex": {"answer": {"id": "9"}}, "exportOrder": ["1"]}
        self.html_prefix = '<style>.summary{white-space:pre-line}</style><button id="export-txt">导出</button>'
        self.html_suffix = '<script>const preserve = "export and search";</script>'
        (self.root / "index.html").write_text(self.html_prefix + '<script id="catalog-data" type="application/json">' + json.dumps(embedded, ensure_ascii=False) + '</script>' + self.html_suffix)
        header = {key: self.row[key] for key in ("id", "title", "publisher", "publisher_token")}
        header["text_chars"] = len(self.body)
        (self.sources / "1.txt").write_text(json.dumps(header, ensure_ascii=False) + '\n\n【完整可见正文开始】\n' + self.body + '\n【完整可见正文结束】\n链接附录不属于正文')
        self.baseline = self.root / "baseline.json"
        self.source_entry = {"source_sha256": self.row["source_sha256"], "content_sha256": self.row["content_sha256"],
                             "text_sha256": flow.digest(self.body), "text_chars": len(self.body)}
        flow.write_json(self.baseline, {"1": self.source_entry})
        self.prepare()
        self.make_results()

    def prepare(self, run="test-1"):
        return flow.prepare(self.root, self.sources, self.baseline, self.work, run, "2026-11-01", 20, 50)

    def make_results(self, summary="本文主张学习应关注实际理解，并通过及时反馈调整方法。", decision="rewrite"):
        item = flow.read_json(self.work / "inputs/1/article.json")
        self.item = item
        self.writer = {"id": "1", "writer": "long_writer_1", "model": flow.MODEL, "reasoning_effort": flow.EFFORT,
                       "source_sha256": item["source_sha256"], "body_sha256": item["body_sha256"], "old_summary_sha256": item["old_summary_sha256"],
                       "read_chunks": list(range(len(item["chunks"]))), "genre": "论说文", "decision": decision,
                       "decision_reason": "旧稿没有覆盖反馈机制", "summary": summary, "length_reason": "保留两个独立论点",
                       "major_points": [{"key": "P1", "point": "理解和反馈", "source_spans": [{"start": 0, "end": 20}],
                                         "old_coverage": "missing" if decision == "rewrite" else "covered", "old_coverage_reason": "比较完整句", "summary_evidence": summary}],
                       "issues": [{"problem": "遗漏反馈", "fix": "补充反馈"}] if decision == "rewrite" else []}
        self.review = {"id": "1", "reviewer": "long_reviewer_1", "model": flow.MODEL, "reasoning_effort": flow.EFFORT,
                       "source_sha256": item["source_sha256"], "body_sha256": item["body_sha256"], "summary_sha256": flow.digest(summary),
                       "read_chunks": self.writer["read_chunks"], "verdict": "pass",
                       "major_point_checks": [{"key": "P1", "verdict": "covered", "evidence": summary}],
                       "independent_major_points": [{"point": "独立阅读发现反馈主张", "source_spans": [{"start": 10, "end": 30}], "summary_evidence": summary}],
                       "additional_major_points": [], "notes": ["已独立核对两个主题"]}
        self.save_results()

    def save_results(self):
        flow.write_json(self.work / "writers/1.json", self.writer)
        flow.write_json(self.work / "reviewers/1.json", self.review)

    def output_bytes(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in (self.root / "data").glob("*.json")} | {"index.html": (self.root / "index.html").read_bytes()}

    def test_prepare_validates_all_inputs_before_writing(self):
        flow.write_json(self.baseline, {"1": {**self.source_entry, "source_sha256": "wrong"}})
        target = self.root / "bad-work"
        with self.assertRaisesRegex(ValueError, "source hash mismatch"):
            flow.prepare(self.root, self.sources, self.baseline, target, "bad", "2026-11-01", 20)
        self.assertFalse(target.exists())

    def test_body_tampering_same_length_is_rejected(self):
        chunk = self.work / self.item["chunks"][0]["path"]
        chunk.write_text("错" + chunk.read_text()[1:])
        with self.assertRaisesRegex(ValueError, "body hash"):
            flow.check(self.root, self.work)

    def test_prepare_rejects_same_length_snapshot_substitution(self):
        snapshot = self.sources / "1.txt"
        snapshot.write_text(snapshot.read_text().replace("第一节", "伪一节", 1))
        with self.assertRaisesRegex(ValueError, "trusted source manifest"):
            flow.prepare(self.root, self.sources, self.baseline, self.root / "substituted", "bad", "2026-11-01", 20)

    def test_missing_review_prevents_any_apply_write(self):
        (self.work / "reviewers/1.json").unlink()
        before = self.output_bytes()
        with self.assertRaisesRegex(ValueError, "Missing writer/reviewer"):
            flow.apply(self.root, self.work)
        self.assertEqual(before, self.output_bytes())

    def test_stale_old_summary_is_rejected(self):
        catalog = flow.read_json(self.root / "data/catalog.json")
        catalog["articles"][0]["summary"] = "另一次修改"
        flow.write_json(self.root / "data/catalog.json", catalog)
        with self.assertRaisesRegex(ValueError, "current summary changed"):
            flow.check(self.root, self.work)

    def test_changed_source_is_rejected(self):
        catalog = flow.read_json(self.root / "data/catalog.json")
        catalog["articles"][0]["source_sha256"] = "c" * 64
        flow.write_json(self.root / "data/catalog.json", catalog)
        with self.assertRaisesRegex(ValueError, "catalog source changed"):
            flow.check(self.root, self.work)

    def test_changed_source_link_is_rejected(self):
        catalog = flow.read_json(self.root / "data/catalog.json")
        catalog["articles"][0]["url"] = "https://example.com/changed"
        flow.write_json(self.root / "data/catalog.json", catalog)
        with self.assertRaisesRegex(ValueError, "catalog source changed"):
            flow.check(self.root, self.work)

    def test_wrong_actor_or_changed_assignment_is_rejected(self):
        self.writer["writer"] = "long_writer_2"
        self.save_results()
        with self.assertRaisesRegex(ValueError, "unexpected writer"):
            flow.check(self.root, self.work)
        self.writer["writer"] = "long_writer_1"
        self.save_results()
        flow.write_json(self.work / "writer-1-assignment.json", [])
        with self.assertRaisesRegex(ValueError, "assignment file changed"):
            flow.check(self.root, self.work)

    def test_duplicate_point_checks_and_scope_are_rejected(self):
        self.review["major_point_checks"].append(copy.deepcopy(self.review["major_point_checks"][0]))
        self.save_results()
        with self.assertRaisesRegex(ValueError, "duplicate point checks"):
            flow.check(self.root, self.work)
        manifest = flow.read_json(self.work / "manifest.json")
        manifest["articles"].append(copy.deepcopy(manifest["articles"][0]))
        flow.write_json(self.work / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "duplicate review scope"):
            flow.check(self.root, self.work)

    def test_incomplete_reading_cannot_pass(self):
        self.writer["read_chunks"] = self.writer["read_chunks"][:-1]
        self.save_results()
        with self.assertRaisesRegex(ValueError, "read_chunks"):
            flow.check(self.root, self.work)

    def test_same_writer_and_reviewer_are_rejected(self):
        self.review["reviewer"] = self.writer["writer"]
        self.save_results()
        with self.assertRaisesRegex(ValueError, "not independent"):
            flow.check(self.root, self.work)

    def test_review_must_match_current_draft(self):
        self.review["summary_sha256"] = "stale"
        self.save_results()
        with self.assertRaisesRegex(ValueError, "stale draft"):
            flow.check(self.root, self.work)

    def test_uncovered_independent_point_cannot_pass(self):
        self.review["additional_major_points"] = ["缺失的主要主题"]
        self.save_results()
        with self.assertRaisesRegex(ValueError, "remain uncovered"):
            flow.check(self.root, self.work)

    def test_paraphrased_evidence_is_rejected(self):
        self.review["independent_major_points"][0]["summary_evidence"] = "总结没有这句逐字表述"
        self.save_results()
        with self.assertRaisesRegex(ValueError, "absent"):
            flow.check(self.root, self.work)

    def test_token_evidence_and_tiny_source_span_are_rejected(self):
        evidence = self.writer["major_points"][0]["summary_evidence"]
        self.writer["major_points"][0]["summary_evidence"] = "学"
        self.save_results()
        with self.assertRaisesRegex(ValueError, "too short"):
            flow.check(self.root, self.work)
        self.writer["major_points"][0]["summary_evidence"] = evidence
        self.writer["major_points"][0]["source_spans"] = [{"start": 0, "end": 1}]
        self.save_results()
        with self.assertRaisesRegex(ValueError, "too short"):
            flow.check(self.root, self.work)

    def test_apply_is_idempotent_and_preserves_other_fields(self):
        result = flow.apply(self.root, self.work)
        first = self.output_bytes()
        self.assertEqual(result["rewritten"], 1)
        flow.apply(self.root, self.work)
        self.assertEqual(first, self.output_bytes())
        catalog = flow.read_json(self.root / "data/catalog.json")
        row = catalog["articles"][0]
        self.assertEqual(row["summary"], self.writer["summary"])
        for key in self.row:
            if key != "summary":
                self.assertEqual(row[key], self.row[key])
        html = (self.root / "index.html").read_text()
        self.assertTrue(html.startswith(self.html_prefix) and html.endswith(self.html_suffix))
        embedded = json.loads(flow.DATA_PATTERN.search(html)[2])
        self.assertEqual(embedded["records"], catalog["articles"])
        self.assertEqual(embedded["report"], catalog["metadata"])
        self.assertEqual(embedded["report"], flow.read_json(self.root / "data/validation.json"))
        self.assertEqual(embedded["answerIndex"], {"answer": {"id": "9"}})
        self.assertEqual(len(flow.load_ledger(self.root)["entries"]), 1)
        self.assertEqual(flow.load_ledger(self.root)["entries"][0]["review"], self.review)

    def test_retained_article_is_audited_without_new_revision(self):
        self.make_results(self.item["summary"], "retain")
        result = flow.apply(self.root, self.work)
        self.assertEqual(result["retained"], 1)
        self.assertEqual(flow.load_ledger(self.root)["entries"], [])
        audit = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertEqual(len(audit["entries"]), 1)

    def test_second_revision_and_import_replay_preserve_history(self):
        flow.apply(self.root, self.work)
        self.work = self.root / "work-2"
        self.prepare("test-2")
        self.make_results("新版还说明反馈应服务于实际理解，并据结果调整学习方法。")
        flow.apply(self.root, self.work)
        history = flow.load_ledger(self.root)["entries"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["before"]["summary"], history[0]["after"]["summary"])
        imported = self.root / "old-catalog.json"
        flow.write_json(imported, self.catalog)
        flow.apply(self.root, self.work, imported)
        self.assertEqual(flow.read_json(self.root / "data/catalog.json")["articles"][0]["summary"], self.writer["summary"])
        self.assertEqual(len(flow.load_ledger(self.root)["entries"]), 2)
        audit = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertEqual([run["run_id"] for run in audit["previous_runs"]], ["test-1"])
        self.assertEqual(audit["previous_runs"][0]["entries"][0]["review"]["reviewer"], "long_reviewer_1")

    def test_import_cannot_change_keywords_or_citations(self):
        flow.apply(self.root, self.work)
        bad = copy.deepcopy(self.catalog)
        bad["articles"][0]["keywords"] = ["未知标签"]
        imported = self.root / "bad-fields.json"
        flow.write_json(imported, bad)
        before = self.output_bytes()
        with self.assertRaisesRegex(ValueError, "outside summaries"):
            flow.apply(self.root, self.work, imported)
        self.assertEqual(before, self.output_bytes())

    def test_public_history_replay_does_not_need_private_work_directory(self):
        flow.apply(self.root, self.work)
        imported = self.root / "old-public-catalog.json"
        flow.write_json(imported, self.catalog)
        # sync_import only uses the catalog and committed review history.
        flow.sync_import(self.root, imported)
        self.assertEqual(flow.read_json(self.root / "data/catalog.json")["articles"][0]["summary"], self.writer["summary"])
        ledger = flow.load_ledger(self.root)
        ledger["entries"][0]["review"]["summary_sha256"] = "invalid"
        flow.write_json(self.root / "data/summary-revisions.json", ledger)
        before = self.output_bytes()
        with self.assertRaisesRegex(ValueError, "invalid historical review"):
            flow.sync_import(self.root, imported)
        self.assertEqual(before, self.output_bytes())

    def test_new_history_event_cannot_omit_review(self):
        flow.apply(self.root, self.work)
        ledger = flow.load_ledger(self.root)
        ledger["entries"][0].pop("review")
        flow.write_json(self.root / "data/summary-revisions.json", ledger)
        imported = self.root / "old-public-catalog.json"
        flow.write_json(imported, self.catalog)
        before = self.output_bytes()
        with self.assertRaisesRegex(ValueError, "historical review is missing"):
            flow.sync_import(self.root, imported)
        self.assertEqual(before, self.output_bytes())

    def test_returning_to_a_previous_wording_is_a_new_history_event(self):
        first_summary = self.writer["summary"]
        flow.apply(self.root, self.work)
        self.work = self.root / "cycle-2"
        self.prepare("cycle-2")
        self.make_results(self.row["summary"])
        flow.apply(self.root, self.work)
        self.work = self.root / "cycle-3"
        self.prepare("cycle-3")
        self.make_results(first_summary)
        flow.apply(self.root, self.work)
        events = flow.load_ledger(self.root)["entries"]
        self.assertEqual(len(events), 3)
        self.assertEqual(len({event["revision_id"] for event in events}), 3)
        self.assertEqual(events[-1]["after"]["summary"], first_summary)
        self.assertEqual(flow.read_json(self.root / "data/catalog.json")["articles"][0]["summary"], first_summary)

    def test_unknown_imported_summary_is_not_silently_overwritten(self):
        flow.apply(self.root, self.work)
        bad = copy.deepcopy(self.catalog)
        bad["articles"][0]["summary"] = "未经审核的另一份总结"
        imported = self.root / "bad-import.json"
        flow.write_json(imported, bad)
        before = self.output_bytes()
        with self.assertRaisesRegex(ValueError, "outside accepted history"):
            flow.apply(self.root, self.work, imported)
        self.assertEqual(before, self.output_bytes())

    def test_legacy_history_is_kept(self):
        legacy = {"id": "1", "source": {"source_sha256": self.row["source_sha256"], "content_sha256": self.row["content_sha256"]},
                  "before": {"summary": "最早的一版"}, "after": {"summary": self.row["summary"], "revision_date": "2026-10-31", "reviewer": "Codex"}}
        flow.write_json(self.root / "data/summary-revisions.json", {"schema_version": 1, "entries": [legacy]})
        flow.apply(self.root, self.work)
        self.assertEqual(flow.load_ledger(self.root)["entries"][0], legacy)


if __name__ == "__main__":
    unittest.main()
