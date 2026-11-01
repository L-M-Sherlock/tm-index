"""Regression checks for review gates, history replay and coordinated outputs."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

    def prepare_blind(self, work_name="blind-work", run="blind-run", reuse_verdict=None, omit_archive_url=False):
        reuse_path = None
        if reuse_verdict is not None:
            entry = {"id": self.row["id"], "title": self.row["title"], "url": self.row["url"],
                     "archive_url": self.row["archive_url"], "summary": self.row["summary"],
                     "summary_sha256": flow.digest(self.row["summary"]), "final_verdict": reuse_verdict,
                     "source": {"source_path": self.row["source_path"], "source_sha256": self.row["source_sha256"],
                                "content_sha256": self.row["content_sha256"], "body_sha256": flow.digest(self.body),
                                "source_method": "verified_snapshot", "read_chars": len(self.body)}}
            if omit_archive_url:
                entry.pop("archive_url")
            reuse_path = self.root / "data/summary-sampling-audit.json"
            flow.write_json(reuse_path, {"run_id": "prior-audit", "entries": [entry]})
        work = self.root / work_name
        flow.prepare(self.root, self.sources, self.baseline, work, run, "2026-11-01", 20,
                     max_body_chars=len(self.body) + 1, recovery_dir=None, reuse_audit=reuse_path,
                     actor_prefix="mid", blind_review=True)
        return work

    def prepare_multi_blind(self, count=3, work_name=".work/multi-blind", run="multi-blind", lengths=None):
        rows, baseline = [], {}
        for number in range(1, count + 1):
            article_id = str(number)
            target_length = lengths[number - 1] if lengths is not None else len(self.body)
            body = (self.body * ((target_length // len(self.body)) + 1))[:target_length]
            row = copy.deepcopy(self.row)
            row.update(id=article_id, title=f"多篇测试文章{article_id}",
                       url=f"https://example.com/{article_id}",
                       archive_url=f"https://example.com/archive/{article_id}",
                       source_path=f"article/{article_id}.json",
                       source_sha256=flow.digest("source-" + article_id),
                       content_sha256=flow.digest("content-" + article_id), read_chars=len(body))
            rows.append(row)
            header = {key: row[key] for key in ("id", "title", "publisher", "publisher_token")}
            header["text_chars"] = len(body)
            source_text = (json.dumps(header, ensure_ascii=False) + '\n\n【完整可见正文开始】\n'
                           + body + '\n【完整可见正文结束】\n链接附录不属于正文')
            (self.sources / f"{article_id}.txt").write_text(source_text, encoding="utf-8")
            baseline[article_id] = {"source_sha256": row["source_sha256"],
                                    "content_sha256": row["content_sha256"],
                                    "text_sha256": flow.digest(body), "text_chars": len(body)}
        catalog = {"metadata": {**self.catalog["metadata"], "included_articles": count}, "articles": rows}
        flow.write_json(self.root / "data/catalog.json", catalog)
        flow.write_json(self.root / "data/validation.json", catalog["metadata"])
        flow.write_json(self.baseline, baseline)
        html = (self.root / "index.html").read_text(encoding="utf-8")
        match = flow.DATA_PATTERN.search(html)
        embedded = json.loads(match[2])
        embedded["records"], embedded["report"] = rows, catalog["metadata"]
        payload = json.dumps(embedded, ensure_ascii=False)
        (self.root / "index.html").write_text(html[:match.start(2)] + payload + html[match.end(2):], encoding="utf-8")
        work = self.root / work_name
        minimum, maximum = (8000, 20000) if lengths is not None else (20, len(self.body) + 1)
        flow.prepare(self.root, self.sources, self.baseline, work, run, "2026-11-01", minimum,
                     max_body_chars=maximum, actor_prefix="mid", blind_review=True)
        return work

    def write_blind_records(self, work, writer_old="undercovered", reviewer_old="undercovered", reused=False,
                            with_resolution=False, summary_text=None, writer_actor="mid_writer_1",
                            writer_point_coverage=None, reviewer_point_coverage=None, article_id=None,
                            reviewer_actor="mid_reviewer_1"):
        manifest = flow.read_json(work / "manifest.json")
        item = next(item for item in manifest["articles"] if article_id is None or item["id"] == article_id)
        body = "".join((work / chunk["path"]).read_text(encoding="utf-8") for chunk in item["chunks"])
        writer_outline = {"id": item["id"], "actor": writer_actor, "model": flow.MODEL,
                          "reasoning_effort": flow.EFFORT, "source_sha256": item["source_sha256"],
                          "body_sha256": item["body_sha256"], "read_chunks": list(range(len(item["chunks"]))),
                          "genre": "论说文", "major_points": [{"key": "P1", "point": "理解与反馈的主要观点",
                              "source_spans": [{"start": 0, "end": 25, "quote": body[0:12]}]}]}
        reviewer_outline = {**writer_outline, "actor": reviewer_actor,
                            "major_points": [{"key": "R1", "point": "独立读到的反馈机制",
                                              "source_spans": [{"start": 25, "end": 55, "quote": body[30:42]}]}]}
        writer_outline_path = work / "outlines" / writer_actor / (item["id"] + ".json")
        reviewer_outline_path = work / "outlines" / reviewer_actor / (item["id"] + ".json")
        flow.write_json(writer_outline_path, writer_outline)
        flow.write_json(reviewer_outline_path, reviewer_outline)
        writer_initial_path = work / "initial" / (item["id"] + ".json")
        reviewer_initial_path = work / "reviewer-initial" / (item["id"] + ".json")
        writer_coverage = writer_point_coverage or ("covered" if writer_old == "adequate" else "missing")
        reviewer_coverage = reviewer_point_coverage or ("covered" if reviewer_old == "adequate" else "missing")
        writer_initial = {"id": item["id"], "actor": writer_actor, "model": flow.MODEL,
                          "reasoning_effort": flow.EFFORT, "source_sha256": item["source_sha256"],
                          "body_sha256": item["body_sha256"], "old_summary_sha256": item["old_summary_sha256"],
                          "outline_sha256": flow.file_hash(writer_outline_path), "read_chunks": writer_outline["read_chunks"],
                          "verdict": writer_old, "rationale": "依据旧稿覆盖的独立主要点作初判",
                          "major_points": [{**writer_outline["major_points"][0],
                              "old_coverage": writer_coverage,
                              "old_evidence": self.row["summary"] if writer_coverage != "missing" else None}], "distortions": []}
        reviewer_initial = {"id": item["id"], "actor": reviewer_actor, "model": flow.MODEL,
                            "reasoning_effort": flow.EFFORT, "source_sha256": item["source_sha256"],
                            "body_sha256": item["body_sha256"], "old_summary_sha256": item["old_summary_sha256"],
                            "outline_sha256": flow.file_hash(reviewer_outline_path), "read_chunks": reviewer_outline["read_chunks"],
                            "verdict": reviewer_old, "rationale": "独立比较旧稿与正文",
                            "major_points": [{**reviewer_outline["major_points"][0],
                                "old_coverage": reviewer_coverage,
                                "old_evidence": self.row["summary"] if reviewer_coverage != "missing" else None}], "distortions": []}
        if not reused:
            flow.write_json(writer_initial_path, writer_initial)
            flow.write_json(reviewer_initial_path, reviewer_initial)
        if with_resolution:
            request = {"id": item["id"], "writer_initial_sha256": flow.file_hash(writer_initial_path),
                       "reviewer_initial_sha256": flow.file_hash(reviewer_initial_path), "issue": "是否覆盖反馈机制",
                       "source_spans": [{"start": 0, "end": 25}], "rationale": "双方初判分类不同"}
            flow.write_json(work / "requests" / (item["id"] + ".json"), request)
            adjudication = {"id": item["id"], "decider": "Codex主代理",
                            "writer_initial_sha256": request["writer_initial_sha256"],
                            "reviewer_initial_sha256": request["reviewer_initial_sha256"],
                            "final_old_verdict": "undercovered", "rationale": "回查正文后确认是主要遗漏",
                            "source_spans": [{"start": 0, "end": 25, "quote": body[0:12]}],
                            "excluded_writer_points": [], "excluded_reviewer_points": []}
            flow.write_json(work / "adjudications" / (item["id"] + ".json"), adjudication)
        summary = summary_text or "本文主张学习应关注实际理解，并通过及时反馈调整方法。"
        writer = {"id": item["id"], "writer": writer_actor, "model": flow.MODEL, "reasoning_effort": flow.EFFORT,
                  "source_sha256": item["source_sha256"], "body_sha256": item["body_sha256"],
                  "old_summary_sha256": item["old_summary_sha256"], "read_chunks": writer_outline["read_chunks"],
                  "genre": "论说文", "decision": "rewrite", "decision_reason": "旧稿漏掉反馈机制",
                  "summary": summary, "length_reason": "概括中心机制",
                  "major_points": [{**writer_outline["major_points"][0],
                                    "old_coverage": writer_coverage,
                                    "old_coverage_reason": "初判旧稿覆盖情况",
                                    "summary_evidence": summary}],
                  "issues": [{"problem": "旧稿未说明反馈机制", "fix": "补入反馈与学习路径的关系"}],
                  "blind_outline_sha256": flow.file_hash(writer_outline_path)}
        review = {"id": item["id"], "reviewer": reviewer_actor, "model": flow.MODEL,
                  "reasoning_effort": flow.EFFORT, "source_sha256": item["source_sha256"],
                  "body_sha256": item["body_sha256"], "summary_sha256": flow.digest(summary),
                  "read_chunks": reviewer_outline["read_chunks"], "verdict": "pass",
                  "major_point_checks": [{"key": "P1", "verdict": "covered", "evidence": summary}],
                  "independent_major_points": [{"blind_key": "R1", "point": reviewer_outline["major_points"][0]["point"],
                                                "source_spans": reviewer_outline["major_points"][0]["source_spans"],
                                                "summary_evidence": summary}],
                  "additional_major_points": [], "notes": ["已核对两份盲读提纲中的主点"],
                  "blind_outline_sha256": flow.file_hash(reviewer_outline_path)}
        if not reused:
            writer["initial_assessment_sha256"] = flow.file_hash(writer_initial_path)
            review["initial_assessment_sha256"] = flow.file_hash(reviewer_initial_path)
        flow.write_json(work / "writers" / (item["id"] + ".json"), writer)
        flow.write_json(work / "reviewers" / (item["id"] + ".json"), review)
        return item

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

    def test_extended_prepare_uses_left_closed_right_open_bounds(self):
        length = len(self.body)
        target = self.root / "bounded"
        flow.prepare(self.root, self.sources, self.baseline, target, "bounded", "2026-11-01", length,
                     max_body_chars=length + 1, actor_prefix="mid", blind_review=True)
        manifest = flow.read_json(target / "manifest.json")
        self.assertEqual([item["id"] for item in manifest["articles"]], ["1"])
        self.assertEqual(manifest["max_body_chars"], length + 1)
        rejected = self.root / "empty-bound"
        with self.assertRaisesRegex(ValueError, "No articles"):
            flow.prepare(self.root, self.sources, self.baseline, rejected, "empty", "2026-11-01", 20,
                         max_body_chars=length, actor_prefix="mid", blind_review=True)
        self.assertFalse(rejected.exists())

    def test_extended_prepare_recovers_exact_raw_html(self):
        html = "<p>测试正文</p><script>hidden</script><div> 后续内容 </div>"
        parser = flow.summary_sources.VisibleStrings()
        parser.feed(html)
        parser.close()
        body = "\n".join(parser.parts)
        raw_dir = self.root / "raw"
        raw_dir.mkdir()
        raw_bytes = json.dumps({"content": html}, ensure_ascii=False).encode("utf-8")
        raw_row = {**self.row, "id": "raw", "title": "原始恢复", "read_chars": len(body),
                   "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                   "content_sha256": flow.digest(html)}
        (raw_dir / "raw.json").write_bytes(raw_bytes)
        trusted = self.root / "raw-trusted.json"
        flow.write_json(trusted, {"raw": {"source_sha256": raw_row["source_sha256"],
                                           "content_sha256": raw_row["content_sha256"],
                                           "text_sha256": flow.digest(body), "text_chars": len(body)}})
        flow.write_json(self.root / "data/catalog.json", {"metadata": {"included_articles": 1}, "articles": [raw_row]})
        target = self.root / "raw-work"
        flow.prepare(self.root, self.sources, trusted, target, "raw", "2026-11-01", 1,
                     recovery_dir=raw_dir, actor_prefix="mid", blind_review=True)
        item = flow.read_json(target / "manifest.json")["articles"][0]
        self.assertEqual(item["source_method"], "exact_raw_snapshot_recovery")
        self.assertEqual("".join((target / chunk["path"]).read_text() for chunk in item["chunks"]), body)

    def test_reuse_audit_direct_retain_pins_entry_and_skips_assignment(self):
        entry = {"id": self.row["id"], "title": self.row["title"], "url": self.row["url"],
                 "archive_url": self.row["archive_url"], "summary": self.row["summary"],
                 "summary_sha256": flow.digest(self.row["summary"]), "final_verdict": "adequate",
                 "source": {"source_path": self.row["source_path"], "source_sha256": self.row["source_sha256"],
                            "content_sha256": self.row["content_sha256"], "body_sha256": flow.digest(self.body),
                            "source_method": "verified_snapshot", "read_chars": len(self.body)}}
        audit_path = self.root / "data/summary-sampling-audit.json"
        flow.write_json(audit_path, {"run_id": "prior-audit", "entries": [entry]})
        pinned_report = audit_path.read_bytes()
        target = self.root / "reuse-work"
        flow.prepare(self.root, self.sources, self.baseline, target, "reuse", "2026-11-01", 20,
                     max_body_chars=len(self.body) + 1, reuse_audit=audit_path, actor_prefix="mid", blind_review=True)
        manifest = flow.read_json(target / "manifest.json")
        item = manifest["articles"][0]
        self.assertEqual(item["review_origin"], "sampling_reuse_adequate")
        self.assertNotIn(item["id"], [id for group in manifest["assignments"]["writer"].values() for id in group])
        self.assertEqual(flow.read_json(target / item["prior_review_path"]), entry)
        source = flow.read_json(target / "inputs/1/source.json")
        self.assertNotIn("summary", source)
        self.assertNotIn("final_verdict", source)
        self.assertEqual(manifest["preserve_files"]["data/summary-sampling-audit.json"], flow.file_hash(audit_path))
        flow.apply(self.root, target)
        self.assertEqual(audit_path.read_bytes(), pinned_report)

    def test_reuse_problem_is_assigned_and_fingerprint_mismatch_is_invalidated(self):
        entry = {"id": self.row["id"], "title": self.row["title"], "url": self.row["url"],
                 "archive_url": self.row["archive_url"], "summary": self.row["summary"],
                 "summary_sha256": flow.digest(self.row["summary"]), "final_verdict": "undercovered",
                 "source": {"source_path": self.row["source_path"], "source_sha256": self.row["source_sha256"],
                            "content_sha256": self.row["content_sha256"], "body_sha256": flow.digest(self.body),
                            "source_method": "verified_snapshot", "read_chars": len(self.body)}}
        audit_path = self.root / "prior-audit.json"
        flow.write_json(audit_path, {"run_id": "prior-audit", "entries": [entry]})
        target = self.root / "problem-work"
        flow.prepare(self.root, self.sources, self.baseline, target, "problem", "2026-11-01", 20,
                     max_body_chars=len(self.body) + 1, reuse_audit=audit_path, actor_prefix="mid", blind_review=True)
        manifest = flow.read_json(target / "manifest.json")
        item = manifest["articles"][0]
        self.assertEqual(item["review_origin"], "sampling_reuse_problem")
        self.assertEqual([id for group in manifest["assignments"]["writer"].values() for id in group], ["1"])
        bad = copy.deepcopy(entry)
        bad["summary_sha256"] = "0" * 64
        flow.write_json(audit_path, {"run_id": "prior-audit", "entries": [bad]})
        invalidated = self.root / "invalidated-work"
        flow.prepare(self.root, self.sources, self.baseline, invalidated, "invalidated", "2026-11-01", 20,
                     max_body_chars=len(self.body) + 1, reuse_audit=audit_path, actor_prefix="mid", blind_review=True)
        fresh_manifest = flow.read_json(invalidated / "manifest.json")
        self.assertEqual(fresh_manifest["articles"][0]["review_origin"], "fresh")
        self.assertTrue(fresh_manifest["reuse_invalidations"][0]["reasons"])

    def test_blind_review_rejects_quote_outside_declared_span(self):
        work = self.prepare_blind(work_name="bad-quote", run="bad-quote")
        item = flow.read_json(work / "manifest.json")["articles"][0]
        actor = "mid_writer_1"
        outline = {"id": item["id"], "actor": actor, "model": flow.MODEL, "reasoning_effort": flow.EFFORT,
                   "source_sha256": item["source_sha256"], "body_sha256": item["body_sha256"],
                   "read_chunks": list(range(len(item["chunks"]))), "genre": "论说文",
                   "major_points": [{"key": "P1", "point": "错误坐标示例",
                                     "source_spans": [{"start": 0, "end": 8, "quote": self.body[30:45]}]}]}
        flow.write_json(work / "outlines" / actor / (item["id"] + ".json"), outline)
        with self.assertRaisesRegex(ValueError, "quote is outside"):
            flow.check(self.root, work, allow_incomplete=True)

    def test_blind_reviewer_cannot_map_own_point_to_writer_anchor(self):
        work = self.prepare_blind(work_name="wrong-review-anchor", run="wrong-review-anchor")
        self.write_blind_records(work)
        item = flow.read_json(work / "manifest.json")["articles"][0]
        path = work / "reviewers" / (item["id"] + ".json")
        review = flow.read_json(path)
        writer_point = flow.read_json(work / "outlines/mid_writer_1" / (item["id"] + ".json"))["major_points"][0]
        review["independent_major_points"][0]["source_spans"] = writer_point["source_spans"]
        flow.write_json(path, review)
        with self.assertRaisesRegex(ValueError, "reviewer blind point no longer uses its own source anchors"):
            flow.check(self.root, work, allow_incomplete=True)

    def test_blind_check_lists_missing_stages_without_crashing(self):
        work = self.prepare_blind(work_name="pending-work", run="pending-work")
        manifest, pairs, pending = flow.check(self.root, work, allow_incomplete=True)
        item_id = manifest["articles"][0]["id"]
        self.assertEqual(pairs, [])
        self.assertIn(f"{item_id}:writer-outline", pending)
        self.assertIn(f"{item_id}:writer-initial", pending)
        self.assertIn(f"{item_id}:draft", pending)
        self.assertIn(f"{item_id}:review", pending)
        with self.assertRaisesRegex(ValueError, "Missing initial/draft/review/adjudication"):
            flow.check(self.root, work)

    def test_blind_disagreement_waits_for_root_adjudication(self):
        work = self.prepare_blind(work_name="disagreement-work", run="disagreement-work")
        self.write_blind_records(work, writer_old="undercovered", reviewer_old="adequate")
        manifest, pairs, pending = flow.check(self.root, work, allow_incomplete=True)
        item_id = manifest["articles"][0]["id"]
        self.assertEqual(pairs, [])
        self.assertIn(f"{item_id}:request", pending)
        self.assertIn(f"{item_id}:adjudication", pending)
        with self.assertRaisesRegex(ValueError, "Missing initial/draft/review/adjudication"):
            flow.check(self.root, work)
        self.write_blind_records(work, writer_old="undercovered", reviewer_old="adequate", with_resolution=True)
        _, pairs, pending = flow.check(self.root, work)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)

    def test_blind_direct_retain_uses_prior_record_without_fake_receipts(self):
        work = self.prepare_blind(work_name="direct-work", run="direct-work", reuse_verdict="adequate")
        manifest, pairs, pending = flow.check(self.root, work)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)
        self.assertIsNone(pairs[0][1])
        self.assertIsNone(pairs[0][2])
        primary = {"decider": "Codex主代理", "entries": [{"id": manifest["articles"][0]["id"],
                   "accepted": True, "summary_sha256": flow.digest(manifest["articles"][0]["summary"]),
                   "review_sha256": "historical-review-hash-may-differ"}]}
        flow.write_json(work / "primary-acceptance.json", primary)
        result = flow.apply(self.root, work)
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(result["reused_direct_retains"], 1)
        audit = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertIsNone(audit["entries"][0]["writer"])
        self.assertIsNone(audit["entries"][0]["review"])
        self.assertEqual(audit["primary_acceptance"], primary)
        before = self.output_bytes()
        flow.apply(self.root, work)
        self.assertEqual(before, self.output_bytes())
        item_id = manifest["articles"][0]["id"]
        flow.write_json(work / "writers" / (item_id + ".json"), {"id": item_id})
        with self.assertRaisesRegex(ValueError, "direct retain has fake new review records"):
            flow.check(self.root, work)

    def test_blind_reused_problem_requires_rewrite_without_fake_initials(self):
        work = self.prepare_blind(work_name="reused-problem-work", run="reused-problem-work", reuse_verdict="undercovered")
        self.write_blind_records(work, reused=True)
        manifest, pairs, pending = flow.check(self.root, work)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(manifest["articles"][0]["review_origin"], "sampling_reuse_problem")
        self.assertFalse((work / "initial" / (manifest["articles"][0]["id"] + ".json")).exists())
        self.assertEqual(flow.apply(self.root, work)["reused_old_judgments"], 1)

    def test_single_initial_validator_rejects_reused_problem_origin(self):
        work = self.prepare_blind(work_name="reused-initial-call", run="reused-initial-call",
                                  reuse_verdict="undercovered")
        self.write_blind_records(work, reused=True)
        item = flow.read_json(work / "manifest.json")["articles"][0]
        actor = "mid_writer_1"
        outline_path = work / "outlines" / actor / (item["id"] + ".json")
        outline = flow.read_json(outline_path)
        valid_but_fabricated = {"id": item["id"], "actor": actor, "model": flow.MODEL,
            "reasoning_effort": flow.EFFORT, "source_sha256": item["source_sha256"],
            "body_sha256": item["body_sha256"], "old_summary_sha256": item["old_summary_sha256"],
            "outline_sha256": flow.file_hash(outline_path), "read_chunks": outline["read_chunks"],
            "verdict": "undercovered", "rationale": "表面上完整的独立初判",
            "major_points": [{**outline["major_points"][0], "old_coverage": "missing", "old_evidence": None}],
            "distortions": []}
        body = flow.validate_body(work, item, require_source=True)
        with self.assertRaisesRegex(ValueError, "only valid for fresh items"):
            flow.validate_initial(valid_but_fabricated, outline, outline_path, actor, item, body)

    def test_reuse_accepts_legacy_audit_entry_without_archive_url(self):
        work = self.prepare_blind(work_name="legacy-reuse-work", run="legacy-reuse-work",
                                  reuse_verdict="adequate", omit_archive_url=True)
        manifest = flow.read_json(work / "manifest.json")
        item = manifest["articles"][0]
        self.assertEqual(item["review_origin"], "sampling_reuse_adequate")
        self.assertNotIn("reuse_invalidations", manifest)
        self.assertEqual(flow.read_json(work / item["prior_review_path"])["final_verdict"], "adequate")

    def test_missing_prior_ids_are_fresh_without_invalidation_records(self):
        audit_path = self.root / "data/summary-sampling-audit.json"
        flow.write_json(audit_path, {"run_id": "prior-audit", "entries": []})
        work = self.root / "missing-prior-work"
        flow.prepare(self.root, self.sources, self.baseline, work, "missing-prior", "2026-11-01", 20,
                     max_body_chars=len(self.body) + 1, reuse_audit=audit_path,
                     actor_prefix="mid", blind_review=True)
        manifest = flow.read_json(work / "manifest.json")
        self.assertEqual(manifest["articles"][0]["review_origin"], "fresh")
        self.assertNotIn("reuse_invalidations", manifest)

    def test_blind_writer_must_keep_its_frozen_points_and_initial_hash(self):
        work = self.prepare_blind(work_name="frozen-writer-work", run="frozen-writer-work")
        self.write_blind_records(work)
        item = flow.read_json(work / "manifest.json")["articles"][0]
        writer_path = work / "writers" / (item["id"] + ".json")
        writer = flow.read_json(writer_path)
        writer["major_points"][0]["point"] = "改写后的提纲点"
        flow.write_json(writer_path, writer)
        with self.assertRaisesRegex(ValueError, "writer changed blind major point"):
            flow.check(self.root, work)
        self.write_blind_records(work)
        initial_path = work / "initial" / (item["id"] + ".json")
        initial = flow.read_json(initial_path)
        initial["rationale"] += "补写"
        flow.write_json(initial_path, initial)
        with self.assertRaisesRegex(ValueError, "stale initial assessment"):
            flow.check(self.root, work)

    def test_root_can_correct_only_writer_coverage_with_old_summary_evidence(self):
        def make_case(name, correction):
            work = self.prepare_blind(work_name=name, run=name)
            item = self.write_blind_records(work, writer_old="undercovered", reviewer_old="undercovered",
                                            with_resolution=True, writer_point_coverage="partial",
                                            reviewer_point_coverage="partial")
            writer_path = work / "writers" / (item["id"] + ".json")
            writer = flow.read_json(writer_path)
            writer["major_points"][0]["old_coverage"] = "covered"
            writer["major_points"][0]["old_coverage_reason"] = "根据根裁定修正旧稿覆盖判断。"
            flow.write_json(writer_path, writer)
            adju_path = work / "adjudications" / (item["id"] + ".json")
            adju = flow.read_json(adju_path)
            adju["writer_coverage_corrections"] = [correction]
            flow.write_json(adju_path, adju)
            return work, item

        good = {"key": "P1", "old_coverage": "covered", "reason": "旧稿已有该主要观点的实质论述。",
                "old_summary_evidence": self.row["summary"]}
        work, item = make_case("authorized-coverage-correction", good)
        _, pairs, pending = flow.check(self.root, work)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)

        wrong_key = {**good, "key": "P99"}
        work, _ = make_case("invalid-coverage-correction-key", wrong_key)
        with self.assertRaisesRegex(ValueError, "key is not in writer blind outline"):
            flow.check(self.root, work)

        fake_evidence = {**good, "old_summary_evidence": "这段旧稿从未包含的虚构证据内容"}
        work, _ = make_case("invalid-coverage-correction-evidence", fake_evidence)
        with self.assertRaisesRegex(ValueError, "evidence is absent"):
            flow.check(self.root, work)

        unapproved = self.prepare_blind(work_name="unapproved-coverage-correction", run="unapproved-coverage-correction")
        item = self.write_blind_records(unapproved, writer_old="undercovered", reviewer_old="undercovered",
                                         with_resolution=True, writer_point_coverage="partial",
                                         reviewer_point_coverage="partial")
        writer_path = unapproved / "writers" / (item["id"] + ".json")
        writer = flow.read_json(writer_path)
        writer["major_points"][0]["old_coverage"] = "covered"
        flow.write_json(writer_path, writer)
        with self.assertRaisesRegex(ValueError, "writer changed initial old coverage"):
            flow.check(self.root, unapproved)

    def test_root_coverage_correction_can_relabel_covered_as_partial_or_missing(self):
        def prepare_partial(work_name, corrected_coverage, evidence):
            work = self.prepare_blind(work_name=work_name, run=work_name)
            item = self.write_blind_records(work, writer_old="adequate", reviewer_old="adequate",
                                            with_resolution=True, writer_point_coverage="covered",
                                            reviewer_point_coverage="covered")
            writer_path = work / "writers" / (item["id"] + ".json")
            writer = flow.read_json(writer_path)
            writer["major_points"][0]["old_coverage"] = corrected_coverage
            writer["major_points"][0]["old_coverage_reason"] = "按主代理逐点裁定修正旧稿覆盖分类。"
            flow.write_json(writer_path, writer)
            adjudication_path = work / "adjudications" / (item["id"] + ".json")
            adjudication = flow.read_json(adjudication_path)
            adjudication["writer_coverage_corrections"] = [{"key": "P1", "old_coverage": corrected_coverage,
                "reason": "复核认为此点实际覆盖程度应这样分类。", "old_summary_evidence": evidence}]
            flow.write_json(adjudication_path, adjudication)
            return work

        partial_work = prepare_partial("covered-to-partial-authorized", "partial", self.row["summary"])
        _, pairs, pending = flow.check(self.root, partial_work)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)

        missing_work = prepare_partial("covered-to-missing-authorized", "missing", None)
        _, pairs, pending = flow.check(self.root, missing_work)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)

        distorted_work = prepare_partial("covered-to-distorted-authorized", "distorted", self.row["summary"])
        _, pairs, pending = flow.check(self.root, distorted_work)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)

        bad_missing_work = prepare_partial("covered-to-missing-fabricated-evidence", "missing", self.row["summary"])
        with self.assertRaisesRegex(ValueError, "missing corrected point must have null"):
            flow.check(self.root, bad_missing_work)

    def test_blind_apply_archived_run_is_blocked_but_check_remains_valid(self):
        work1 = self.prepare_blind(work_name="archive-run-1", run="archive-run-1")
        self.write_blind_records(work1, summary_text="第一轮总结：及时反馈有助于形成自主学习方法。")
        flow.apply(self.root, work1)
        work2 = self.root / "archive-run-2"
        flow.prepare(self.root, self.sources, self.baseline, work2, "archive-run-2", "2026-11-02", 20,
                     max_body_chars=len(self.body) + 1, actor_prefix="mid", blind_review=True)
        self.write_blind_records(work2, summary_text="第二轮总结：学习路径应随反馈调整并重视理解。")
        flow.apply(self.root, work2)
        second_apply_bytes = self.output_bytes()
        flow.apply(self.root, work2)
        self.assertEqual(second_apply_bytes, self.output_bytes())
        audit = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertEqual([run["run_id"] for run in audit["previous_runs"]], ["archive-run-1"])
        _, pairs, pending = flow.check(self.root, work1)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)
        before = self.output_bytes()
        with self.assertRaisesRegex(ValueError, "archived review run"):
            flow.apply(self.root, work1)
        self.assertEqual(before, self.output_bytes())
        imported = self.root / "old-catalog-for-blind.json"
        flow.write_json(imported, self.catalog)
        flow.sync_import(self.root, imported)
        synchronized = self.output_bytes()
        flow.sync_import(self.root, imported)
        self.assertEqual(synchronized, self.output_bytes())
        self.assertEqual(flow.read_json(self.root / "data/catalog.json")["articles"][0]["summary"],
                         "第二轮总结：学习路径应随反馈调整并重视理解。")

    def test_checkpoint_records_are_content_addressed_idempotent_and_append_versions(self):
        work = self.prepare_blind(work_name=".work/checkpoint-history", run="checkpoint-history")
        self.write_blind_records(work)
        first = flow.checkpoint_records(self.root, work)
        self.assertEqual(first, {"records": 6, "new_versions": 6, "unchanged": 0})
        item = flow.read_json(work / "manifest.json")["articles"][0]
        writer_path = work / "writers" / (item["id"] + ".json")
        index_path = work / "checkpoints/index/mid_writer_1/writer/1.json"
        index_before = index_path.read_bytes()
        index = json.loads(index_before)
        self.assertEqual(index["first_sha256"], flow.file_hash(writer_path))
        self.assertIn("does not prove prior blind-reading order", index["ordering_note"])
        snapshot = work / index["versions"][0]["snapshot_path"]
        first_snapshot_bytes = snapshot.read_bytes()
        self.assertEqual(first_snapshot_bytes, writer_path.read_bytes())

        second = flow.checkpoint_records(self.root, work)
        self.assertEqual(second, {"records": 6, "new_versions": 0, "unchanged": 6})
        self.assertEqual(index_path.read_bytes(), index_before)

        writer = flow.read_json(writer_path)
        writer["summary"] = "更新版本仍明确论述反馈与理解。"
        writer["major_points"][0]["summary_evidence"] = writer["summary"]
        flow.write_json(writer_path, writer)
        review_path = work / "reviewers" / (item["id"] + ".json")
        review = flow.read_json(review_path)
        review["summary_sha256"] = flow.digest(writer["summary"])
        review["major_point_checks"][0]["evidence"] = writer["summary"]
        review["independent_major_points"][0]["summary_evidence"] = writer["summary"]
        flow.write_json(review_path, review)
        third = flow.checkpoint_records(self.root, work)
        self.assertEqual(third, {"records": 6, "new_versions": 2, "unchanged": 4})
        final_index = flow.read_json(index_path)
        self.assertEqual(final_index["first_sha256"], index["first_sha256"])
        self.assertEqual(final_index["first_observed_at"], index["first_observed_at"])
        self.assertEqual(len(final_index["versions"]), 2)
        self.assertEqual(snapshot.read_bytes(), first_snapshot_bytes)
        self.assertEqual(flow.checkpoint_records(self.root, work)["new_versions"], 0)

    def test_checkpoint_records_can_filter_to_one_actor_and_article_without_reading_other_agents(self):
        work = self.prepare_blind(work_name=".work/checkpoint-filtered", run="checkpoint-filtered")
        item = self.write_blind_records(work)
        reviewer_outline = work / "outlines/mid_reviewer_1" / (item["id"] + ".json")
        reviewer_outline.write_text("not valid json", encoding="utf-8")
        reviewer_log = work / "format-corrections/mid_reviewer_1.json"
        reviewer_log.parent.mkdir(parents=True, exist_ok=True)
        reviewer_log.write_text("not valid json", encoding="utf-8")

        result = flow.checkpoint_records(self.root, work, actor="mid_writer_1", article_id=item["id"])

        self.assertEqual(result, {"records": 3, "new_versions": 3, "unchanged": 0})
        self.assertTrue((work / "checkpoints/index/mid_writer_1/outline/1.json").exists())
        self.assertTrue((work / "checkpoints/index/mid_writer_1/initial/1.json").exists())
        self.assertTrue((work / "checkpoints/index/mid_writer_1/writer/1.json").exists())
        self.assertFalse((work / "checkpoints/index/mid_reviewer_1/outline/1.json").exists())
        self.assertFalse((work / "checkpoints/index/mid_reviewer_1/format-corrections/run.json").exists())

    def test_checkpoint_records_article_filter_captures_only_that_article_stages(self):
        work = self.prepare_blind(work_name=".work/checkpoint-article-filter", run="checkpoint-article-filter")
        item = self.write_blind_records(work)

        result = flow.checkpoint_records(self.root, work, article_id=item["id"])

        self.assertEqual(result, {"records": 6, "new_versions": 6, "unchanged": 0})
        self.assertEqual(len(list((work / "checkpoints/index").glob("*/*/*.json"))), 6)

    def test_checkpoint_records_reject_unknown_actor_id_and_wrong_actor_article_pair(self):
        work = self.prepare_blind(work_name=".work/checkpoint-invalid-filter", run="checkpoint-invalid-filter")
        item = self.write_blind_records(work)
        manifest_path = work / "manifest.json"
        manifest = flow.read_json(manifest_path)
        manifest["assignments"]["writer"]["mid_writer_2"] = []
        flow.write_json(manifest_path, manifest)

        with self.assertRaisesRegex(ValueError, "Unknown checkpoint actor"):
            flow.checkpoint_records(self.root, work, actor="not-an-actor")
        with self.assertRaisesRegex(ValueError, "Unknown checkpoint article ID"):
            flow.checkpoint_records(self.root, work, article_id="missing-id")
        with self.assertRaisesRegex(ValueError, "is not assigned article"):
            flow.checkpoint_records(self.root, work, actor="mid_writer_2", article_id=item["id"])

    def test_checkpoint_blocks_paired_frozen_outline_and_initial_edits_without_format_chain(self):
        work = self.prepare_blind(work_name=".work/checkpoint-paired-change", run="checkpoint-paired-change")
        self.write_blind_records(work)
        flow.checkpoint_records(self.root, work)
        item = flow.read_json(work / "manifest.json")["articles"][0]
        outline_path = work / "outlines/mid_writer_1" / (item["id"] + ".json")
        outline = flow.read_json(outline_path)
        outline["major_points"][0]["point"] = "一起修改后的point"
        flow.write_json(outline_path, outline)
        initial_path = work / "initial" / (item["id"] + ".json")
        initial = flow.read_json(initial_path)
        initial["outline_sha256"] = flow.file_hash(outline_path)
        initial["major_points"][0]["point"] = "一起修改后的point"
        flow.write_json(initial_path, initial)
        writer_path = work / "writers" / (item["id"] + ".json")
        writer = flow.read_json(writer_path)
        writer["blind_outline_sha256"] = flow.file_hash(outline_path)
        writer["initial_assessment_sha256"] = flow.file_hash(initial_path)
        writer["major_points"][0]["point"] = "一起修改后的point"
        flow.write_json(writer_path, writer)
        with self.assertRaisesRegex(ValueError, "Frozen checkpoint changed without a valid format chain"):
            flow.check(self.root, work, allow_incomplete=True)

    def test_checkpoint_records_refuse_public_paths(self):
        with self.assertRaisesRegex(ValueError, "private .work"):
            flow.checkpoint_records(self.root, self.work)

    def test_format_correction_logs_preserve_blind_semantics_and_allow_later_draft_revision(self):
        work = self.prepare_blind(work_name=".work/format-log-work", run="format-log-work")
        item = self.write_blind_records(work)
        flow.checkpoint_records(self.root, work)
        writer_actor = "mid_writer_1"
        outline_path = work / "outlines" / writer_actor / (item["id"] + ".json")
        initial_path = work / "initial" / (item["id"] + ".json")
        writer_path = work / "writers" / (item["id"] + ".json")
        original_paths = [outline_path, initial_path, writer_path]
        labels = ["outline.json", "initial.json", "writer.json"]
        records = []
        before_objects = [flow.read_json(path) for path in original_paths]
        backup_dir = work / "format-before" / writer_actor / item["id"]
        for path, label in zip(original_paths, labels):
            backup = backup_dir / label
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(path.read_bytes())
            records.append({"id": item["id"], "path": path.relative_to(work).as_posix(),
                            "backup_path": backup.relative_to(work).as_posix(),
                            "before_sha256": flow.file_hash(backup), "changes": ["Quote-coordinate format correction"]})
        quote_span = {"start": 1, "end": 25, "quote": self.body[1:13]}
        outline = before_objects[0]
        outline["major_points"][0]["source_spans"] = [quote_span]
        flow.write_json(outline_path, outline)
        initial = before_objects[1]
        initial["outline_sha256"] = flow.file_hash(outline_path)
        initial["major_points"][0]["source_spans"] = [quote_span]
        flow.write_json(initial_path, initial)
        writer = before_objects[2]
        writer["blind_outline_sha256"] = flow.file_hash(outline_path)
        writer["initial_assessment_sha256"] = flow.file_hash(initial_path)
        writer["major_points"][0]["source_spans"] = [quote_span]
        flow.write_json(writer_path, writer)
        for record, path in zip(records, original_paths):
            record["after_sha256"] = flow.file_hash(path)
        log = {"actor": writer_actor, "reason": "Coordinates and exact source quotes only.", "files": records}
        flow.write_json(work / "format-corrections" / (writer_actor + ".json"), log)

        # A later valid draft edit may make the logged intermediate after hash stale.
        writer["summary"] = "新版总结明确关联理解与及时反馈。"
        writer["major_points"][0]["summary_evidence"] = writer["summary"]
        flow.write_json(writer_path, writer)
        review_path = work / "reviewers" / (item["id"] + ".json")
        review = flow.read_json(review_path)
        review["summary_sha256"] = flow.digest(writer["summary"])
        review["major_point_checks"][0]["evidence"] = writer["summary"]
        review["independent_major_points"][0]["summary_evidence"] = writer["summary"]
        flow.write_json(review_path, review)

        flow.check(self.root, work)
        flow.apply(self.root, work)
        audit = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertEqual(audit["format_corrections"][0]["path"], "format-corrections/mid_writer_1.json")
        self.assertTrue(audit["format_corrections"][0]["log"]["files"][0]["backup_path"].startswith("format-before/"))

    def test_format_correction_cannot_change_blind_point_semantics(self):
        work = self.prepare_blind(work_name="bad-format-log", run="bad-format-log")
        item = self.write_blind_records(work)
        actor = "mid_writer_1"
        path = work / "outlines" / actor / (item["id"] + ".json")
        backup = work / "format-before" / actor / item["id"] / "outline.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(path.read_bytes())
        outline = flow.read_json(path)
        outline["major_points"][0]["point"] = "改掉原来的盲读判断"
        flow.write_json(path, outline)
        flow.write_json(work / "format-corrections" / (actor + ".json"), {
            "actor": actor, "reason": "伪装格式修复", "files": [{"id": item["id"],
                "path": path.relative_to(work).as_posix(), "backup_path": backup.relative_to(work).as_posix(),
                "before_sha256": flow.file_hash(backup), "after_sha256": flow.file_hash(path), "changes": ["point changed"]}]})
        with self.assertRaisesRegex(ValueError, "Format correction changed non-location judgment content"):
            flow.check(self.root, work, allow_incomplete=True)

    def test_nonfrozen_format_after_snapshot_still_rejects_substantive_change(self):
        work = self.prepare_blind(work_name="format-writer-semantic-change", run="format-writer-semantic-change")
        self.write_blind_records(work)
        item = flow.read_json(work / "manifest.json")["articles"][0]
        path = work / "writers" / (item["id"] + ".json")
        backup = work / "format-before/mid_writer_1/1/writer-before.json"
        backup.parent.mkdir(parents=True)
        before = flow.read_json(path)
        flow.write_json(backup, before)
        after = copy.deepcopy(before)
        after["summary"] = "未经draft revision记录的实质性改写。"
        flow.write_json(path, after)
        flow.write_json(work / "format-corrections/mid_writer_1.json", {
            "actor": "mid_writer_1", "reason": "仅应做格式更正。",
            "files": [{"id": item["id"], "path": "writers/1.json",
                       "backup_path": backup.relative_to(work).as_posix(),
                       "before_sha256": flow.file_hash(backup), "after_sha256": flow.file_hash(path),
                       "changes": ["format-only claim"]}]})
        with self.assertRaisesRegex(ValueError, "Format correction changed non-location judgment content"):
            flow.check(self.root, work, allow_incomplete=True)

    def test_input_exception_preserves_unread_reassignment_without_fake_receipt(self):
        work = self.prepare_blind(work_name="exception-work", run="exception-work")
        manifest_path = work / "manifest.json"
        manifest = flow.read_json(manifest_path)
        item = manifest["articles"][0]
        writer_groups = manifest["assignments"]["writer"]
        writer_groups["mid_writer_1"].remove(item["id"])
        writer_groups["mid_writer_2"].append(item["id"])
        for index, group in enumerate(writer_groups.values(), 1):
            flow.write_json(work / f"writer-{index}-assignment.json", group)
        archived = work / "exceptions" / item["id"] / "mid_writer_1" / "outline.json"
        archived_value = {"id": item["id"], "actor": "mid_writer_1", "recorded_before_blind_read": True}
        flow.write_json(archived, archived_value)
        manifest["input_exceptions"] = [{"id": item["id"], "actor": "mid_writer_1",
            "kind": "reported_old_summary_seen_before_blind_outline_saved",
            "archived_record": archived.relative_to(work).as_posix(), "archived_sha256": flow.file_hash(archived),
            "old_initial_created": False, "draft_created": False,
            "resolution": "Archived outline, reassigned to unread mid_writer_2."}]
        flow.write_json(manifest_path, manifest)
        self.write_blind_records(work, writer_actor="mid_writer_2")
        _, pairs, pending = flow.check(self.root, work)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)
        flow.apply(self.root, work)
        audit = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertEqual(audit["input_exceptions"], manifest["input_exceptions"])

    def test_apply_requires_complete_current_primary_acceptance_without_partial_write(self):
        work = self.prepare_blind(work_name="primary-acceptance-work", run="primary-acceptance-work")
        self.write_blind_records(work)
        item = flow.read_json(work / "manifest.json")["articles"][0]
        writer = flow.read_json(work / "writers" / (item["id"] + ".json"))
        before = self.output_bytes()
        incomplete = {"decider": "Codex主代理", "entries": []}
        flow.write_json(work / "primary-acceptance.json", incomplete)
        with self.assertRaisesRegex(ValueError, "exact run scope"):
            flow.apply(self.root, work)
        self.assertEqual(before, self.output_bytes())

        stale = {"decider": "Codex主代理", "entries": [{"id": item["id"], "accepted": True,
                 "summary_sha256": "0" * 64, "review_sha256": "stale-review-hash"}]}
        flow.write_json(work / "primary-acceptance.json", stale)
        with self.assertRaisesRegex(ValueError, "primary acceptance summary SHA is stale"):
            flow.apply(self.root, work)
        self.assertEqual(before, self.output_bytes())

        accepted = {"decider": "Codex主代理", "scope_note": "本测试仅核对接口。",
                    "entries": [{"id": item["id"], "accepted": True,
                                 "summary_sha256": flow.digest(writer["summary"]),
                                 "review_sha256": "not-enforced-by-design"}]}
        flow.write_json(work / "primary-acceptance.json", accepted)
        flow.apply(self.root, work)
        audit = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertEqual(audit["primary_acceptance"], accepted)

    def test_accepted_only_publication_cumulates_idempotently_and_completes_full_scope(self):
        work = self.prepare_multi_blind(lengths=[9000, 11000, 16000])
        manifest = flow.read_json(work / "manifest.json")
        target_ids = [item["id"] for item in manifest["articles"]]
        owners = {role: {article_id: actor for actor, ids in groups.items() for article_id in ids}
                  for role, groups in manifest["assignments"].items()}

        def add_records(article_id):
            self.write_blind_records(work, article_id=article_id, writer_actor=owners["writer"][article_id],
                                     reviewer_actor=owners["reviewer"][article_id])
            writer = flow.read_json(work / "writers" / (article_id + ".json"))
            return {"id": article_id, "accepted": True, "summary_sha256": flow.digest(writer["summary"]),
                    "review_sha256": "independent-review-receipt"}

        first = add_records(target_ids[0])
        acceptance = {"decider": "Codex主代理", "entries": [first]}
        flow.write_json(work / "primary-acceptance.json", acceptance)
        result = flow.apply(self.root, work, accepted_only=True, publication_ids=[target_ids[0]])
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(result["publication"], {"target_scope": 3, "status": "in_progress",
                         "published_ids": [target_ids[0]], "pending_ids": target_ids[1:]})
        audit1 = flow.read_json(self.root / "data/summary-reviews.json")
        first_published_entry = audit1["entries"][0]
        self.assertEqual([entry["id"] for entry in audit1["entries"]], [target_ids[0]])
        self.assertEqual(audit1["statistics"]["reviewed"], 1)
        self.assertEqual(len(audit1["publication_history"]), 1)
        self.assertEqual(audit1["statistics"]["length_strata"], [
            {"lower": 8000, "upper": 10000, "population_count": 1, "published_count": 0},
            {"lower": 10000, "upper": 15000, "population_count": 1, "published_count": 0},
            {"lower": 15000, "upper": 20000, "population_count": 1, "published_count": 1}])
        self.assertEqual(flow.read_json(self.root / "data/catalog.json")["metadata"]["summary_reviews"]["publication"],
                         {"status": "in_progress", "target_scope": 3, "published": 1, "pending": 2})
        published_bytes = self.output_bytes()
        flow.apply(self.root, work, accepted_only=True, publication_ids=[target_ids[0]])
        self.assertEqual(published_bytes, self.output_bytes())

        second = add_records(target_ids[1])
        acceptance["entries"].append(second)
        flow.write_json(work / "primary-acceptance.json", acceptance)
        ids_file = work / "release-ids.json"
        flow.write_json(ids_file, target_ids[:2])
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = flow.main(["--root", str(self.root), "apply", "--work-dir", str(work),
                              "--accepted-only", "--article-ids-file", str(ids_file)])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["reviewed"], 2)
        audit2 = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertEqual([entry["id"] for entry in audit2["entries"]], target_ids[:2])
        self.assertEqual(audit2["entries"][0], first_published_entry)
        self.assertEqual(audit2["statistics"]["reviewed"], 2)
        self.assertEqual([row["population_count"] for row in audit2["statistics"]["length_strata"]], [1, 1, 1])
        self.assertEqual([row["published_count"] for row in audit2["statistics"]["length_strata"]], [0, 1, 1])
        self.assertEqual(len(audit2["publication_history"]), 2)

        third = add_records(target_ids[2])
        acceptance["entries"].append(third)
        flow.write_json(work / "primary-acceptance.json", acceptance)
        result = flow.apply(self.root, work)
        self.assertEqual(result["reviewed"], 3)
        self.assertEqual(result["publication"]["status"], "complete")
        self.assertEqual(result["publication"]["published_ids"], target_ids)
        self.assertEqual(result["publication"]["pending_ids"], [])
        audit3 = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertEqual([entry["id"] for entry in audit3["entries"]], target_ids)
        self.assertEqual(audit3["entries"][0], first_published_entry)
        self.assertEqual(audit3["statistics"]["reviewed"], 3)
        self.assertEqual([row["population_count"] for row in audit3["statistics"]["length_strata"]], [1, 1, 1])
        self.assertEqual([row["published_count"] for row in audit3["statistics"]["length_strata"]], [1, 1, 1])
        self.assertEqual(len(audit3["publication_history"]), 3)
        completed_bytes = self.output_bytes()
        flow.apply(self.root, work)
        self.assertEqual(completed_bytes, self.output_bytes())

    def test_accepted_only_refuses_shrinking_or_changing_published_receipts_and_drafts(self):
        work = self.prepare_multi_blind(count=2, work_name=".work/publication-protection", run="publication-protection")
        manifest = flow.read_json(work / "manifest.json")
        target_ids = [item["id"] for item in manifest["articles"]]
        owners = {role: {article_id: actor for actor, ids in groups.items() for article_id in ids}
                  for role, groups in manifest["assignments"].items()}
        acceptance_entries = []
        for article_id in target_ids:
            self.write_blind_records(work, article_id=article_id, writer_actor=owners["writer"][article_id],
                                     reviewer_actor=owners["reviewer"][article_id])
            writer = flow.read_json(work / "writers" / (article_id + ".json"))
            acceptance_entries.append({"id": article_id, "accepted": True,
                                       "summary_sha256": flow.digest(writer["summary"]),
                                       "review_sha256": "unchanged-review-receipt"})
        acceptance = {"decider": "Codex主代理", "entries": acceptance_entries}
        flow.write_json(work / "primary-acceptance.json", acceptance)
        flow.apply(self.root, work, accepted_only=True, publication_ids=[target_ids[0]])

        with self.assertRaisesRegex(ValueError, "cannot omit already published IDs"):
            flow.apply(self.root, work, accepted_only=True, publication_ids=[target_ids[1]])

        before = self.output_bytes()
        writer_path = work / "writers" / (target_ids[0] + ".json")
        review_path = work / "reviewers" / (target_ids[0] + ".json")
        writer, review = flow.read_json(writer_path), flow.read_json(review_path)
        changed_summary = "本文主张学习关注理解，并依据及时反馈调整方法。"
        writer["summary"] = changed_summary
        for point in writer["major_points"]:
            point["summary_evidence"] = changed_summary
        review["summary_sha256"] = flow.digest(changed_summary)
        for point in review["major_point_checks"]:
            point["evidence"] = changed_summary
        for point in review["independent_major_points"]:
            point["summary_evidence"] = changed_summary
        flow.write_json(writer_path, writer)
        flow.write_json(review_path, review)
        with self.assertRaisesRegex(ValueError, "currently accepted with a matching summary SHA"):
            flow.apply(self.root, work, accepted_only=True, publication_ids=target_ids)
        self.assertEqual(before, self.output_bytes())

        acceptance["entries"][0]["review_sha256"] = "changed-published-receipt"
        flow.write_json(work / "primary-acceptance.json", acceptance)
        with self.assertRaisesRegex(ValueError, "primary acceptance receipt changed"):
            flow.apply(self.root, work, accepted_only=True, publication_ids=target_ids)
        self.assertEqual(before, self.output_bytes())

    def test_accepted_only_still_checks_sources_and_old_summaries_for_unselected_items(self):
        work = self.prepare_multi_blind(count=2, work_name=".work/fixed-unselected", run="fixed-unselected")
        manifest = flow.read_json(work / "manifest.json")
        target_ids = [item["id"] for item in manifest["articles"]]
        owners = {role: {article_id: actor for actor, ids in groups.items() for article_id in ids}
                  for role, groups in manifest["assignments"].items()}
        self.write_blind_records(work, article_id=target_ids[0], writer_actor=owners["writer"][target_ids[0]],
                                 reviewer_actor=owners["reviewer"][target_ids[0]])
        writer = flow.read_json(work / "writers" / (target_ids[0] + ".json"))
        acceptance = {"decider": "Codex主代理", "entries": [{"id": target_ids[0], "accepted": True,
                     "summary_sha256": flow.digest(writer["summary"])}]}
        flow.write_json(work / "primary-acceptance.json", acceptance)

        catalog_path = self.root / "data/catalog.json"
        original_catalog = flow.read_json(catalog_path)
        changed_source = copy.deepcopy(original_catalog)
        changed_source["articles"][1]["source_sha256"] = "0" * 64
        flow.write_json(catalog_path, changed_source)
        with self.assertRaisesRegex(ValueError, "catalog source changed"):
            flow.apply(self.root, work, accepted_only=True, publication_ids=[target_ids[0]])
        flow.write_json(catalog_path, original_catalog)

        changed_summary = copy.deepcopy(original_catalog)
        changed_summary["articles"][1]["summary"] = "未发布文章的旧摘要已变化。"
        flow.write_json(catalog_path, changed_summary)
        with self.assertRaisesRegex(ValueError, "unselected old summary changed"):
            flow.apply(self.root, work, accepted_only=True, publication_ids=[target_ids[0]])

    def test_cross_stage_exception_requires_reassignment_and_exact_archives(self):
        work = self.prepare_blind(work_name="cross-stage-exception", run="cross-stage-exception")
        manifest_path = work / "manifest.json"
        manifest = flow.read_json(manifest_path)
        item = manifest["articles"][0]
        old_actor, new_actor = "mid_writer_1", "mid_writer_2"
        archive_records = []
        for role in ("outline", "initial", "writer"):
            record = {"id": item["id"], "role": role, "model": flow.MODEL, "reasoning_effort": flow.EFFORT,
                      "source_sha256": item["source_sha256"], "body_sha256": item["body_sha256"],
                      "read_chunks": list(range(len(item["chunks"]))), "old_record_marker": role}
            record["writer" if role == "writer" else "actor"] = old_actor
            path = work / "exceptions" / item["id"] / old_actor / (role + ".json")
            flow.write_json(path, record)
            archive_records.append({"role": role, "path": path.relative_to(work).as_posix(),
                                    "sha256": flow.file_hash(path), "record": record})
        exception = {"id": item["id"], "actor": old_actor,
                     "kind": "cross_stage_point_mismatch_reassigned",
                     "resolution": "原记录归档，分配给未读的新写作者。", "archived_records": archive_records}
        manifest["input_exceptions"] = [exception]
        flow.write_json(manifest_path, manifest)
        owners = {role: {article_id: actor for actor, ids in groups.items() for article_id in ids}
                  for role, groups in manifest["assignments"].items()}
        with self.assertRaisesRegex(ValueError, "exception was not reassigned"):
            flow._validate_input_exceptions(work, manifest, owners)

        groups = manifest["assignments"]["writer"]
        groups[old_actor].remove(item["id"])
        groups[new_actor].append(item["id"])
        for index, (actor, ids) in enumerate(groups.items(), 1):
            flow.write_json(work / f"writer-{index}-assignment.json", ids)
        manifest["assignments"]["writer"] = {actor: groups[actor] for actor in groups}
        flow.write_json(manifest_path, manifest)
        owners = {role: {article_id: actor for actor, ids in role_groups.items() for article_id in ids}
                  for role, role_groups in manifest["assignments"].items()}

        bad_manifest = copy.deepcopy(manifest)
        bad_manifest["input_exceptions"][0]["archived_records"][0]["sha256"] = "0" * 64
        flow.write_json(manifest_path, bad_manifest)
        with self.assertRaisesRegex(ValueError, "archived record file/hash mismatch"):
            flow.check(self.root, work, allow_incomplete=True)
        flow.write_json(manifest_path, manifest)

        partial, pairs, pending = flow.check(self.root, work, allow_incomplete=True)
        self.assertEqual(pairs, [])
        self.assertTrue(any(pending_item.startswith(item["id"] + ":writer-outline") for pending_item in pending))
        self.write_blind_records(work, writer_actor=new_actor)
        _, pairs, pending = flow.check(self.root, work)
        self.assertEqual(pending, [])
        self.assertEqual(len(pairs), 1)
        flow.apply(self.root, work)
        audit = flow.read_json(self.root / "data/summary-reviews.json")
        self.assertEqual(audit["input_exceptions"], manifest["input_exceptions"])

    def test_format_correction_same_path_uses_append_only_hash_chain(self):
        work = self.root / "format-chain"
        target = work / "outlines/mid_writer_1/1.json"
        target.parent.mkdir(parents=True)
        backup_dir = work / "format-before/mid_writer_1/1"
        backup_dir.mkdir(parents=True)
        original = {"id": "1", "actor": "mid_writer_1", "major_points": [{"key": "P1", "point": "保持原意",
                    "source_spans": [{"start": 0, "end": 20, "quote": "第一句原文内容"}]}]}
        flow.write_json(target, original)
        backup1 = backup_dir / "step1.json"
        backup1.write_bytes(target.read_bytes())
        first = flow.read_json(target)
        first["major_points"][0]["source_spans"] = [{"start": 1, "end": 21, "quote": "一句原文内容第二"}]
        flow.write_json(target, first)
        record1 = {"id": "1", "path": "outlines/mid_writer_1/1.json",
                   "backup_path": backup1.relative_to(work).as_posix(), "before_sha256": flow.file_hash(backup1),
                   "after_sha256": flow.file_hash(target), "changes": ["窄化quote范围"]}
        backup2 = backup_dir / "step2.json"
        backup2.write_bytes(target.read_bytes())
        second = flow.read_json(target)
        second["major_points"][0]["source_spans"] = [{"start": 2, "end": 22, "quote": "句原文内容第二句"}]
        flow.write_json(target, second)
        record2 = {"id": "1", "path": record1["path"],
                   "backup_path": backup2.relative_to(work).as_posix(), "before_sha256": flow.file_hash(backup2),
                   "after_sha256": flow.file_hash(target), "changes": ["再次对齐quote"]}
        backup3 = backup_dir / "step3.json"
        backup3.write_bytes(target.read_bytes())
        third = flow.read_json(target)
        third["major_points"][0]["source_spans"] = [{"start": 3, "end": 23, "quote": "原文内容第二句后"}]
        flow.write_json(target, third)
        record3 = {"id": "1", "path": record1["path"],
                   "backup_path": backup3.relative_to(work).as_posix(), "before_sha256": flow.file_hash(backup3),
                   "after_sha256": flow.file_hash(target), "changes": ["第三步对齐quote"]}
        extra_backup = work / "format-before/old-unlogged/extra.json"
        extra_backup.parent.mkdir(parents=True)
        extra_backup.write_text("{}")
        flow.write_json(work / "format-corrections/mid_writer_1.json",
                        {"actor": "mid_writer_1", "reason": "连续格式更正", "files": [record1, record2, record3]})
        manifest = {"articles": [{"id": "1"}]}
        owners = {"writer": {"1": "mid_writer_1"}, "reviewer": {"1": "mid_reviewer_1"}}
        logs, aliases = flow._validate_format_corrections(work, manifest, owners)
        self.assertEqual(len(logs), 1)
        self.assertEqual(len(aliases[record1["path"]]), 4)
        # Independent extra snapshots need not invent correction records.
        self.assertTrue(extra_backup.exists())
        # Removing a middle sidecar entry breaks the hash chain even though its backup remains.
        flow.write_json(work / "format-corrections/mid_writer_1.json",
                        {"actor": "mid_writer_1", "reason": "错误删去历史记录", "files": [record1, record3]})
        with self.assertRaisesRegex(ValueError, "Ambiguous or disconnected format-correction chain"):
            flow._validate_format_corrections(work, manifest, owners)

    def test_nonfrozen_format_components_allow_disconnected_draft_revision_steps(self):
        work = self.root / "draft-bridge"
        target = work / "writers/1.json"
        before_format = work / "format-before/mid_writer_1/1/writer-before-format.json"
        intermediate = work / "draft-before/mid_writer_1/1/writer-after-revision.json"
        after_first_format_snapshot = work / "draft-before/mid_writer_1/1/writer-after-format.json"
        target.parent.mkdir(parents=True)
        before_format.parent.mkdir(parents=True)
        intermediate.parent.mkdir(parents=True)
        original = {"id": "1", "writer": "mid_writer_1", "source_sha256": "a" * 64,
                    "body_sha256": "b" * 64, "read_chunks": [0, 1], "summary": "原始草稿。",
                    "major_points": [{"key": "P1", "point": "原始观点", "source_spans": [{"start": 0, "end": 20, "quote": "第一段原文内容"}]}]}
        flow.write_json(target, original)
        before_format.write_bytes(target.read_bytes())
        first = copy.deepcopy(original)
        first["major_points"][0]["source_spans"] = [{"start": 1, "end": 21, "quote": "一段原文内容第二"}]
        flow.write_json(target, first)
        first_hash = flow.file_hash(target)
        after_first_format_snapshot.write_bytes(target.read_bytes())

        # A substantive draft revision starts from the pre-format version and
        # ends at a new state before the second formatting component.
        revised = copy.deepcopy(original)
        revised["summary"] = "实质修订后的草稿。"
        flow.write_json(intermediate, revised)
        second_before = work / "format-before/mid_writer_1/1/writer-after-revision-before-format.json"
        second_before.write_bytes(intermediate.read_bytes())
        second = copy.deepcopy(revised)
        second["major_points"][0]["source_spans"] = [{"start": 2, "end": 22, "quote": "段原文内容第二段"}]
        flow.write_json(target, second)

        backup1 = before_format
        record1 = {"id": "1", "path": "writers/1.json", "backup_path": backup1.relative_to(work).as_posix(),
                   "before_sha256": flow.file_hash(backup1), "after_sha256": first_hash, "changes": ["首次格式定位"]}
        record2 = {"id": "1", "path": "writers/1.json", "backup_path": second_before.relative_to(work).as_posix(),
                   "before_sha256": flow.file_hash(second_before), "after_sha256": flow.file_hash(target),
                   "changes": ["实质修订后再次格式定位"]}
        flow.write_json(work / "format-corrections/mid_writer_1.json",
                        {"actor": "mid_writer_1", "reason": "两次格式修复间发生独立稿件修订。", "files": [record1, record2]})
        draft_log = {"actor": "mid_writer_1", "reason": "记录实质稿件修订。", "revisions": [{
            "id": "1", "path": "writers/1.json", "before_sha256": flow.file_hash(backup1),
            "after_sha256": flow.file_hash(intermediate), "backup_path": backup1.relative_to(work).as_posix(),
            "intermediate_path": intermediate.relative_to(work).as_posix(), "changes": ["新增P9"]}]}
        flow.write_json(work / "draft-revisions/mid_writer_1.json", draft_log)
        manifest = {"articles": [{"id": "1", "source_sha256": "a" * 64, "body_sha256": "b" * 64,
                                  "chunks": [{"index": 0}, {"index": 1}]}]}
        owners = {"writer": {"1": "mid_writer_1"}, "reviewer": {"1": "mid_reviewer_1"}}
        logs, _ = flow._validate_format_corrections(work, manifest, owners)
        self.assertEqual(len(logs), 1)
        # A substantive draft log is retained as process evidence, but its
        # exact intermediate hash chain is not a prerequisite for final review.
        flow.write_json(work / "draft-revisions/mid_writer_1.json",
                        {"actor": "mid_writer_1", "reason": "草稿日志保留为过程证据。", "revisions": []})
        logs, _ = flow._validate_format_corrections(work, manifest, owners)
        self.assertEqual(len(logs), 1)

    def test_initial_reason_field_rename_preserves_text_only(self):
        def validate_change(directory_name, after_point):
            work = self.root / directory_name
            target = work / "initial/1.json"
            backup = work / "format-before/mid_writer_1/1/initial.json"
            target.parent.mkdir(parents=True)
            backup.parent.mkdir(parents=True)
            before = {"id": "1", "actor": "mid_writer_1", "outline_sha256": "a" * 64,
                      "major_points": [{"key": "P1", "point": "实质主要观点",
                                        "source_spans": [{"start": 0, "end": 12}],
                                        "old_coverage": "missing", "old_evidence": None,
                                        "reason": "原有的覆盖判断理由"}]}
            flow.write_json(backup, before)
            after = copy.deepcopy(before)
            after["outline_sha256"] = "b" * 64
            after["major_points"][0] = after_point(copy.deepcopy(before["major_points"][0]))
            flow.write_json(target, after)
            log = {"actor": "mid_writer_1", "reason": "保留理由文本并更新字段名。",
                   "files": [{"id": "1", "path": "initial/1.json", "backup_path": backup.relative_to(work).as_posix(),
                              "before_sha256": flow.file_hash(backup), "after_sha256": flow.file_hash(target),
                              "changes": ["reason field compatibility"]}]}
            flow.write_json(work / "format-corrections/mid_writer_1.json", log)
            owners = {"writer": {"1": "mid_writer_1"}, "reviewer": {"1": "mid_reviewer_1"}}
            return flow._validate_format_corrections(work, {"articles": [{"id": "1"}]}, owners)

        def same_text(point):
            point["old_coverage_reason"] = point.pop("reason")
            return point

        logs, _ = validate_change("reason-rename-valid", same_text)
        self.assertEqual(len(logs), 1)

        def changed_text(point):
            point.pop("reason")
            point["old_coverage_reason"] = "被改写的判断理由"
            return point

        with self.assertRaisesRegex(ValueError, "Format correction changed non-location judgment content"):
            validate_change("reason-rename-changed", changed_text)

        def conflicting_fields(point):
            point["old_coverage_reason"] = "与原字段冲突的文字"
            return point

        with self.assertRaisesRegex(ValueError, "conflicting reason/old_coverage_reason"):
            validate_change("reason-rename-conflict", conflicting_fields)

    def test_initial_point_text_repair_requires_explicit_exact_manifest_approval(self):
        old_text, new_text = "本可以追求独有的问题", "本可以追求独有"
        repair = {"id": "1", "actor": "mid_writer_4", "key": "P4",
                  "before_point_sha256": flow.digest(old_text), "after_point_sha256": flow.digest(new_text),
                  "reason": "同义point恢复为对应冻结盲提纲的精确文案。"}

        def create_case(name, approved):
            work = self.root / name
            outline_path = work / "outlines/mid_writer_4/1.json"
            initial_path = work / "initial/1.json"
            outline_path.parent.mkdir(parents=True)
            initial_path.parent.mkdir(parents=True, exist_ok=True)
            outline = {"id": "1", "actor": "mid_writer_4", "major_points": [{"key": "P4", "point": new_text,
                        "source_spans": [{"start": 0, "end": 10}]}]}
            flow.write_json(outline_path, outline)
            before = {"id": "1", "actor": "mid_writer_4", "outline_sha256": "a" * 64,
                      "major_points": [{"key": "P4", "point": old_text, "old_coverage": "covered",
                                        "old_evidence": "旧稿原有证据片段", "source_spans": [{"start": 0, "end": 10}]}]}
            backup = work / "format-before/mid_writer_4/1/initial.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            flow.write_json(backup, before)
            after = copy.deepcopy(before)
            after["outline_sha256"] = flow.file_hash(outline_path)
            after["major_points"][0]["point"] = new_text
            flow.write_json(initial_path, after)
            flow.write_json(work / "format-corrections/mid_writer_4.json", {
                "actor": "mid_writer_4", "reason": "定位格式修复",
                "files": [{"id": "1", "path": "initial/1.json", "backup_path": backup.relative_to(work).as_posix(),
                           "before_sha256": flow.file_hash(backup), "after_sha256": flow.file_hash(initial_path),
                           "changes": ["restored same-meaning initial point"]}]})
            repairs = [copy.deepcopy(repair)] if approved else []
            manifest = {"articles": [{"id": "1"}], "initial_point_repairs": repairs}
            owners = {"writer": {"1": "mid_writer_4"}, "reviewer": {"1": "mid_reviewer_1"}}
            return work, manifest, owners

        work, manifest, owners = create_case("initial-point-approved", True)
        logs, _ = flow._validate_format_corrections(work, manifest, owners)
        self.assertEqual(len(logs), 1)
        self.assertEqual(manifest["initial_point_repairs"], [repair])

        work, manifest, owners = create_case("initial-point-unapproved", False)
        with self.assertRaisesRegex(ValueError, "non-location judgment content"):
            flow._validate_format_corrections(work, manifest, owners)

        work, manifest, owners = create_case("initial-point-wrong-hash", True)
        manifest["initial_point_repairs"][0]["before_point_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "point repair text hash mismatch"):
            flow._validate_format_corrections(work, manifest, owners)

    def test_format_metadata_repairs_are_narrow_and_source_based(self):
        expected_source = "a" * 64
        expected_body = "b" * 64
        owners = {"writer": {"1": "mid_writer_4"}, "reviewer": {"1": "mid_reviewer_1"}}

        def validate_change(name, before_source, before_chunks, after_source=expected_source, after_chunks=None, chunk_count=2):
            work = self.root / name
            path = work / "outlines/mid_writer_4/1.json"
            backup = work / "format-before/mid_writer_4/1/outline.json"
            path.parent.mkdir(parents=True)
            backup.parent.mkdir(parents=True)
            expected_chunks = list(range(chunk_count))
            item = {"id": "1", "source_sha256": expected_source, "body_sha256": expected_body,
                    "chunks": [{"index": index} for index in expected_chunks]}
            after_chunks = expected_chunks if after_chunks is None else after_chunks
            before = {"id": "1", "actor": "mid_writer_4", "source_sha256": before_source,
                      "body_sha256": expected_body, "read_chunks": before_chunks,
                      "major_points": [{"key": "P1", "point": "不变的观点",
                                        "source_spans": [{"start": 0, "end": 8}]}]}
            after = {**before, "source_sha256": after_source, "read_chunks": after_chunks}
            flow.write_json(backup, before)
            flow.write_json(path, after)
            flow.write_json(work / "format-corrections/mid_writer_4.json", {
                "actor": "mid_writer_4", "reason": "修复备份中的来源元数据格式",
                "files": [{"id": "1", "path": "outlines/mid_writer_4/1.json",
                           "backup_path": backup.relative_to(work).as_posix(),
                           "before_sha256": flow.file_hash(backup), "after_sha256": flow.file_hash(path),
                           "changes": ["source/read_chunks metadata correction"]}]})
            return flow._validate_format_corrections(work, {"articles": [item]}, owners)

        # A malformed 62-character hash can be corrected only to this item's trusted hash.
        logs, _ = validate_change("source-hash-truncated", "a" * 62, [0, 1])
        self.assertEqual(len(logs), 1)
        # A syntactically valid but different SHA is not a format typo.
        with self.assertRaisesRegex(ValueError, "Invalid source_sha256 format correction"):
            validate_change("source-hash-valid-wrong", "c" * 64, [0, 1])
        # Extra out-of-range read receipts can be removed, but missing actual chunks cannot be filled in.
        validate_change("read-chunks-extra", expected_source, [0, 1, 2])
        with self.assertRaisesRegex(ValueError, "Invalid read_chunks format correction"):
            validate_change("read-chunks-missing", expected_source, [0, 2])
        # A legacy scalar count can be normalized only when it equals the actual chunk count.
        validate_change("read-chunks-scalar-count", expected_source, 3, chunk_count=3)
        with self.assertRaisesRegex(ValueError, "Invalid read_chunks format correction"):
            validate_change("read-chunks-scalar-too-small", expected_source, 2, chunk_count=3)
        with self.assertRaisesRegex(ValueError, "Invalid read_chunks format correction"):
            validate_change("read-chunks-scalar-too-large", expected_source, 4, chunk_count=3)
        with self.assertRaisesRegex(ValueError, "Invalid read_chunks format correction"):
            validate_change("read-chunks-bool-is-not-int", expected_source, True, chunk_count=1)

    def test_blind_check_pins_sampling_report_and_requires_fresh_work(self):
        report_path = self.root / "data/summary-sampling-audit.json"
        flow.write_json(report_path, {"run_id": "sampling", "entries": []})
        work = self.root / "frozen-work"
        flow.prepare(self.root, self.sources, self.baseline, work, "frozen", "2026-11-01", 20,
                     max_body_chars=len(self.body) + 1, actor_prefix="mid", blind_review=True)
        report_path.write_text("changed")
        with self.assertRaisesRegex(ValueError, "Preserved file changed"):
            flow.check(self.root, work, allow_incomplete=True)
        nonempty = self.root / "nonempty-work"
        nonempty.mkdir()
        (nonempty / "stale.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Fresh empty work directory"):
            flow.prepare(self.root, self.sources, self.baseline, nonempty, "blocked", "2026-11-01", 20,
                         max_body_chars=len(self.body) + 1, actor_prefix="mid", blind_review=True)

    def test_staged_write_rolls_back_all_targets_when_second_replace_fails(self):
        directory = self.root / "staged"
        directory.mkdir()
        catalog = directory / "catalog.json"
        new_target = directory / "index.html"
        ledger = directory / "ledger.json"
        catalog.write_bytes(b"old catalog")
        ledger.write_bytes(b"old ledger")
        original_replace = Path.replace
        calls = 0

        def fail_after_second_replace(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                original_replace(source, target)
                raise OSError("injected second replace failure")
            return original_replace(source, target)

        with patch.object(Path, "replace", fail_after_second_replace):
            with self.assertRaisesRegex(OSError, "injected second replace failure"):
                flow.staged_write({catalog: "new catalog", new_target: "new index", ledger: "new ledger"})

        self.assertEqual(catalog.read_bytes(), b"old catalog")
        self.assertFalse(new_target.exists())
        self.assertEqual(ledger.read_bytes(), b"old ledger")
        self.assertEqual(list(directory.glob(".summary-*")) + list(directory.glob(".summary-backup-*")), [])


if __name__ == "__main__":
    unittest.main()
