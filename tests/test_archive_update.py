import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import archive_update as archive_update
import archive_update_install
import answer_reference_update
from answer_archive_delta import ACCOUNT_NAMES
from summary_workflow import canonical_hash, digest, json_text, read_json


class ArchiveUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.archive = self.path / "archive"
        self.archive.mkdir()
        self.root = self.path / "site"
        self.root.mkdir()
        self.git("init", "-q")
        self.article = {
            "id": 1, "title": "A translation", "author": {"name": "Thoughts Memo", "url_token": "fixture"},
            "created": 1730000000, "voteup_count": 4, "content": "<p>Existing translation content.</p>",
        }
        self.answer = {
            "id": 2, "question": {"id": 8, "title": "A question"},
            "author": {"name": "Thoughts Memo", "url_token": "fixture"},
            "created_time": 1730000000, "voteup_count": 4, "content": "<p>Answer content.</p>",
        }
        self.write_archive("article", self.article)
        self.write_archive("answer", self.answer)
        self.base = self.commit("base")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.archive), *args], text=True).strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("-c", "user.name=Archive Fixture", "-c", "user.email=fixture@example.test",
                 "commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json_text(value), encoding="utf-8")

    def write_archive(self, kind, value):
        self.write_json(self.archive / kind / f"{value['id']}.json", value)

    def make_site(self):
        body = "Existing translation content."
        source = (self.archive / "article/1.json").read_bytes()
        row = {
            "id": "1", "decision": "translation", "summary": "A verified existing summary.",
            "keywords": ["topic"], "author_entities": [{"name": "The author"}], "original_author_status": "resolved",
            "references": [], "reference_count": 0, "referenced_by": [], "referenced_by_count": 0,
            "cited_by_answer_ids": [], "cited_by_answer_count": 0, "answer_reference_levels": {},
            "source_path": "article/1.json", "source_sha256": hashlib.sha256(source).hexdigest(),
            "content_sha256": digest(self.article["content"]), "read_chars": len(body),
            **archive_update.display_fields("article", self.article),
        }
        graph = {"metadata": {"nodes": 1, "edges": 0}, "edges": []}
        answers = {
            "metadata": {"answers_with_citations": 0, "translations_cited": 0, "citation_pairs": 0,
                         "classification_counts": {"1": 0, "2": 0, "3": 0, "unclassified": 0}},
            "answers": {}, "edges": [],
        }
        metadata = {"included_articles": 1, "references": graph["metadata"], "answer_references": answers["metadata"]}
        catalog = {"metadata": metadata, "articles": [row]}
        for name, value in {
            "catalog.json": catalog, "validation.json": metadata, "references.json": graph,
            "answer-references.json": answers, "authors.json": [{"name": "The author", "article_ids": ["1"],
                                                                  "article_count": 1}],
            "keywords.json": [{"keyword": "topic", "count": 1}],
            "summary-reviews.json": {"entries": []}, "summary-revisions.json": {"entries": []},
        }.items():
            self.write_json(self.root / "data" / name, value)
        self.write_site_outputs(catalog, answers)
        author = {key: row[key] for key in ("id", "author_entities", "original_author_status")}
        (self.root / "data/original-authors.jsonl").write_text(json.dumps(author) + "\n", encoding="utf-8")
        return catalog, answers

    def write_site_outputs(self, catalog, answers):
        self.write_json(self.root / "data/catalog.json", catalog)
        self.write_json(self.root / "data/validation.json", catalog["metadata"])
        self.write_json(self.root / "data/answer-references.json", answers)
        payload = {"records": catalog["articles"], "report": catalog["metadata"],
                   "answerIndex": answers["answers"], "exportOrder": ["1"]}
        prefix = '<script id="catalog-data" type="application/json">'
        (self.root / "index.html").write_text(prefix + json.dumps(payload) + "</script>", encoding="utf-8")

    def plan(self, message):
        tip = self.commit(message)
        return archive_update.make_plan(self.archive, self.base, tip)

    def decisions(self, plan, items=None):
        return {"schema_version": 1, "plan_sha256": canonical_hash(plan), "items": items or []}

    def test_distinguishes_new_deleted_display_and_visible_changes(self):
        self.write_archive("article", {**self.article, "voteup_count": 15})
        self.write_archive("article", {**self.article, "id": 7})
        self.write_archive("answer", {**self.answer, "content": '<p class="updated">Answer content.</p>'})
        self.write_archive("answer", {**self.answer, "id": 9, "content": "<p>A different answer.</p>"})
        self.commit("prepare deletable prior entry")
        previous = self.git("rev-parse", "HEAD")
        self.write_archive("article", {**self.article, "voteup_count": 16})
        self.write_archive("article", {**self.article, "id": 7, "content": "<p>A replacement translation.</p>"})
        self.write_archive("article", {**self.article, "id": 12})
        self.write_archive("answer", {**self.answer, "content": '<p class="other">Answer content.</p>'})
        (self.archive / "answer/9.json").unlink()
        latest = self.commit("change both namespaces")

        plan = archive_update.make_plan(self.archive, previous, latest)
        changes = {(r["kind"], r["id"]): r for r in plan["changes"]}
        self.assertEqual(plan["archive_tip"], latest)
        self.assertEqual(changes["article", "1"]["change"], "metadata")
        self.assertEqual(changes["article", "1"]["display_changes"], ["voteup_count"])
        self.assertFalse(changes["article", "1"]["manual_decision"])
        self.assertTrue(changes["article", "7"]["visible_text_changed"])
        self.assertEqual(changes["article", "12"]["change"], "added")
        self.assertIsNone(changes["article", "12"]["before"])
        self.assertFalse(changes["answer", "2"]["visible_text_changed"])
        self.assertEqual(plan["counts"]["answer"]["html_only_changes"], 1)
        self.assertEqual(changes["answer", "9"]["change"], "removed")
        self.assertIsNone(changes["answer", "9"]["after"])

    def test_gate_rebuilds_frozen_plan_and_requires_every_manual_status(self):
        self.write_archive("answer", {**self.answer, "id": 3})
        plan = self.plan("new answer")
        tampered = {**plan, "changes": []}
        with self.assertRaisesRegex(ValueError, "differs from the pinned Git changes"):
            archive_update.check(self.root, self.archive, tampered, self.decisions(tampered))
        with self.assertRaisesRegex(ValueError, "Missing 1 archive update decisions"):
            archive_update.check(self.root, self.archive, plan, self.decisions(plan))

    def test_gate_requires_answer_stage_to_be_applied_and_full(self):
        self.write_archive("answer", {**self.answer, "id": 3})
        plan = self.plan("new answer")
        catalog, answers = self.make_site()
        answer_plan = {"changes": "all"}
        answer_reviews = {"reviews": []}
        partial = {
            "archive_base": plan["archive_base"], "archive_tip": plan["archive_tip"], "changes": "added",
            "new_article_ids": [], "plan_sha256": canonical_hash(answer_plan),
            "reviews_sha256": canonical_hash(answer_reviews),
        }
        answers["metadata"]["incremental_archive_scan"] = partial
        self.write_site_outputs(catalog, answers)
        decisions = self.decisions(plan, [
            {"kind": "answer", "id": "3", "status": "no_references", "reason": "The pinned answer lacks citations."},
        ])
        with self.assertRaisesRegex(ValueError, "unlinked relationships"):
            archive_update.check(self.root, self.archive, plan, decisions)
        decisions["unlinked_answer_review"] = {
            "scope": "The new answer", "method": "Review exact title and quote overlap",
            "result": "No verified matches", "limitations": "Paraphrase recall is incomplete",
        }
        with self.assertRaisesRegex(ValueError, "complete scan of the pinned archive update"):
            archive_update.check(self.root, self.archive, plan, decisions,
                                 answer_plan=answer_plan, answer_reviews=answer_reviews)
        claimed = {**partial, "changes": "all"}
        answers["metadata"]["incremental_archive_scan"] = claimed
        self.write_site_outputs(catalog, answers)
        with patch("answer_reference_update.check", return_value={**claimed, "modified": True}):
            with self.assertRaisesRegex(ValueError, "differs from the applied full scan"):
                archive_update.check(self.root, self.archive, plan, decisions,
                                     answer_plan=answer_plan, answer_reviews=answer_reviews)

    def test_new_translation_is_blocked_without_review_for_exact_version(self):
        self.write_archive("article", {**self.article, "id": 6})
        plan = self.plan("new translation")
        item = next(iter(plan["changes"]))
        row = {
            "source_path": item["path"],
            "source_sha256": item["after"]["source_sha256"],
            "content_sha256": item["after"]["content_sha256"],
            "read_chars": item["after"]["read_chars"],
            "summary": "An independent summary.",
            **item["after"]["display"],
        }
        with self.assertRaisesRegex(ValueError, "lack an accepted independent review"):
            archive_update.validate_article(item, {"status": "included"}, row, set())
        version = ("6", row["source_sha256"], row["content_sha256"], digest(row["summary"]))
        archive_update.validate_article(item, {"status": "included"}, row, {version})

    def test_reference_phase_requires_applied_evidence_for_all_new_targets(self):
        self.make_site()
        self.write_archive("article", {**self.article, "id": 7})
        plan = self.plan("new translation")
        article_plan = {"new_target_ids": ["7"]}
        reviews = {"items": []}
        graph_path = self.root / "data/references.json"
        graph = read_json(graph_path)
        receipt = {
            "archive_base": plan["archive_base"], "archive_tip": plan["archive_tip"],
            "plan_sha256": canonical_hash(article_plan), "reviews_sha256": canonical_hash(reviews),
            "new_article_ids": [],
        }
        graph["metadata"]["incremental_article_reference_scan"] = receipt
        self.write_json(graph_path, graph)
        with self.assertRaisesRegex(ValueError, "scan did not cover backlinks to all new translations"):
            archive_update.verify_article_scan(self.root, self.archive, plan, {"7"}, article_plan, reviews)
        receipt["new_article_ids"] = ["7"]
        self.write_json(graph_path, graph)
        with patch("archive_article_reference_audit.check", return_value={**receipt, "modified": True}):
            with self.assertRaisesRegex(ValueError, "differs from the applied scan"):
                archive_update.verify_article_scan(self.root, self.archive, plan, {"7"}, article_plan, reviews)

    def test_exclusion_needs_specific_pinned_evidence(self):
        self.write_archive("article", {**self.article, "id": 6})
        plan = self.plan("new original")
        decisions = self.decisions(plan, [
            {"kind": "article", "id": "6", "status": "excluded", "reason": "A publishing note."},
        ])
        with self.assertRaisesRegex(ValueError, "exclusion needs specific evidence"):
            archive_update.check(self.root, self.archive, plan, decisions)

    def test_site_check_catches_desynchronized_index(self):
        catalog, answers = self.make_site()
        archive_update.verify_site(self.root)
        catalog["articles"][0]["title"] = "New title"
        self.write_json(self.root / "data/catalog.json", catalog)
        with self.assertRaisesRegex(ValueError, "Embedded catalog differs"):
            archive_update.verify_site(self.root)
        self.write_site_outputs(catalog, answers)
        self.write_json(self.root / "data/keywords.json", [])
        with self.assertRaisesRegex(ValueError, "Keyword index differs"):
            archive_update.verify_site(self.root)

    def test_complete_is_idempotent_and_governs_next_explicit_base(self):
        self.make_site()
        (self.archive / "README.md").write_text("Rendered asset changes only.\n", encoding="utf-8")
        plan = self.plan("asset update")
        decisions = self.decisions(plan)
        with self.assertRaisesRegex(ValueError, "Pass --base"):
            archive_update.baseline(self.root, self.archive, None)
        self.assertEqual(archive_update.baseline(self.root, self.archive, self.base), self.base)
        result = archive_update.complete(self.root, self.archive, plan, decisions)
        state = read_json(self.root / archive_update.STATE)
        self.assertEqual(state["archive_commit"], plan["archive_tip"])
        self.assertEqual(state["completed_runs"], [result])
        self.assertEqual(result, archive_update.complete(self.root, self.archive, plan, decisions))
        self.assertEqual(read_json(self.root / archive_update.STATE), state)
        with self.assertRaisesRegex(ValueError, "Base differs"):
            archive_update.baseline(self.root, self.archive, self.base)

    def test_install_rejects_drift_and_is_idempotent(self):
        self.make_site()
        stage = self.path / "stage"
        shutil.copytree(self.root, stage)
        baseline = archive_update_install.snapshot(self.root)
        (self.archive / "README.md").write_text("Archive rendering update.", encoding="utf-8")
        plan = self.plan("asset update")
        archive_update.complete(stage, self.archive, plan, self.decisions(plan))
        index = self.root / "index.html"
        original = index.read_bytes()
        index.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "Live site changed"):
            archive_update_install.install(self.root, stage, baseline)
        self.assertFalse((self.root / archive_update.STATE).exists())
        index.write_bytes(original)
        stage_index = stage / "index.html"
        stage_index.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "differ from the completed update"):
            archive_update_install.install(self.root, stage, baseline)
        stage_index.write_bytes(original)
        self.assertTrue(archive_update_install.install(self.root, stage, baseline)["changed"])
        self.assertFalse(archive_update_install.install(self.root, stage, baseline)["changed"])
        self.assertEqual(archive_update.baseline(self.root, self.archive, None), plan["archive_tip"])

    def test_answer_stage_can_complete_a_pinned_backup_increment(self):
        account_id = next(key for key, name in ACCOUNT_NAMES.items() if name == "Thoughts Memo")
        self.answer["author"]["id"] = account_id
        self.write_archive("answer", self.answer)
        self.base = self.commit("Record the published answer account")
        catalog, _ = self.make_site()
        self.write_archive("answer", {
            **self.answer, "id": 3,
            "content": '<p>See <a href="https://zhuanlan.zhihu.com/p/1">A translation</a>.</p>',
        })
        plan = self.plan("add a citing answer")
        candidates = answer_reference_update.prepare(self.root, self.archive, self.base, plan["archive_tip"])
        [candidate] = candidates["candidates"]
        [article] = catalog["articles"]
        reviews = {
            "schema_version": 1, "plan_sha256": canonical_hash(candidates),
            "reviews": [{
                **{key: candidate[key] for key in ("answer_id", "article_id", "source_sha256", "content_sha256")},
                "article_source_sha256": article["source_sha256"],
                "article_content_sha256": article["content_sha256"], "level": 1,
                "classification": {
                    "evidence": "The answer links to the article by title.",
                    "answer_evidence": article["title"], "article_evidence": article["title"],
                    "reviewer": "fixture", "reviewer_model": "fixture", "reasoning_effort": "fixture",
                    "review_status": "reviewed",
                },
            }],
        }
        answer_reference_update.apply(self.root, self.archive, candidates, reviews)
        decisions = self.decisions(plan, [
            {"kind": "answer", "id": "3", "status": "indexed", "reason": "One verified direct reference."},
        ])
        decisions["unlinked_answer_review"] = {
            "scope": "New answer", "method": "Read the visible text",
            "result": "No additional relationships", "limitations": "Only the fixture corpus",
        }
        result = archive_update.complete(
            self.root, self.archive, plan, decisions, answer_plan=candidates, answer_reviews=reviews,
        )
        self.assertEqual(result["archive_tip"], plan["archive_tip"])
        rows, _, _, answer_edges = archive_update.verify_site(self.root)
        self.assertEqual(rows["1"]["cited_by_answer_ids"], ["3"])
        self.assertEqual(answer_edges["3", "1"]["level"], 1)


if __name__ == "__main__":
    unittest.main()
