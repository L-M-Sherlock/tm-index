"""Sampling integrity, diagnostic gates, weights, and read-only reporting."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("sampling_audit", Path(__file__).resolve().parents[1] / "scripts/summary_sampling_audit.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
base = audit.base


class SamplingAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "sources"
        self.source.mkdir()
        self.work = self.root / "work"
        self.summary = "作者主张理解核心机制，并依据及时反馈调整学习路径。"
        self.rows, source_rows = [], []
        for number, length in enumerate([5100, 6100, 7100, 8100, 10100, 11100, 15100], 1):
            id = str(number)
            body = ((self.summary + "\n作者还主张保障独立选择的条件。\n") * length)[:length]
            row = {"id": id, "title": "文章" + id, "read_chars": length, "summary": self.summary,
                   "publisher": "Thoughts Memo", "publisher_token": "L.M.Sherlock", "url": "https://example.com/" + id,
                   "archive_url": "https://example.com/archive/" + id, "source_path": "article/" + id + ".json",
                   "source_sha256": base.digest("raw-" + id), "content_sha256": base.digest("html-" + id)}
            self.rows.append(row)
            source_rows.append({"id": id, "source_sha256": row["source_sha256"], "content_sha256": row["content_sha256"], "text_sha256": base.digest(body), "text_chars": length})
            header = {key: row[key] for key in ("id", "title", "publisher", "publisher_token")}
            header["text_chars"] = length
            (self.source / (id + ".txt")).write_text(json.dumps(header, ensure_ascii=False) + "\n【完整可见正文开始】\n" + body + "\n【完整可见正文结束】\n")
        base.write_json(self.root / "data/catalog.json", {"metadata": {"included_articles": 7}, "articles": self.rows})
        base.write_json(self.root / "data/summary-revisions.json", {"entries": []})
        (self.root / "index.html").write_text("<p>Protected site</p>")
        (self.root / "archive-export.js").write_text("// protected export")
        self.trusted = self.root / "trusted.json"
        base.write_json(self.trusted, {"articles": source_rows})
        audit.prepare(self.root, self.source, self.trusted, None, self.work, "sample-test", "2026-11-01", per_stratum=1)
        self.manifest = base.read_json(self.work / "manifest.json")
        self.owners = {role: {id: actor for actor, ids in groups.items() for id in ids} for role, groups in self.manifest["assignments"].items()}
        for item in self.manifest["articles"]:
            self.make_results(item)

    def make_results(self, item, first_verdict="adequate", second_verdict="adequate"):
        id = item["id"]
        for role, verdict, directory in (("assessor", first_verdict, "assessments"), ("reviewer", second_verdict, "independent")):
            actor = self.owners[role][id]
            outline = {"id": id, "actor": actor, "model": base.MODEL, "reasoning_effort": base.EFFORT,
                       "source_sha256": item["source_sha256"], "body_sha256": item["body_sha256"],
                       "read_chunks": list(range(len(item["chunks"]))), "genre": "论说文",
                       "major_points": [{"key": "P1", "point": "理解与反馈的主张", "source_spans": [{"start": 0, "end": 25}]}]}
            outline_path = self.work / "outlines" / actor / (id + ".json")
            base.write_json(outline_path, outline)
            point = {**outline["major_points"][0], "coverage": "missing" if verdict == "undercovered" else "covered",
                     "summary_evidence": None if verdict == "undercovered" else self.summary, "reason": "主要机制的覆盖判断"}
            distortions = [{"claim": self.summary, "source_spans": [{"start": 0, "end": 25}], "reason": "本测试的方向错误证据"}] if verdict == "distorted" else []
            result = {key: outline[key] for key in ("id", "actor", "model", "reasoning_effort", "source_sha256", "body_sha256", "read_chunks", "genre")}
            result.update(summary_sha256=item["summary_sha256"], outline_sha256=audit.file_hash(outline_path), verdict=verdict,
                          rationale="逐点比较后得出的独立结论", major_points=[point], minor_omissions=[], distortions=distortions)
            base.write_json(self.work / directory / (id + ".json"), result)
        self.save_comparison(item, first_verdict != second_verdict)

    def save_comparison(self, item, disagree=False):
        id = item["id"]
        base.write_json(self.work / "comparisons" / (id + ".json"), {
            "id": id, "reviewer": self.owners["reviewer"][id],
            "first_assessment_sha256": audit.file_hash(self.work / "assessments" / (id + ".json")),
            "independent_assessment_sha256": audit.file_hash(self.work / "independent" / (id + ".json")),
            "needs_adjudication": disagree,
            "conflicts": [{"issue": "核心机制是否已经覆盖", "source_spans": [{"start": 0, "end": 25}], "reason": "双方独立判断覆盖程度不同"}] if disagree else [],
            "notes": ["已比较双方独立初判"]})

    def test_reproducible_numeric_id_sampling(self):
        a = audit.sampled_population(self.rows, 20261101, 1)
        b = audit.sampled_population(list(reversed(self.rows)), 20261101, 1)
        self.assertEqual(a, b)
        self.assertEqual([s["population_size"] for s in a[1]], [3, 1, 2, 1])
        self.assertEqual(len({r["id"] for _, r in a[2]}), 4)
        for index, row in a[2]:
            self.assertTrue(audit.BOUNDS[index] <= row["read_chars"] < audit.BOUNDS[index + 1])

    def test_oversampling_does_not_silently_change_design(self):
        with self.assertRaisesRegex(ValueError, "exceeds population"):
            audit.sampled_population(self.rows, 1, 2)

    def test_sample_replacement_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["articles"][0]["id"] = "not-sampled"
        base.write_json(self.work / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "Sample IDs"):
            audit.check(self.root, self.work)

    def test_stratum_cannot_be_relabelled_even_if_group_sizes_stay_equal(self):
        m = copy.deepcopy(self.manifest)
        m["articles"][0]["stratum"], m["articles"][1]["stratum"] = m["articles"][1]["stratum"], m["articles"][0]["stratum"]
        for item in m["articles"][:2]:
            base.write_json(self.work / "inputs" / item["id"] / "article.json", item)
        base.write_json(self.work / "manifest.json", m)
        with self.assertRaisesRegex(ValueError, "Article stratum"):
            audit.check(self.root, self.work)

    def test_deleted_manifest_does_not_allow_reusing_old_judgments(self):
        (self.work / "manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "fresh empty work"):
            audit.prepare(self.root, self.source, self.trusted, None, self.work, "new-run", "2026-11-02", per_stratum=1)

    def test_summary_or_site_change_is_rejected(self):
        (self.root / "index.html").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Protected file changed"):
            audit.check(self.root, self.work)

    def test_body_same_length_tampering_is_rejected(self):
        item = self.manifest["articles"][0]
        path = self.work / item["chunks"][0]["path"]
        path.write_text("伪" + path.read_text()[1:])
        with self.assertRaisesRegex(ValueError, "Body changed"):
            audit.check(self.root, self.work)

    def test_missing_source_keeps_original_sample_and_denominator(self):
        selected = self.manifest["articles"][0]["id"]
        (self.source / (selected + ".txt")).unlink()
        other = self.root / "gap-work"
        result = audit.prepare(self.root, self.source, self.trusted, None, other, "gap-test", "2026-11-01", per_stratum=1)
        m = base.read_json(other / "manifest.json")
        self.assertEqual(result["source_gaps"], 1)
        self.assertEqual([a["id"] for a in m["articles"]], [a["id"] for a in self.manifest["articles"]])
        records = [{"id": a["id"], "stratum": a["stratum"], "final_verdict": "unassessable" if a["id"] == selected else "adequate",
                    **({} if a["id"] == selected else {"first_assessment": {"verdict": "adequate"}, "independent_assessment": {"verdict": "adequate"}})} for a in m["articles"]]
        stats = audit.statistics(m, records)
        self.assertEqual(stats["sample_size"], 4)
        self.assertIsNone(stats["weighted_problem_rate_estimate"])
        self.assertEqual(stats["weighted_problem_rate_bounds"], [0, 3 / 7])

    def test_reading_and_actor_gates(self):
        item = self.manifest["articles"][0]
        path = self.work / "assessments" / (item["id"] + ".json")
        result = base.read_json(path)
        result["actor"] = "wrong"
        base.write_json(path, result)
        with self.assertRaisesRegex(ValueError, "unexpected assessment actor"):
            audit.check(self.root, self.work)
        result["actor"] = self.owners["assessor"][item["id"]]
        result["read_chunks"] = []
        base.write_json(path, result)
        with self.assertRaisesRegex(ValueError, "read_chunks"):
            audit.check(self.root, self.work)

    def test_outline_must_remain_frozen(self):
        item = self.manifest["articles"][0]
        path = self.work / "outlines" / self.owners["assessor"][item["id"]] / (item["id"] + ".json")
        outline = base.read_json(path)
        outline["major_points"][0]["point"] = "看了总结后改掉的观点"
        base.write_json(path, outline)
        with self.assertRaisesRegex(ValueError, "Blind outline changed"):
            audit.check(self.root, self.work)

    def test_missing_evidence_is_null_not_fabricated(self):
        item = self.manifest["articles"][0]
        self.make_results(item, "undercovered", "undercovered")
        audit.check(self.root, self.work)
        path = self.work / "assessments" / (item["id"] + ".json")
        result = base.read_json(path)
        result["major_points"][0]["summary_evidence"] = self.summary
        base.write_json(path, result)
        with self.assertRaisesRegex(ValueError, "must not fabricate"):
            audit.check(self.root, self.work)

    def test_verdict_follows_main_points_not_minor_omissions(self):
        item = self.manifest["articles"][0]
        path = self.work / "assessments" / (item["id"] + ".json")
        result = base.read_json(path)
        result["minor_omissions"] = ["一个普通例子没有出现"]
        base.write_json(path, result)
        self.save_comparison(item)
        audit.check(self.root, self.work)
        result["verdict"] = "undercovered"
        base.write_json(path, result)
        with self.assertRaisesRegex(ValueError, "Verdict conflicts"):
            audit.check(self.root, self.work)

    def test_disagreement_needs_adjudication_and_preserves_both_labels(self):
        item = self.manifest["articles"][0]
        self.make_results(item, "undercovered", "adequate")
        with self.assertRaisesRegex(ValueError, "Pending"):
            audit.check(self.root, self.work)
        comparison = base.read_json(self.work / "comparisons" / (item["id"] + ".json"))
        base.write_json(self.work / "adjudications" / (item["id"] + ".json"), {
            "id": item["id"], "decider": "Codex主代理", "first_assessment_sha256": comparison["first_assessment_sha256"],
            "independent_assessment_sha256": comparison["independent_assessment_sha256"], "final_verdict": "adequate",
            "rationale": "回查原文后判断争议属于次要例子", "source_spans": [{"start": 0, "end": 30}]})
        _, records, _, _ = audit.check(self.root, self.work)
        record = next(r for r in records if r["id"] == item["id"])
        self.assertEqual(record["final_verdict"], "adequate")
        self.assertEqual(record["first_assessment"]["verdict"], "undercovered")
        self.assertEqual(record["independent_assessment"]["verdict"], "adequate")

    def test_comparison_cannot_hide_label_conflict(self):
        item = self.manifest["articles"][0]
        self.make_results(item, "undercovered", "adequate")
        self.save_comparison(item, False)
        with self.assertRaisesRegex(ValueError, "hidden"):
            audit.check(self.root, self.work)

    def test_conflict_details_cannot_be_empty_even_with_an_adjudication_flag(self):
        item = self.manifest["articles"][0]
        self.make_results(item, "undercovered", "adequate")
        path = self.work / "comparisons" / (item["id"] + ".json")
        comparison = base.read_json(path)
        comparison["conflicts"] = []
        base.write_json(path, comparison)
        with self.assertRaisesRegex(ValueError, "concrete conflict"):
            audit.check(self.root, self.work)

    def test_primary_can_correct_an_agreed_misclassification(self):
        item = self.manifest["articles"][0]
        self.make_results(item, "undercovered", "undercovered")
        comparison = base.read_json(self.work / "comparisons" / (item["id"] + ".json"))
        decision = {"id": item["id"], "decider": "Codex主代理", "first_assessment_sha256": comparison["first_assessment_sha256"],
                    "independent_assessment_sha256": comparison["independent_assessment_sha256"], "final_verdict": "adequate",
                    "rationale": "主代理发现双方把同一次要案例误判为主要遗漏，回查后纠正", "source_spans": [{"start": 0, "end": 30}]}
        path = self.work / "adjudications" / (item["id"] + ".json")
        base.write_json(path, decision)
        _, records, _, _ = audit.check(self.root, self.work)
        record = next(r for r in records if r["id"] == item["id"])
        self.assertEqual(record["final_verdict"], "adequate")
        self.assertEqual(record["first_assessment"]["verdict"], "undercovered")
        decision["decider"] = "sample_assessor_1"
        base.write_json(path, decision)
        with self.assertRaisesRegex(ValueError, "primary agent"):
            audit.check(self.root, self.work)

    def test_comparison_hash_must_match_initial_judgments(self):
        item = self.manifest["articles"][0]
        path = self.work / "assessments" / (item["id"] + ".json")
        result = base.read_json(path)
        result["rationale"] += "新的补充"
        base.write_json(path, result)
        with self.assertRaisesRegex(ValueError, "stale initial"):
            audit.check(self.root, self.work)

    def test_weighted_rate_is_not_pooled_sample_rate(self):
        records = []
        for item in self.manifest["articles"]:
            verdict = "undercovered" if item["stratum"] == 0 else "adequate"
            records.append({"id": item["id"], "stratum": item["stratum"], "final_verdict": verdict,
                            "first_assessment": {"verdict": verdict}, "independent_assessment": {"verdict": verdict}})
        stats = audit.statistics(self.manifest, records)
        self.assertAlmostEqual(stats["weighted_problem_rate_estimate"], 3 / 7)
        self.assertNotEqual(stats["weighted_problem_rate_estimate"], 1 / 4)

    def test_wilson_boundaries_do_not_collapse_at_zero_or_all(self):
        self.assertAlmostEqual(audit.wilson_interval(0, 15)[0], 0)
        self.assertGreater(audit.wilson_interval(0, 15)[1], 0.2)
        self.assertLess(audit.wilson_interval(15, 15)[0], 0.8)
        self.assertAlmostEqual(audit.wilson_interval(15, 15)[1], 1)

    def test_report_is_read_only_and_idempotent(self):
        before = {path: audit.file_hash(self.root / path) for path in self.manifest["protected_files"]}
        stats = audit.report(self.root, self.work)
        output = (self.root / audit.REPORT_PATH).read_bytes()
        audit.report(self.root, self.work)
        self.assertEqual(output, (self.root / audit.REPORT_PATH).read_bytes())
        self.assertEqual(before, {path: audit.file_hash(self.root / path) for path in before})
        self.assertEqual(stats["counts"], {"adequate": 4})


if __name__ == "__main__":
    unittest.main()
