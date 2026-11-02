"""Coordinated marker corrections preserve unrelated catalog and page content."""
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import apply_reference_recheck as flow


class ApplyReferenceRecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "archive"
        for relative in ["data", "docs", "archive/article"]:
            (self.root / relative).mkdir(parents=True)
        self.sources = []
        self.source_evidence = []
        self.results = []
        for source_id in ["1", "2"]:
            reference = {"target_id": "3", "target_title": "目标", "recommended_zhihu_url": "https://zhuanlan.zhihu.com/p/3",
                         "original_urls": ["https://example.com/target"], "original_hosts": ["example.com"],
                         "link_texts": ["原文"], "occurrences": 1, "match_quality": ["exact"], "confidence": "high", "category": "content"}
            self.sources.append({"source_id": source_id, "source_title": "来源" + source_id, "source_zhihu_url": "https://zhuanlan.zhihu.com/p/" + source_id,
                                 "missing_zhihu_reference_count": 1, "references": [reference], "reference_live_status": "verified", "reference_checked_at": "old-check"})
            raw = ("raw source " + source_id).encode()
            (self.archive / "article" / (source_id + ".json")).write_bytes(raw)
            self.source_evidence.append({"path": "article/" + source_id + ".json", "source_sha256": flow.digest(raw)})
            self.results.append({"source_id": source_id, "source_title": "来源" + source_id, "target_id": "3", "target_title": "目标",
                                 "status": "resolved_direct_link" if source_id == "1" else "still_missing",
                                 "direct_link_evidence": [{"kind": "native_reference", "url": "https://zhuanlan.zhihu.com/p/3"}] if source_id == "1" else []})
        candidates = {"metadata": {"candidate_edges": 2, "source_articles": 2, "target_articles": 1,
                                   "top_targets": [{"id": "3"}], "prior_review_rows_no_longer_candidates": 0},
                      "sources": self.sources,
                      "edges": [{"source_id": source["source_id"], **reference}
                                for source in self.sources for reference in source["references"]]}
        self.write("data/missing-zhihu-references.json", candidates)
        candidate_raw = (self.root / "data/missing-zhihu-references.json").read_bytes()
        self.audit = {"metadata": {"candidate_file_sha256": flow.digest(candidate_raw), "archive_head": "head", "checked_at": "new-check"},
                      "sources": self.source_evidence, "results": self.results}
        self.write("data/missing-zhihu-reference-recheck.json", self.audit)
        edge = lambda source_id: {"source_id": source_id, "target_id": "3", "via": ["original"], "match_quality": ["exact"],
                                  "source_urls": ["https://example.com/target"], "occurrences": 1}
        metadata = {"method_occurrence_counts": {"zhihu": 0, "original": 2}, "link_occurrences": 2}
        self.write("data/references.json", {"metadata": metadata, "edges": [edge("1"), edge("2")]})
        self.catalog = {"metadata": {"references": metadata, "summary_reviews": {"unchanged": True}}, "articles": []}
        for source_id in ["1", "2", "3"]:
            self.catalog["articles"].append({"id": source_id, "summary": "完整总结" + source_id, "source_sha256": "summary-source-hash",
                                             "missing_zhihu_reference_count": 1 if source_id != "3" else 0,
                                             "references": [{"id": "3", "via": ["original"]}] if source_id != "3" else [],
                                             "referenced_by": [{"id": "1", "via": ["original"]}, {"id": "2", "via": ["original"]}] if source_id == "3" else []})
        self.write("data/catalog.json", self.catalog)
        self.write("data/validation.json", self.catalog["metadata"])
        self.write("data/missing-zhihu-reference-review-method.json", {"input": {}, "validation": {}})
        embedded = {"records": self.catalog["articles"], "report": self.catalog["metadata"], "answerIndex": {"keep": True}, "exportOrder": ["1", "2", "3"]}
        self.html_prefix = '<style>.card{height:720px}</style><script id="catalog-data" type="application/json">'
        self.html_suffix = '</script><script>const untouched = "exports";</script>'
        (self.root / "index.html").write_text(self.html_prefix + json.dumps(embedded) + self.html_suffix, encoding="utf-8")
        (self.root / "docs/missing-zhihu-reference-recheck.md").write_text("历史核验报告\n", encoding="utf-8")
        (self.root / "data/missing-zhihu-references.csv").write_text("source_id,target_id,original_urls,link_texts,reference_checked_at\n1,3,https://example.com/target,原文,old-check\n2,3,https://example.com/target,原文,old-check\n", encoding="utf-8")

    def write(self, relative, value):
        (self.root / relative).write_text(flow.json_text(value), encoding="utf-8")

    def prepare(self):
        with patch.object(flow, "git_value", side_effect=["head", ""]):
            return flow.prepare_updates(self.root, self.archive)

    def test_synchronized_counts_links_and_preserved_summaries(self):
        updates = self.prepare()
        candidates = json.loads(updates["data/missing-zhihu-references.json"])
        self.assertEqual(candidates["metadata"]["candidate_edges"], 1)
        self.assertEqual(candidates["metadata"]["source_articles"], 1)
        self.assertEqual(candidates["sources"][0]["source_id"], "2")
        self.assertEqual([(edge["source_id"], edge["target_id"]) for edge in candidates["edges"]], [("2", "3")])
        catalog = json.loads(updates["data/catalog.json"])
        self.assertEqual([row["summary"] for row in catalog["articles"]], [row["summary"] for row in self.catalog["articles"]])
        self.assertEqual(catalog["metadata"]["summary_reviews"], self.catalog["metadata"]["summary_reviews"])
        self.assertEqual([row["missing_zhihu_reference_count"] for row in catalog["articles"]], [0, 1, 0])
        self.assertEqual(catalog["articles"][0]["references"][0]["via"], ["original", "zhihu"])
        self.assertEqual(catalog["articles"][2]["referenced_by"][0]["via"], ["original", "zhihu"])
        references = json.loads(updates["data/references.json"])
        self.assertEqual(references["metadata"]["link_occurrences"], 3)
        self.assertEqual(references["metadata"]["method_occurrence_counts"], {"original": 2, "zhihu": 1})
        self.assertEqual(references["metadata"], catalog["metadata"]["references"])
        self.assertEqual(references["metadata"], json.loads(updates["data/validation.json"])["references"])
        html = updates["index.html"]
        self.assertTrue(html.startswith(self.html_prefix))
        self.assertTrue(html.endswith(self.html_suffix))
        embedded = json.loads(flow.DATA_PATTERN.search(html).group(2))
        self.assertEqual(embedded["records"], catalog["articles"])
        self.assertEqual(embedded["answerIndex"], {"keep": True})
        rows = list(csv.DictReader(io.StringIO(updates["data/missing-zhihu-references.csv"])))
        self.assertEqual([(row["source_id"], row["target_id"]) for row in rows], [("2", "3")])
        self.assertIn("来源2", updates["docs/missing-zhihu-reference-articles.md"])
        self.assertEqual(json.loads(updates["data/missing-zhihu-reference-recheck.json"])["application"]["removed_candidates"][0]["reference"], self.sources[0]["references"][0])
        self.assertEqual(json.loads((self.root / "data/catalog.json").read_bytes()), self.catalog)

    def test_repeat_application_is_no_op(self):
        for relative, text in self.prepare().items():
            (self.root / relative).write_text(text, encoding="utf-8", newline="")
        self.assertEqual(flow.prepare_updates(self.root, self.archive), {})

    def test_stale_top_level_edges_are_removed(self):
        path = self.root / "data/missing-zhihu-references.json"
        candidates = json.loads(path.read_text(encoding="utf-8"))
        candidates["edges"].append({"source_id": "4", "target_id": "3"})
        self.write("data/missing-zhihu-references.json", candidates)
        self.audit["metadata"]["candidate_file_sha256"] = flow.digest(path.read_bytes())
        self.write("data/missing-zhihu-reference-recheck.json", self.audit)
        result = json.loads(self.prepare()["data/missing-zhihu-references.json"])
        self.assertEqual([(edge["source_id"], edge["target_id"]) for edge in result["edges"]], [("2", "3")])

    def test_repeat_application_does_not_hide_stale_top_level_edges(self):
        for relative, text in self.prepare().items():
            (self.root / relative).write_text(text, encoding="utf-8", newline="")
        path = self.root / "data/missing-zhihu-references.json"
        candidates = json.loads(path.read_text(encoding="utf-8"))
        candidates["edges"].append({"source_id": "1", "target_id": "3"})
        self.write("data/missing-zhihu-references.json", candidates)
        audit = json.loads((self.root / "data/missing-zhihu-reference-recheck.json").read_text(encoding="utf-8"))
        audit["application"]["candidate_file_sha256_after"] = flow.digest(path.read_bytes())
        self.write("data/missing-zhihu-reference-recheck.json", audit)
        with self.assertRaisesRegex(ValueError, "inconsistent top-level edges"):
            flow.prepare_updates(self.root, self.archive)

    def test_missing_or_duplicate_top_level_edge_is_rejected(self):
        path = self.root / "data/missing-zhihu-references.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for edges in (original["edges"][:1], original["edges"] + original["edges"][:1]):
            with self.subTest(edges=edges):
                candidates = {**original, "edges": edges}
                self.write("data/missing-zhihu-references.json", candidates)
                self.audit["metadata"]["candidate_file_sha256"] = flow.digest(path.read_bytes())
                self.write("data/missing-zhihu-reference-recheck.json", self.audit)
                with self.assertRaisesRegex(ValueError, "duplicated or omit"):
                    self.prepare()

    def test_editorial_queue_resolution_preserves_mechanical_counts(self):
        # Source 4 represents an already-reviewed exclusion: it remains a
        # mechanical original-only graph edge but is not in the pending queue.
        candidate_path = self.root / "data/missing-zhihu-references.json"
        candidates = json.loads(candidate_path.read_text(encoding="utf-8"))
        editorial = {"decisions_file": "decisions.json", "mechanical_candidates": 3,
                     "editorially_excluded": 1, "pending_candidates": 2}
        candidates["metadata"]["editorial_review"] = editorial
        for source in candidates["sources"]:
            source["references"][0]["review_reason"] = "需要决定"
        self.write("data/missing-zhihu-references.json", candidates)
        self.audit["metadata"]["candidate_file_sha256"] = flow.digest(candidate_path.read_bytes())
        self.write("data/missing-zhihu-reference-recheck.json", self.audit)

        graph = json.loads((self.root / "data/references.json").read_text(encoding="utf-8"))
        graph["edges"].append({"source_id": "4", "target_id": "3", "via": ["original"],
                               "match_quality": ["exact"], "source_urls": ["https://example.com/target"], "occurrences": 1})
        graph["metadata"].update({"missing_zhihu_reference_candidates": 3, "articles_with_missing_zhihu_references": 3,
                                  "editorial_pending_candidates": 2, "editorial_pending_source_articles": 2,
                                  "editorial_review": editorial})
        graph["metadata"]["method_occurrence_counts"]["original"] = 3
        self.write("data/references.json", graph)
        catalog = json.loads((self.root / "data/catalog.json").read_text(encoding="utf-8"))
        catalog["metadata"]["references"] = graph["metadata"]
        for row in catalog["articles"][:2]:
            row["references"][0].update({"pending_review": True, "pending_review_reason": "需要决定"})
        catalog["articles"].append({"id": "4", "summary": "完整总结4", "source_sha256": "summary-source-hash",
                                    "missing_zhihu_reference_count": 0,
                                    "references": [{"id": "3", "via": ["original"]}], "referenced_by": []})
        catalog["articles"][2]["referenced_by"].append({"id": "4", "via": ["original"]})
        self.write("data/catalog.json", catalog)
        self.write("data/validation.json", catalog["metadata"])
        embedded = {"records": catalog["articles"], "report": catalog["metadata"],
                    "answerIndex": {"keep": True}, "exportOrder": ["1", "2", "3"]}
        (self.root / "index.html").write_text(self.html_prefix + json.dumps(embedded) + self.html_suffix, encoding="utf-8")

        updates = self.prepare()
        result = json.loads(updates["data/missing-zhihu-references.json"])
        result_graph = json.loads(updates["data/references.json"])
        result_catalog = json.loads(updates["data/catalog.json"])
        self.assertEqual(result["metadata"]["candidate_edges"], 1)
        self.assertEqual(result["metadata"]["editorial_review"]["mechanical_candidates"], 2)
        self.assertEqual(result["metadata"]["editorial_review"]["editorially_excluded"], 1)
        self.assertEqual(result["metadata"]["editorial_review"]["pending_candidates"], 1)
        self.assertEqual(result_graph["metadata"]["missing_zhihu_reference_candidates"], 2)
        self.assertEqual(result_graph["metadata"]["articles_with_missing_zhihu_references"], 2)
        self.assertEqual(result_graph["metadata"]["editorial_pending_candidates"], 1)
        self.assertEqual(result_graph["metadata"]["editorial_pending_source_articles"], 1)
        self.assertNotIn("pending_review", result_catalog["articles"][0]["references"][0])
        self.assertNotIn("pending_review_reason", result_catalog["articles"][0]["references"][0])
        self.assertTrue(result_catalog["articles"][1]["references"][0]["pending_review"])
        self.assertEqual(result_catalog["articles"][1]["references"][0]["pending_review_reason"], "需要决定")
        self.assertNotIn("pending_review", result_catalog["articles"][3]["references"][0])
        self.assertEqual(json.loads(updates["data/validation.json"])["references"], result_graph["metadata"])
        self.assertEqual(json.loads(updates["data/missing-zhihu-reference-review-method.json"])["editorial_review"], result["metadata"]["editorial_review"])

    def test_candidate_drift_is_rejected(self):
        with (self.root / "data/missing-zhihu-references.json").open("a", encoding="utf-8") as stream:
            stream.write(" ")
        with self.assertRaisesRegex(ValueError, "Candidate file changed"):
            self.prepare()

    def test_source_drift_is_rejected(self):
        (self.archive / "article/1.json").write_text("changed")
        with self.assertRaisesRegex(ValueError, "source hash differs"):
            self.prepare()

    def test_embedded_catalog_drift_is_rejected(self):
        html = (self.root / "index.html").read_text(encoding="utf-8").replace('"keep": true', '"keep": false').replace('"summary":', '"changed_summary":', 1)
        (self.root / "index.html").write_text(html, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Embedded catalog differs"):
            self.prepare()

    def test_unverifiable_audit_cannot_remove_candidates(self):
        self.audit["results"][0]["status"] = "unverifiable"
        self.write("data/missing-zhihu-reference-recheck.json", self.audit)
        with self.assertRaisesRegex(ValueError, "manual handling"):
            self.prepare()

    def test_wrong_target_evidence_is_rejected(self):
        self.audit["results"][0]["direct_link_evidence"][0]["url"] = "https://zhuanlan.zhihu.com/p/999"
        self.write("data/missing-zhihu-reference-recheck.json", self.audit)
        with self.assertRaisesRegex(ValueError, "mismatched direct-link"):
            self.prepare()

    def test_dirty_archive_is_rejected(self):
        with patch.object(flow, "git_value", side_effect=["head", " M article/1.json"]):
            with self.assertRaisesRegex(ValueError, "tracked modifications"):
                flow.prepare_updates(self.root, self.archive)


if __name__ == "__main__":
    unittest.main()
