"""Verify reviewed answer links update the index, catalog and page together."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import answer_reference_update as flow
from answer_archive_delta import ACCOUNT_NAMES
from summary_workflow import DATA_PATTERN, canonical_hash, digest, file_hash, read_json, write_json


OUTPUTS = ("data/answer-references.json", "data/catalog.json", "data/validation.json", "index.html")
PASSAGE = "先独立找出结论，再用实测结果核验预测。"
ARTICLE_HTML = {"1": f"<p>{PASSAGE}</p>", "2": "<p>这是另一篇译文的正文。</p>"}
TITLES = {"1": "如何用实测结果独立核验预测", "2": "阅读与反馈"}
PARAPHRASE = "我的做法是先自行作出可检验的判断，接着用实测数据比对。"


class AnswerReferenceUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "archive"
        self.archive.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "user.name", "Fixture")
        old_link = f'<p><a href="https://zhuanlan.zhihu.com/p/1">{TITLES["1"]}</a></p>'
        old_answer = self.write_answer("10", old_link, vote_count=5)
        self.write_answer("11", ARTICLE_HTML["2"], vote_count=3)
        for article_id, body in ARTICLE_HTML.items():
            write_json(self.archive / f"article/{article_id}.json", {
                "id": int(article_id), "title": TITLES[article_id], "content": body,
            })
        self.base = self.commit("Archive base")
        self.write_answer("10", old_answer["content"], vote_count=9)
        self.write_answer("20", (
            f'<blockquote>{PASSAGE}</blockquote><p>整理引用'
            '<sup data-draft-type="reference" data-url="https://zhuanlan.zhihu.com/p/1" '
            f'data-text="{TITLES["1"]}"></sup>，以及'
            '<a href="https://zhuanlan.zhihu.com/p/2">阅读与反馈</a>。</p>'
        ), vote_count=2, title="新增回答 <b>和标题</b>")
        self.write_answer("21", "<p>这个回答没有引用译文。</p>", vote_count=1)
        self.tip = self.commit("Add two answers and update historical votes")

        self.old_edge = {
            "answer_id": "10", "article_id": "1", "via": ["zhihu"], "evidence": ["link"], "level": 1,
            "classification": {
                "evidence": "仅链接标题。",
                "answer_evidence": TITLES["1"], "article_evidence": TITLES["1"],
                "reviewer_model": "historical-fixture", "reasoning_effort": "max", "review_status": "reviewed",
            },
        }
        self.old_answer = {
            "id": "10", "question_id": "31", "title": old_answer["question"]["title"], "author": "Thoughts Memo",
            "created_time": old_answer["created_time"], "voteup_count": 5, "date": "2026-10-01",
            "url": "https://www.zhihu.com/question/31/answer/10",
            "archive_url": "https://l-m-sherlock.github.io/ZhiHuArchive/10.html",
        }
        answer_metadata = {
            "schema_version": 1, "answers_scanned": 2, "answers_with_citations": 1, "translations_cited": 1,
            "citation_pairs": 1, "skipped_other_authors": 0,
            "earliest_answer_date": self.old_answer["date"], "latest_answer_date": self.old_answer["date"],
            "classification_counts": {"1": 1, "2": 0, "3": 0, "unclassified": 0}, "new_nonlink_pairs": 0,
            "reviewed_pairs": 1, "submitted_pairs": 1, "unreviewed_submitted_pairs": 0,
            "checks": {"unique_pairs": True, "targets_exist": True, "counts_match": True,
                       "all_direct_links_retained": True},
        }
        self.catalog = {
            "metadata": {"included_articles": 2, "answer_references": answer_metadata,
                         "summary_reviews": {"run_id": "historical-summary-review"}},
            "articles": [
                {
                    "id": article_id, "title": TITLES[article_id], "summary": f"此前完成的总结 {article_id}",
                    "source_path": f"article/{article_id}.json",
                    "source_sha256": file_hash(self.archive / f"article/{article_id}.json"),
                    "content_sha256": digest(ARTICLE_HTML[article_id]),
                    "cited_by_answer_ids": ["10"] if article_id == "1" else [],
                    "cited_by_answer_count": int(article_id == "1"),
                    "answer_reference_levels": {
                        "10": {"level": 1, "evidence": self.old_edge["classification"]["evidence"], "direct": True}
                    } if article_id == "1" else {},
                }
                for article_id in ("1", "2")
            ],
        }
        write_json(self.root / "data/answer-references.json", {
            "metadata": answer_metadata, "answers": {"10": self.old_answer}, "edges": [self.old_edge],
        })
        write_json(self.root / "data/catalog.json", self.catalog)
        write_json(self.root / "data/validation.json", self.catalog["metadata"])
        self.html_prefix = '<!doctype html><script id="catalog-data" type="application/json">'
        self.html_suffix = '</script><script>const keep = "search";</script>\n'
        embedded = {
            "records": self.catalog["articles"], "report": self.catalog["metadata"],
            "answerIndex": {"10": self.old_answer}, "exportOrder": ["2", "1"],
        }
        (self.root / "index.html").write_text(
            self.html_prefix + json.dumps(embedded, ensure_ascii=False) + self.html_suffix, encoding="utf-8",
        )
        self.plan = flow.prepare(self.root, self.archive, self.base, self.tip, changes="added")
        self.reviews = self.review_package()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.archive), *args], text=True).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def write_answer(self, answer_id, body, *, vote_count, title="历史回答"):
        author_id = next(key for key, value in ACCOUNT_NAMES.items() if value == "Thoughts Memo")
        record = {
            "id": int(answer_id), "content": body, "voteup_count": vote_count,
            "author": {"id": author_id, "name": "Thoughts Memo"},
            "question": {"id": 31, "title": title}, "created_time": 1790784000 + int(answer_id) * 86400 - 864000,
        }
        write_json(self.archive / f"answer/{answer_id}.json", record)
        return record

    def review_package(self, plan=None):
        plan = self.plan if plan is None else plan
        articles = {article["id"]: article for article in self.catalog["articles"]}
        reviews = []
        for candidate in plan["candidates"]:
            answer_id, article_id = candidate["answer_id"], candidate["article_id"]
            has_excerpt = answer_id == "20" and article_id == "1"
            excerpt = PASSAGE if has_excerpt else TITLES[article_id]
            reviews.append({
                "answer_id": answer_id, "article_id": article_id,
                "source_sha256": candidate["source_sha256"], "content_sha256": candidate["content_sha256"],
                "article_source_sha256": articles[article_id]["source_sha256"],
                "article_content_sha256": digest(ARTICLE_HTML[article_id]),
                "level": 3 if has_excerpt else 1,
                "classification": {
                    "answer_evidence": excerpt, "article_evidence": excerpt,
                    "evidence": "答主正文独立节选译文原句。" if has_excerpt else "正文仅链接目标译文标题。",
                    "reviewer": "fixture-reviewer", "reviewer_model": "fixture-model",
                    "reasoning_effort": "max", "review_status": "reviewed",
                },
            })
        return {"schema_version": 1, "plan_sha256": canonical_hash(plan), "reviews": reviews, "pair_decisions": []}

    def output_bytes(self):
        return {relative: (self.root / relative).read_bytes() for relative in OUTPUTS}

    def inferred_review(self, tip, answer_id, article_id, answer_quote, article_quote, level):
        raw = subprocess.check_output(["git", "-C", str(self.archive), "show", f"{tip}:answer/{answer_id}.json"])
        article = next(row for row in self.catalog["articles"] if row["id"] == article_id)
        return {
            "answer_id": answer_id, "article_id": article_id,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": digest(json.loads(raw)["content"]),
            "article_source_sha256": article["source_sha256"], "article_content_sha256": digest(ARTICLE_HTML[article_id]),
            "level": level,
            "classification": {
                "answer_evidence": answer_quote, "article_evidence": article_quote,
                "evidence": "人工核对回答正文明示的题名或取材内容。",
                "reviewer": "fixture-reviewer", "reviewer_model": "fixture-model",
                "reasoning_effort": "max", "review_status": "reviewed",
            },
        }

    def test_added_answers_preserve_historical_index_and_update_all_outputs(self):
        self.assertEqual({row["id"] for row in self.plan["answers"].values()}, {"20", "21"})
        before = self.output_bytes()
        flow.check(self.root, self.archive, self.plan, self.reviews)
        self.assertEqual(self.output_bytes(), before)
        flow.apply(self.root, self.archive, self.plan, self.reviews)

        published = read_json(self.root / "data/answer-references.json")
        self.assertEqual(published["answers"]["10"], self.old_answer)
        self.assertEqual(set(published["answers"]), {"10", "20"})
        self.assertIn(self.old_edge, published["edges"])
        self.assertEqual([(row["article_id"], row["answer_id"], row["level"]) for row in published["edges"]],
                         [("1", "10", 1), ("1", "20", 3), ("2", "20", 1)])
        self.assertEqual(published["metadata"]["citation_pairs"], 3)
        self.assertEqual(published["metadata"]["classification_counts"], {"1": 2, "2": 0, "3": 1, "unclassified": 0})
        self.assertEqual(published["metadata"]["incremental_archive_scan"]["changes"], "added")
        self.assertIn("21", published["metadata"]["incremental_archive_scan"]["added_without_citations"])
        self.assertEqual(published["metadata"]["incremental_archive_scan"]["counts"]["deferred"], 1)
        catalog = read_json(self.root / "data/catalog.json")
        self.assertEqual([a["summary"] for a in catalog["articles"]], [a["summary"] for a in self.catalog["articles"]])
        self.assertEqual(catalog["metadata"]["summary_reviews"], self.catalog["metadata"]["summary_reviews"])
        self.assertEqual(catalog["articles"][0]["cited_by_answer_ids"], ["20", "10"])
        self.assertEqual(catalog["articles"][0]["answer_reference_levels"], {
            "20": {"level": 3, "evidence": "答主正文独立节选译文原句。", "direct": True},
            "10": {"level": 1, "evidence": "仅链接标题。", "direct": True},
        })
        self.assertEqual(catalog["articles"][1]["cited_by_answer_ids"], ["20"])
        self.assertEqual(catalog["articles"][1]["cited_by_answer_count"], 1)
        html = (self.root / "index.html").read_text(encoding="utf-8")
        self.assertTrue(html.startswith(self.html_prefix))
        self.assertTrue(html.endswith(self.html_suffix))
        embedded = json.loads(DATA_PATTERN.search(html).group(2))
        self.assertEqual(embedded["exportOrder"], ["2", "1"])
        self.assertEqual(embedded["records"], catalog["articles"])
        self.assertEqual(embedded["answerIndex"], published["answers"])
        self.assertEqual(embedded["report"], catalog["metadata"])
        self.assertEqual(embedded["report"], read_json(self.root / "data/validation.json"))
        self.assertEqual(embedded["report"]["answer_references"], published["metadata"])

    def test_repeat_application_leaves_outputs_byte_identical(self):
        flow.apply(self.root, self.archive, self.plan, self.reviews)
        after = self.output_bytes()
        flow.apply(self.root, self.archive, self.plan, self.reviews)
        self.assertEqual(self.output_bytes(), after)

    def test_rejects_duplicate_or_incomplete_reviews_without_updates(self):
        before = self.output_bytes()
        for reviews in [self.reviews["reviews"][:1], self.reviews["reviews"] + self.reviews["reviews"][:1]]:
            with self.subTest(count=len(reviews)), self.assertRaises(ValueError):
                flow.apply(self.root, self.archive, self.plan, {**self.reviews, "reviews": reviews})
            self.assertEqual(self.output_bytes(), before)

    def test_rejects_stale_evidence_and_different_plan(self):
        before = self.output_bytes()
        original = self.reviews["reviews"][0]
        for field in ("source_sha256", "content_sha256", "article_source_sha256", "article_content_sha256"):
            package = copy.deepcopy(self.reviews)
            package["reviews"][0] = {**original, field: "b" * 64}
            with self.subTest(field=field), self.assertRaises(ValueError):
                flow.apply(self.root, self.archive, self.plan, package)
        mismatched_plan = copy.deepcopy(self.plan)
        mismatched_plan["candidates"][0]["occurrences"][0]["url"] = "https://example.test/"
        package = {**self.reviews, "plan_sha256": canonical_hash(mismatched_plan)}
        with self.assertRaises(ValueError):
            flow.apply(self.root, self.archive, mismatched_plan, package)
        self.assertEqual(self.output_bytes(), before)

    def test_rejects_annotation_title_as_excerpt_evidence(self):
        before = self.output_bytes()
        package = copy.deepcopy(self.reviews)
        package["reviews"][0]["classification"]["answer_evidence"] = TITLES["1"]
        package["reviews"][0]["classification"]["article_evidence"] = TITLES["1"]
        with self.assertRaises(ValueError):
            flow.apply(self.root, self.archive, self.plan, package)
        self.assertEqual(self.output_bytes(), before)

    def test_rejects_excerpt_only_present_in_generated_citation(self):
        before = self.output_bytes()
        for generated_citation in (
            '<sup data-draft-type="reference" data-url="https://zhuanlan.zhihu.com/p/1">'
            f'<span>{PASSAGE}</span></sup>',
            f'<a class="LinkCard" href="https://zhuanlan.zhihu.com/p/1"><span>{PASSAGE}</span></a>',
        ):
            with self.subTest(generated_citation=generated_citation):
                self.write_answer("20", f"<p>我在此添加参考链接。{generated_citation}</p>", vote_count=2)
                tip = self.commit("Put the excerpt in generated citation markup only")
                plan = flow.prepare(self.root, self.archive, self.base, tip, changes="added")
                with self.assertRaisesRegex(ValueError, "Answer evidence does not match"):
                    flow.apply(self.root, self.archive, plan, self.review_package(plan))
                self.assertEqual(self.output_bytes(), before)

    def test_aborts_when_embedded_data_is_stale(self):
        path = self.root / "index.html"
        html = path.read_text(encoding="utf-8")
        match = DATA_PATTERN.search(html)
        embedded = json.loads(match.group(2))
        embedded["answerIndex"]["10"]["voteup_count"] = 12
        path.write_text(html[:match.start(2)] + json.dumps(embedded) + html[match.end(2):], encoding="utf-8")
        before = self.output_bytes()
        with self.assertRaises(ValueError):
            flow.apply(self.root, self.archive, self.plan, self.reviews)
        self.assertEqual(self.output_bytes(), before)

    def test_full_plan_can_refresh_historical_answer_metadata(self):
        plan = flow.prepare(self.root, self.archive, self.base, self.tip)
        self.assertEqual(plan["answers"]["10"]["change"], "metadata_only")
        flow.apply(self.root, self.archive, plan, self.review_package(plan))
        data = read_json(self.root / "data/answer-references.json")
        self.assertEqual(data["answers"]["10"]["voteup_count"], 9)
        self.assertIn(self.old_edge, data["edges"])
        self.assertEqual(data["metadata"]["incremental_archive_scan"]["changes"], "all")

    def test_removed_link_requires_explicit_decision_and_preserves_history(self):
        self.write_answer("10", "<p>此版删去了历史译文链接。</p>", vote_count=9)
        tip = self.commit("Remove a historical link from the archived answer")
        plan = flow.prepare(self.root, self.archive, self.base, tip)
        self.assertEqual(plan["answers"]["10"]["removed_direct_pairs"], ["1"])
        package = self.review_package(plan)
        before = self.output_bytes()
        with self.assertRaises(ValueError):
            flow.apply(self.root, self.archive, plan, package)
        self.assertEqual(self.output_bytes(), before)

        package["pair_decisions"] = [{"answer_id": "10", "article_id": "1", "decision": "remove",
                                      "reason": "核实新正文已删除此链接与相关引文。"}]
        flow.apply(self.root, self.archive, plan, package)
        data = read_json(self.root / "data/answer-references.json")
        self.assertNotIn("10", data["answers"])
        self.assertNotIn(self.old_edge, data["edges"])
        receipt = data["metadata"]["incremental_archive_scan"]
        self.assertEqual(receipt["pairs_removed"], 1)
        self.assertEqual(receipt["pair_dispositions"][0]["previous_edge"], self.old_edge)
        after = self.output_bytes()
        flow.apply(self.root, self.archive, plan, package)
        self.assertEqual(self.output_bytes(), after)

    def test_failed_write_rolls_back_all_outputs(self):
        before = self.output_bytes()
        replace = Path.replace

        def fail_second_write(temporary, target):
            if target == self.root / "data/catalog.json" and not temporary.name.startswith(".summary-backup-"):
                raise OSError("simulated replacement failure")
            return replace(temporary, target)

        with patch.object(Path, "replace", new=fail_second_write), self.assertRaises(OSError):
            flow.apply(self.root, self.archive, self.plan, self.reviews)
        self.assertEqual(self.output_bytes(), before)

    def test_repeat_application_rejects_aligned_but_stale_catalog_evidence(self):
        flow.apply(self.root, self.archive, self.plan, self.reviews)
        catalog_path = self.root / "data/catalog.json"
        catalog = read_json(catalog_path)
        catalog["articles"][0]["source_sha256"] = "b" * 64
        write_json(catalog_path, catalog)
        html_path = self.root / "index.html"
        html = html_path.read_text(encoding="utf-8")
        match = DATA_PATTERN.search(html)
        embedded = json.loads(match.group(2))
        embedded["records"] = catalog["articles"]
        payload = json.dumps(embedded, ensure_ascii=False)
        html_path.write_text(html[:match.start(2)] + payload + html[match.end(2):], encoding="utf-8")
        before = self.output_bytes()
        with self.assertRaisesRegex(ValueError, "Stale answer or article evidence"):
            flow.apply(self.root, self.archive, self.plan, self.reviews)
        self.assertEqual(self.output_bytes(), before)

    def test_reviewed_inferred_relationships_preserve_direct_pairs_and_reapply(self):
        title = TITLES["1"]
        self.write_answer("21", f"<p>{PASSAGE}</p>", vote_count=1)
        self.write_answer("22", f"<p>详见《{title}》一文。</p>", vote_count=4)
        self.write_answer("23", f"<p>{PARAPHRASE}</p>", vote_count=4)
        tip = self.commit("Archive three answers with unlinked textual evidence")
        plan = flow.prepare(self.root, self.archive, self.base, tip, changes="added")
        bundle = self.review_package(plan)
        second = self.inferred_review(tip, "23", "1", PARAPHRASE, PASSAGE, 2)
        bundle["inferred_reviews"] = [
            self.inferred_review(tip, "21", "1", PASSAGE, PASSAGE, 3),
            self.inferred_review(tip, "22", "1", title, title, 1),
            second,
        ]
        before = self.output_bytes()
        with self.assertRaises(ValueError):
            flow.apply(self.root, self.archive, plan, bundle)
        self.assertEqual(self.output_bytes(), before)

        second["classification"]["contextual_attribution"] = "答主化用先独立预判、再用客观数据核验的两步顺序。"
        for offset, field, stale in (
            (0, "source_sha256", "c" * 64),
            (0, "article_evidence", PASSAGE[:-2]),
            (1, "answer_evidence", title[:-1]),
            (2, "contextual_attribution", PARAPHRASE),
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(bundle)
                review = invalid["inferred_reviews"][offset]
                record = review if field == "source_sha256" else review["classification"]
                record[field] = stale
                with self.assertRaises(ValueError):
                    flow.apply(self.root, self.archive, plan, invalid)
                self.assertEqual(self.output_bytes(), before)
        flow.apply(self.root, self.archive, plan, bundle)
        index = read_json(self.root / "data/answer-references.json")
        inferred = {edge["answer_id"]: edge for edge in index["edges"] if edge["via"] == ["inferred"]}
        self.assertEqual({key: edge["level"] for key, edge in inferred.items()}, {"21": 3, "22": 1, "23": 2})
        self.assertTrue(all(edge["evidence"] == ["reviewed_text"] for edge in inferred.values()))
        receipt = index["metadata"]["incremental_archive_scan"]
        self.assertEqual(receipt["inferred_pairs_added"], 3)
        self.assertEqual(receipt["inferred_classification_counts"], {"1": 1, "2": 1, "3": 1, "unclassified": 0})
        self.assertEqual(receipt["added_without_citations"], [])
        self.assertIn(self.old_edge, index["edges"])
        data = read_json(self.root / "data/catalog.json")
        self.assertFalse(data["articles"][0]["answer_reference_levels"]["21"]["direct"])
        self.assertTrue(data["articles"][0]["answer_reference_levels"]["20"]["direct"])
        after = self.output_bytes()
        flow.apply(self.root, self.archive, plan, bundle)
        self.assertEqual(self.output_bytes(), after)

    def test_inferred_pair_requires_scope_and_cannot_replace_existing_or_direct_pair(self):
        before = self.output_bytes()
        unchanged_text = "这是另一篇译文的正文。"
        existing = self.inferred_review(self.tip, "10", "1", TITLES["1"], TITLES["1"], 1)
        direct = self.inferred_review(self.tip, "20", "1", PASSAGE, PASSAGE, 3)
        outside_scope = self.inferred_review(self.tip, "11", "2", unchanged_text, unchanged_text, 3)
        for review in (existing, direct, outside_scope):
            bundle = {**self.reviews, "inferred_reviews": [review]}
            with self.subTest(answer=review["answer_id"]), self.assertRaises(ValueError):
                flow.apply(self.root, self.archive, self.plan, bundle)
            self.assertEqual(self.output_bytes(), before)

        plan = flow.prepare(self.root, self.archive, self.base, self.tip, new_article_ids=("2",))
        self.assertNotIn("11", plan["answers"])
        bundle = {**self.review_package(plan), "inferred_reviews": [outside_scope]}
        absent = {**bundle, "inferred_reviews": [{**outside_scope, "answer_id": "99"}]}
        with self.assertRaises(ValueError):
            flow.apply(self.root, self.archive, plan, absent)
        self.assertEqual(self.output_bytes(), before)

        flow.apply(self.root, self.archive, plan, bundle)
        index = read_json(self.root / "data/answer-references.json")
        self.assertIn("11", index["answers"])
        inferred = next(edge for edge in index["edges"] if edge["answer_id"] == "11")
        self.assertEqual((inferred["article_id"], inferred["level"], inferred["via"]), ("2", 3, ["inferred"]))


if __name__ == "__main__":
    unittest.main()
