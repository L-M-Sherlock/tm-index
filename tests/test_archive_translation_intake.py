"""Pin archive additions and preserve the live index while staging them."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import archive_translation_intake as intake


class TranslationIntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.archive = self.workspace / "archive"
        self.live = self.workspace / "live"
        self.work = self.workspace / "work"
        self.preview = self.workspace / "preview"
        (self.archive / "article").mkdir(parents=True)
        self.git("init", "-q")
        self.write_article("1", "<p>原始文章 <a href='https://zhuanlan.zhihu.com/p/2'>新增译文</a></p>")
        self.base = self.commit("baseline")
        self.write_article("2", "<p>翻译证据</p><a href='https://zhuanlan.zhihu.com/p/1'>既有译文</a>")
        self.write_article("3", "<p>原创文章</p>")
        self.tip = self.commit("backup")
        raw = (self.archive / "article/1.json").read_bytes()
        old, _, _ = intake.archive_record("article/1.json", raw, json.loads(raw))
        old.update(summary="已复核的摘要", decision="translation", keywords=["学习"],
                   references=[], referenced_by=[], reference_count=0, referenced_by_count=0,
                   original_author_status="resolved", original_author_source="byline")
        refs = {"link_records_examined": 0, "self_links_ignored": 0, "direct_links_to_nontranslations": 0,
                "method_occurrence_counts": {}}
        metadata = {
            "references": refs, "keyword_aliases": {"学习方法": "学习"}, "publisher_counts": {"测试发布者": 1},
            "source_articles": 1, "annotated_articles": 1, "decision_counts": {"translation": 1, "excluded": 0},
            "covered_visible_text_chars": old["read_chars"], "original_authors": {},
        }
        self.catalog = {"metadata": metadata, "articles": [old]}
        for name, value in (
            ("data/catalog.json", self.catalog), ("data/validation.json", metadata),
            ("data/references.json", {"metadata": refs, "edges": []}), ("data/authors.json", []),
            ("data/keywords.json", [{"keyword": "学习", "count": 1}]),
            ("data/summary-revisions.json", {"entries": [{"original_revision": "preserved"}]}),
        ):
            intake.write_json(self.live / name, value)
        (self.live / "data/original-authors.jsonl").write_text('{"id": "1"}\n')
        (self.live / "archive-export.js").write_text("// original export module\n")
        html = {
            "records": self.catalog["articles"], "report": metadata, "exportOrder": ["1"],
            "answerIndex": {"answer": "keep"},
        }
        (self.live / "index.html").write_text(
            '<script id="catalog-data" type="application/json">' + json.dumps(html) + '</script><footer>keep</footer>'
        )
        intake.prepare(self.archive, self.base, self.tip, self.work)
        item = intake.read_json(self.work / "base-rows.json")[0]
        item.update(decision="translation", scope_reason="标明来源的翻译", evidence="翻译证据",
                    keywords=["学习方法", "学习"], original_authors=["作者甲"], original_author_source="byline",
                    original_author_status="resolved", original_author_evidence=[{"text": "作者署名"}],
                    original_author_notes="归属已核对", author_entities=[{"name": "作者甲", "type": "person", "role": "author"}],
                    adaptation_authors=[], original_source_urls=[], original_author_aliases=[],
                    author_extraction_agent="test")
        self.rows_file = self.work / "rows.json"
        self.exclusions_file = self.work / "exclusions.json"
        intake.write_json(self.rows_file, [item])
        intake.write_json(self.exclusions_file, {"3": "原创文章"})

    def git(self, *args):
        return intake.git(self.archive, *args).decode().strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def write_article(self, article_id, content):
        intake.write_json(
            self.archive / "article" / f"{article_id}.json",
            {"id": article_id, "title": f"文章 {article_id}", "content": content, "voteup_count": 10,
             "author": {"name": "测试发布者", "url_token": "fixture"}, "created": 1730000000 + int(article_id)},
        )

    def stage(self):
        return intake.stage(self.live, self.preview, self.work, self.rows_file,
                            self.exclusions_file, self.archive, "2026-11-21")

    def test_stage_uses_pinned_archive_and_preserves_live_index(self):
        self.write_article("2", "<p>工作目录中的临时修改</p>")
        before = intake.site_hashes(self.live)
        receipt = self.stage()
        self.assertEqual(receipt["baseline_sha256"], before)
        self.assertEqual(intake.site_hashes(self.live), before)
        self.assertEqual(receipt["included_article_ids"], ["2"])
        self.assertEqual(receipt["excluded_articles"], {"3": "原创文章"})
        self.assertEqual(receipt["new_direct_references"], 2)
        new = intake.read_json(self.preview / "data/catalog.json")
        rows = intake.catalog_rows(new)
        self.assertEqual(rows["1"]["summary"], "已复核的摘要")
        self.assertEqual(rows["1"]["reference_count"], 1)
        self.assertEqual(rows["2"]["referenced_by_count"], 1)
        self.assertEqual(rows["2"]["summary"], "")
        self.assertEqual(rows["2"]["keywords"], ["学习"])
        self.assertEqual(new["metadata"], intake.read_json(self.preview / "data/validation.json"))
        embedded = json.loads(intake.DATA_PATTERN.search((self.preview / "index.html").read_text())[2])
        self.assertEqual(embedded["records"], new["articles"])
        self.assertEqual(embedded["answerIndex"], {"answer": "keep"})
        self.assertEqual(embedded["exportOrder"], ["1", "2"])
        self.assertEqual(intake.site_hashes(self.preview), receipt["staged_sha256"])
        self.assertEqual(before["data/summary-revisions.json"], receipt["staged_sha256"]["data/summary-revisions.json"])

    def test_missing_scope_decision_stops_staging(self):
        intake.write_json(self.exclusions_file, {})
        with self.assertRaisesRegex(ValueError, "cover the pinned archive additions"):
            self.stage()
        self.assertFalse(self.preview.exists())

    def test_changed_source_snapshot_stops_staging(self):
        path = self.work / "body/2.txt"
        path.write_text("伪" + path.read_text()[1:])
        with self.assertRaisesRegex(ValueError, "prepared source bytes changed"):
            self.stage()
        self.assertFalse(self.preview.exists())

    def test_preexisting_summary_and_changed_pinned_metadata_are_rejected(self):
        original = intake.read_json(self.rows_file)
        for field, replacement in (("summary", "未经审核的摘要"), ("voteup_count", 100)):
            with self.subTest(field=field):
                intake.write_json(self.rows_file, [{**original[0], field: replacement}])
                with self.assertRaisesRegex(ValueError, "differ from pinned archive"):
                    self.stage()

    def test_all_excluded_batch_updates_inventory(self):
        intake.write_json(self.rows_file, [])
        intake.write_json(self.exclusions_file, {"2": "目录", "3": "原创"})
        receipt = self.stage()
        self.assertEqual(receipt["new_translations"], 0)
        self.assertEqual(receipt["source_articles"], 3)
        self.assertEqual(intake.read_json(self.preview / "data/catalog.json")["articles"], self.catalog["articles"])

    def test_metadata_refresh_updates_cited_title_without_rewriting_source_or_summary(self):
        self.stage()
        path = self.archive / "article/1.json"
        raw = intake.read_json(path)
        raw.update(title="备份中的新标题", content="<p>需要独立复核的新正文</p>", voteup_count=20)
        intake.write_json(path, raw)
        update = self.commit("metadata and body changed")
        before = intake.catalog_rows(intake.read_json(self.preview / "data/catalog.json"))
        result = intake.refresh_metadata(self.preview, self.archive, update, ["1"])
        after = intake.catalog_rows(intake.read_json(self.preview / "data/catalog.json"))
        self.assertEqual(result["updated_articles"], 1)
        self.assertEqual(result["reviewed"]["1"]["updated_fields"], ["title", "voteup_count"])
        self.assertNotEqual(result["reviewed"]["1"]["content_sha256"], after["1"]["content_sha256"])
        self.assertEqual(after["1"]["title"], "备份中的新标题")
        self.assertEqual(after["2"]["references"][0]["title"], "备份中的新标题")
        self.assertEqual(after["1"]["voteup_count"], 20)
        for key in ("content_sha256", "source_sha256", "summary", "read_chars", "keywords"):
            self.assertEqual(after["1"][key], before["1"][key])
        final_hashes = intake.site_hashes(self.preview)
        repeated = intake.refresh_metadata(self.preview, self.archive, update, ["1"])
        self.assertEqual(repeated["updated_articles"], 0)
        self.assertEqual(final_hashes, intake.site_hashes(self.preview))


if __name__ == "__main__":
    unittest.main()
