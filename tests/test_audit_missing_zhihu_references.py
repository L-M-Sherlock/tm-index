"""Regression checks for read-only archived citation rechecks."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("audit_missing", Path(__file__).resolve().parents[1] / "scripts/audit_missing_zhihu_references.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class CitationAuditTests(unittest.TestCase):
    def links(self, html):
        parser = audit.ReferenceLinks()
        parser.feed(html)
        parser.close()
        return parser.links

    def classify(self, html):
        return audit.classify_candidate({"target_id": "123", "original_urls": ["https://example.com/Post#section"]}, self.links(html))

    def test_native_reference_resolves_pair(self):
        result = self.classify('<sup data-draft-type="reference" data-url="https://zhuanlan.zhihu.com/p/123"/>')
        self.assertEqual(result["status"], "resolved_direct_link")
        self.assertEqual(result["direct_link_evidence"][0]["kind"], "native_reference")

    def test_redirect_anchor_resolves_pair(self):
        result = self.classify('<a href="https://link.zhihu.com/?target=https%3A%2F%2Fzhuanlan.zhihu.com%2Fp%2F123">译文</a>')
        self.assertEqual(result["status"], "resolved_direct_link")

    def test_external_link_still_missing(self):
        result = self.classify('<a href="http://www.example.com/Post/?utm_source=test#section">原文</a>')
        self.assertEqual(result["status"], "still_missing")

    def test_absent_original_is_not_resolved(self):
        self.assertEqual(self.classify('<a href="https://example.com/Other">其他</a>')["status"], "original_link_absent")

    def test_wrong_zhihu_targets_do_not_resolve(self):
        for url in ["https://evil.example/p/123", "https://zhuanlan.zhihu.com.evil/p/123",
                    "https://zhuanlan.zhihu.com/p/123/extra",
                    "https://www.zhihu.com/question/1/answer/123", "https://zhuanlan.zhihu.com:999/p/123",
                    "https://user@zhuanlan.zhihu.com/p/123"]:
            with self.subTest(url=url):
                self.assertIsNone(audit.zhihu_article_id(url))

    def test_different_valid_article_does_not_resolve_pair(self):
        result = self.classify('<a href="https://zhuanlan.zhihu.com/p/1234">其他译文</a>')
        self.assertEqual(result["status"], "original_link_absent")

    def test_script_and_style_links_are_ignored(self):
        self.assertEqual(self.links('<script><a href="https://zhuanlan.zhihu.com/p/123"></script><style><a href="https://example.com"></style>'), [])

    def test_fragments_and_meaningful_queries_are_not_discarded(self):
        self.assertNotEqual(audit.original_key("https://example.com/book.pdf#page=1"), audit.original_key("https://example.com/book.pdf#page=2"))
        self.assertNotEqual(audit.original_key("https://example.com/?id=1"), audit.original_key("https://example.com/?id=2"))

    def test_both_kinds_of_evidence_are_preserved(self):
        result = self.classify('<a href="https://example.com/Post#section">原文</a><a href="https://zhuanlan.zhihu.com/p/123">译文</a>')
        self.assertEqual(result["status"], "resolved_direct_link")
        self.assertEqual(len(result["original_link_evidence"]), 1)
        self.assertEqual(len(result["direct_link_evidence"]), 1)

    def test_wiki_title_terminal_question_mark_encoding(self):
        for host in ["supermemo.guru", "wiki.issarice.com"]:
            with self.subTest(host=host):
                expected = audit.original_key("https://" + host + "/wiki/Title?")
                actual = audit.original_key("https://link.zhihu.com/?target=https%3A//" + host + "/wiki/Title%253F")
                self.assertEqual(expected, actual)
        self.assertNotEqual(audit.original_key("https://example.com/Title?"), audit.original_key("https://example.com/Title%3F"))

    def test_archive_link_is_not_a_zhihu_link(self):
        self.assertIsNone(audit.zhihu_article_id("https://l-m-sherlock.github.io/ZhiHuArchive/123.html"))

    def test_missing_and_invalid_sources_are_unverifiable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "article").mkdir()
            candidates = {"metadata": {"candidate_edges": 1}, "sources": [{"source_id": "1", "source_title": "来源",
                          "references": [{"target_id": "123", "target_title": "目标", "original_urls": ["https://example.com"]}]}]}
            (root / "data/missing-zhihu-references.json").write_text(json.dumps(candidates), encoding="utf-8")
            with patch.object(audit, "git_value", return_value="head"):
                result = audit.audit(root, root)
                self.assertEqual(result["metadata"]["status_counts"], {"unverifiable": 1})
                (root / "article/1.json").write_text('{"id":"2","content":""}', encoding="utf-8")
                result = audit.audit(root, root)
                self.assertEqual(result["results"][0]["status"], "unverifiable")

    def test_changed_archive_head_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/missing-zhihu-references.json").write_text('{"metadata":{"candidate_edges":0},"sources":[]}', encoding="utf-8")
            with patch.object(audit, "git_value", side_effect=["old", "new"]):
                with self.assertRaisesRegex(ValueError, "HEAD changed"):
                    audit.audit(root, root)

    def test_applied_audit_history_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audit.json"
            output.write_text('{"application":{"applied_at":"previous"}}', encoding="utf-8")
            arguments = ["audit", "--archive", directory, "--output", str(output), "--report", str(Path(directory) / "report.md")]
            with patch("sys.argv", arguments):
                with self.assertRaisesRegex(ValueError, "history must be preserved"):
                    audit.main()
            self.assertEqual(json.loads(output.read_bytes())["application"]["applied_at"], "previous")


if __name__ == "__main__":
    unittest.main()
