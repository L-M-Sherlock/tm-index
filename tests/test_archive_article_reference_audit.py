"""Verify pinned article-link evidence, explicit removal and synchronized output."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import archive_article_reference_audit as audit


class ArchiveArticleReferenceAuditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.archive = Path(directory.name) / "archive"
        self.root = Path(directory.name) / "site"
        (self.archive / "article").mkdir(parents=True)
        (self.root / "data").mkdir(parents=True)
        self.git("init", "-q")
        self.write_article("1", '<a href="https://zhuanlan.zhihu.com/p/2">引用二</a>')
        self.write_article("2", '<a href="https://l-m-sherlock.github.io/ZhiHuArchive/4.html">新稿</a>')
        self.write_article("3", "<p>译文三</p>")
        self.base = self.commit("baseline")
        self.write_article("1", '<p><sup data-draft-type="reference" data-url="https://zhuanlan.zhihu.com/p/3" '
                           'data-text="原生附注">[1]</sup><a href="https://example.com/shared/">原作</a>'
                           '<a href="https://example.com/sole">另一原作</a>'
                           '<a href="https://zhuanlan.zhihu.com.example.org/p/4">伪装链接</a></p>')
        self.write_article("3", "<p>译文三</p>", voteup=2)
        self.write_article("4", "<p>译文四</p>")
        self.tip = self.commit("updated")
        old = {"source_id": "1", "target_id": "2", "via": ["zhihu"], "match_quality": ["direct"],
               "link_texts": ["引用二"], "source_urls": ["https://zhuanlan.zhihu.com/p/2"], "occurrences": 1}
        graph = {"metadata": {"edges": 1, "method_occurrence_counts": {"zhihu": 1}}, "edges": [old]}
        catalog = {"metadata": {"references": graph["metadata"]}, "articles": [
            {"id": article_id, "title": f"译文{article_id}", "source_path": f"article/{article_id}.json",
             "summary": f"保留的摘要{article_id}", "references": [], "referenced_by": [],
             "reference_count": 0, "referenced_by_count": 0, "original_source_urls": []}
            for article_id in ("1", "2", "3", "4")
        ]}
        catalog["articles"][0]["references"] = [{"id": "2", "title": "译文2", "via": ["zhihu"]}]
        catalog["articles"][0]["reference_count"] = 1
        catalog["articles"][1]["referenced_by"] = [{"id": "1", "title": "译文1", "via": ["zhihu"]}]
        catalog["articles"][1]["referenced_by_count"] = 1
        catalog["articles"][1]["original_source_urls"] = ["https://example.com/sole"]
        for row in catalog["articles"][2:]:
            row["original_source_urls"] = ["https://example.com/shared"]
        self.write("data/references.json", graph)
        self.write_site(catalog)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.archive), *args], text=True, stderr=subprocess.PIPE).strip()

    def commit(self, message):
        self.git("add", "article")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def write_article(self, article_id, html, voteup=1):
        (self.archive / "article" / f"{article_id}.json").write_text(
            json.dumps({"id": article_id, "content": html, "voteup_count": voteup}), encoding="utf-8")

    def write(self, filename, data):
        (self.root / filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_site(self, catalog):
        self.write("data/catalog.json", catalog)
        self.write("data/validation.json", catalog["metadata"])
        payload = {"records": catalog["articles"], "report": catalog["metadata"],
                   "answerIndex": {"kept": True}, "exportOrder": ["3", "1", "2", "4"]}
        html = '<script id="catalog-data" type="application/json">' + json.dumps(payload) + "</script>"
        (self.root / "index.html").write_text(html, encoding="utf-8")

    def plan(self, base=None, tip=None):
        return audit.make_plan(self.root, self.archive, base or self.base, tip or self.tip)

    def reviews(self, plan, decision="remove"):
        return {
            "schema_version": 1, "plan_sha256": audit.canonical_hash(plan),
            "removed_direct": [{"source_id": "1", "target_id": "2", "method": "zhihu", "decision": decision,
                                "reason": "前后对照显示已删除链接，未发现独立公开版本仍有此引用。"}],
            "original_candidates": [
                ({"id": item["id"], "decision": "dismiss", "reason": "该外链对应多篇译文，语境不足以确定目标。"}
                 if len(item["targets"]) > 1 else
                 {"id": item["id"], "decision": "add", "target_id": item["targets"][0],
                  "reason": "文中明确链接至该译文唯一标注的原作。"})
                for item in plan["original_candidates"]
            ],
        }

    def test_pinned_links_native_notes_backward_direction_and_ambiguity(self):
        plan = self.plan()
        self.assertEqual(plan["new_target_ids"], ["4"])
        self.assertEqual(plan["changed_sources"], ["1", "4"])
        self.assertEqual({row["id"] for row in plan["sources"]}, {"1", "2", "4"})
        self.assertEqual({audit._direct_key(item) for item in plan["direct"]},
                         {("1", "2", "zhihu"), ("1", "3", "zhihu"), ("2", "4", "archive")})
        new_link = next(item for item in plan["direct"] if item["target_id"] == "3")
        self.assertEqual(new_link["after"][0]["label"], "原生附注")
        self.assertEqual([x["targets"] for x in plan["original_candidates"]], [["3", "4"], ["2"]])
        self.write_article("1", "<p>本机未提交的内容</p>")
        self.assertEqual(plan, self.plan())

    def test_apply_preserves_accepted_original_and_output_history(self):
        plan = self.plan()
        reviews = self.reviews(plan)
        receipt = audit.apply(self.root, self.archive, plan, reviews)
        self.assertEqual(receipt["direct_pairs_added"], 2)
        self.assertEqual(receipt["direct_pairs_removed"], 1)
        self.assertEqual(receipt["original_candidates_reviewed"], 2)
        self.assertEqual(receipt, audit.check(self.root, self.archive, plan, reviews))
        self.assertEqual(receipt, audit.apply(self.root, self.archive, plan, reviews))
        graph = audit.read_json(self.root / "data/references.json")
        by_pair = {(e["source_id"], e["target_id"]): e["via"] for e in graph["edges"]}
        self.assertEqual(by_pair, {("1", "2"): ["original"], ("1", "3"): ["zhihu"], ("2", "4"): ["archive"]})
        site = audit.read_json(self.root / "data/catalog.json")
        self.assertEqual(site["articles"][0]["summary"], "保留的摘要1")
        html = (self.root / "index.html").read_text()
        payload = json.loads(audit.DATA_PATTERN.search(html)[2])
        self.assertEqual(payload["answerIndex"], {"kept": True})
        self.assertEqual(payload["exportOrder"], ["3", "1", "2", "4"])

    def test_disappearance_requires_review_and_can_retain_historical_edge(self):
        plan = self.plan()
        reviews = self.reviews(plan, "retain")
        missing = dict(reviews, removed_direct=[])
        with self.assertRaisesRegex(ValueError, "Direct removal decisions"):
            audit.apply(self.root, self.archive, plan, missing)
        receipt = audit.apply(self.root, self.archive, plan, reviews)
        self.assertEqual(receipt["direct_pairs_removed"], 0)
        graph = audit.read_json(self.root / "data/references.json")
        self.assertEqual(graph["edges"][0]["via"], ["original", "zhihu"])

    def test_direct_removal_preserves_independent_original_evidence(self):
        graph = audit.read_json(self.root / "data/references.json")
        edge = graph["edges"][0]
        edge["via"] = ["original", "zhihu"]
        edge["match_quality"] = ["direct", "exact"]
        edge["source_urls"].append("https://example.com/sole")
        edge["occurrences"] = 2
        graph["metadata"]["method_occurrence_counts"]["original"] = 1
        self.write("data/references.json", graph)
        catalog = audit.read_json(self.root / "data/catalog.json")
        catalog["metadata"]["references"] = graph["metadata"]
        catalog["articles"][0]["references"][0]["via"] = edge["via"]
        catalog["articles"][1]["referenced_by"][0]["via"] = edge["via"]
        self.write_site(catalog)

        plan = self.plan()
        audit.apply(self.root, self.archive, plan, self.reviews(plan))
        first = audit.read_json(self.root / "data/references.json")["edges"][0]
        self.assertEqual(first["via"], ["original"])
        self.assertEqual(first["source_urls"], ["https://example.com/sole"])
        self.assertEqual(first["match_quality"], ["exact"])
        self.assertEqual(first["occurrences"], 1)

    def test_stale_index_or_archived_evidence_is_rejected(self):
        plan = self.plan()
        changed = dict(plan, changed_sources=[])
        with self.assertRaisesRegex(ValueError, "Archive reference evidence"):
            audit.apply(self.root, self.archive, changed, self.reviews(changed))
        graph = audit.read_json(self.root / "data/references.json")
        graph["edges"][0]["link_texts"] = ["独立编辑修改"]
        self.write("data/references.json", graph)
        with self.assertRaisesRegex(ValueError, "changed since preparation"):
            audit.apply(self.root, self.archive, plan, self.reviews(plan))

    def test_empty_delta_has_checked_receipt(self):
        plan = self.plan(self.tip, self.tip)
        reviews = {"schema_version": 1, "plan_sha256": audit.canonical_hash(plan),
                   "removed_direct": [], "original_candidates": []}
        self.assertEqual(audit.apply(self.root, self.archive, plan, reviews)["changed_sources"], 0)
        self.assertEqual(audit.check(self.root, self.archive, plan, reviews)["direct_pairs_added"], 0)


if __name__ == "__main__":
    unittest.main()
