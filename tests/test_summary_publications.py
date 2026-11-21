import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import summary_publications as receipts


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def source(name):
    return {key: digest(f"{name}/{key}") for key in receipts.SOURCE_HASHES}


def reviewed(article_id, before, after, fingerprints):
    return {"id": article_id, "source": fingerprints, "before_summary_sha256": digest(before),
            "decision": "rewrite" if before != after else "retain",
            "writer": {"writer": "writer", "summary": after, "notes": "Private drafting notes"},
            "review": {"reviewer": "reviewer", "model": "chosen-model", "reasoning_effort": "high",
                       "verdict": "pass", "summary_sha256": digest(after),
                       "source_sha256": fingerprints["source_sha256"], "body_sha256": fingerprints["body_sha256"],
                       "notes": "Private review evidence"}}


def event(run_id, entry, before, *, transition=None):
    fingerprints = entry["source"]
    fields = [run_id, entry["id"], fingerprints["source_sha256"], fingerprints["body_sha256"],
              digest(before), entry["review"]["summary_sha256"]]
    if transition is not None:
        fields.append(canonical(transition))
    result = {"id": entry["id"], "run_id": run_id, "revision_id": digest("\n".join(fields)),
              "source": {"source_sha256": fingerprints["source_sha256"],
                         "content_sha256": fingerprints["content_sha256"],
                         "visible_text_sha256": fingerprints["body_sha256"]},
              "before": {"summary": before}, "after": {
                  "summary": entry["writer"]["summary"], "reviewer": "reviewer", "writer": "writer",
                  "revision_date": "2026-11-01", "model": "chosen-model", "reasoning_effort": "high"}}
    if transition is not None:
        result["source_transition"] = transition
    return result


def run(run_id, entries, scope=None):
    return {"run_id": run_id, "revision_date": "2026-11-01", "entries": entries,
            "scope": {"article_ids": scope if scope is not None else [entry["id"] for entry in entries]}}


def shuffled(value):
    if isinstance(value, dict):
        return {key: shuffled(value[key]) for key in reversed(value)}
    if isinstance(value, list):
        return [shuffled(entry) for entry in value]
    return value


class SummaryPublicationTests(unittest.TestCase):
    def setUp(self):
        self.source = source("first source version")
        first_entry = reviewed("1", "old", "first", self.source)
        second_entry = reviewed("1", "first", "second", self.source)
        self.first = run("first", [first_entry])
        self.second = run("second", [second_entry])
        self.first_event = event("first", first_entry, "old")
        self.second_event = event("second", second_entry, "first")
        self.public = receipts.migrate({"entries": [self.first_event, self.second_event]},
                                       {**self.second, "previous_runs": [self.first]})

    def test_replay_uses_explicit_chains(self):
        result = receipts.replay({"articles": [{"id": "1", **self.source, "summary": "old"}]},
                                 shuffled(self.public))
        self.assertEqual(result["articles"][0]["summary"], "second")
        self.assertEqual(result["articles"][0]["summary_revision"]["revision_id"], self.second_event["revision_id"])
        self.assertEqual([row["before"]["summary_sha256"] for row in receipts.ordered_revisions(shuffled(self.public))],
                         [digest("old"), digest("first")])
        public_text = json.dumps(self.public)
        self.assertNotIn("Private drafting notes", public_text)
        self.assertNotIn("Private review evidence", public_text)
        self.assertNotIn('"summary": "old"', public_text)

    def test_invalid_summary_or_source_is_rejected_before_replay(self):
        for source_fields, summary, diagnostic in (
            (self.source, "unknown", "outside accepted history"),
            (source("wrong version"), "old", "source conflicts"),
        ):
            with self.subTest(summary=summary):
                value = {"articles": [{"id": "1", **source_fields, "summary": summary}]}
                old_value = copy.deepcopy(value)
                with self.assertRaisesRegex(ValueError, diagnostic):
                    receipts.replay(value, self.public)
                self.assertEqual(value, old_value)

    def test_unlinked_and_conflicting_revisions_are_rejected(self):
        bad = copy.deepcopy(self.public)
        bad["revisions"][self.second_event["revision_id"]]["previous_revision_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "Broken revision history"):
            receipts.validate(bad)
        bad = copy.deepcopy(self.public)
        bad["revisions"][self.second_event["revision_id"]]["summary"] = "unapproved"
        with self.assertRaisesRegex(ValueError, "Invalid summary revision"):
            receipts.validate(bad)
        bad = copy.deepcopy(self.public)
        bad["revisions"][self.second_event["revision_id"]]["before_summary_sha256"] = digest("forged")
        with self.assertRaisesRegex(ValueError, "differs from its approval"):
            receipts.validate(bad)

    def test_source_transition_only_accepts_the_current_source_chain(self):
        fingerprints = source("second source version")
        entry = reviewed("1", "second", "source transition", fingerprints)
        transition = {"previous_source_sha256": self.source["source_sha256"],
                      "previous_content_sha256": self.source["content_sha256"],
                      "previous_summary_sha256": digest("second"), "previous_catalog_sha256": digest("catalog")}
        changed = receipts.update(self.public, run("third", [entry]),
                                  [event("third", entry, "second", transition=transition)])
        row = {"id": "1", **fingerprints, "summary": "second"}
        self.assertEqual(receipts.replay({"articles": [row]}, changed)["articles"][0]["summary"], "source transition")
        row["summary"] = "old"
        with self.assertRaisesRegex(ValueError, "outside accepted history"):
            receipts.replay({"articles": [row]}, changed)

    def test_legacy_codex_events_and_sampling_reuse_are_marked_separately(self):
        legacy = {"id": "1", "source": {key: self.source[key] for key in ("source_sha256", "content_sha256")},
                  "before": {"summary": "old"},
                  "after": {"summary": "historic release", "reviewer": "Codex", "revision_date": "2026-10-31"}}
        prior = {"summary_sha256": digest("keep"), "final_verdict": "adequate",
                 "notes": "Private sampling assessment"}
        reused = {"id": "2", "source": self.source, "decision": "retain",
                  "review_origin": "sampling_reuse_adequate", "prior_review": prior,
                  "before_summary_sha256": digest("keep"),
                  "reused_audit": {"run_id": "samples", "final_verdict": "adequate",
                                   "entry_sha256": canonical(prior), "report_sha256": digest("samples report")}}
        value = receipts.migrate({"entries": [legacy]}, run("reuse", [reused]))
        stored = next(iter(value["revisions"].values()))
        self.assertEqual(stored["basis"], "legacy_codex")
        self.assertEqual(value["runs"]["reuse"]["approvals"]["2"]["basis"], "sampling_reuse_adequate")
        self.assertEqual({item[0] for item in receipts.accepted_versions(value)}, {"2"})
        rows = [{"id": "1", **self.source, "summary": "old"}, {"id": "2", **self.source, "summary": "keep"}]
        replayed = receipts.replay({"articles": rows}, value)["articles"]
        self.assertNotIn("revision_id", replayed[0]["summary_revision"])
        self.assertEqual(replayed[0]["summary"], "historic release")
        rows[1]["summary"] = "changed"
        with self.assertRaisesRegex(ValueError, "retained summary differs"):
            receipts.replay({"articles": rows}, value)
        self.assertNotIn(prior["notes"], json.dumps(value))

    def test_phase_updates_keep_previous_approvals_immutable(self):
        first = reviewed("2", "old", "first", self.source)
        second = reviewed("1", "unchanged", "unchanged", self.source)
        first_phase = digest("first-phase")
        first_run = {**run("first", [first], scope=["1", "2"]),
                     "publication": {"status": "in_progress", "target_scope": 2,
                                     "published_ids": ["2"], "pending_ids": ["1"]},
                     "publication_history": [{"phase_id": first_phase, "status": "in_progress",
                                              "published_ids": ["2"], "newly_published_ids": ["2"]}]}
        partial = shuffled(receipts.update(receipts.empty(), first_run, [event("first", first, "old")]))
        final_phase = digest("second-phase")
        final_run = {**first_run, "entries": [second, first],
                     "publication": {"status": "complete", "target_scope": 2,
                                     "published_ids": ["1", "2"], "pending_ids": []},
                     "publication_history": [*first_run["publication_history"],
                                             {"phase_id": final_phase, "status": "complete",
                                              "published_ids": ["1", "2"], "newly_published_ids": ["1"]}]}
        completed = receipts.update(partial, final_run)
        old = partial["runs"]["first"]
        finished = completed["runs"]["first"]
        phases = finished["phases"]
        self.assertEqual(list(finished), list(old))
        self.assertEqual(list(finished["approvals"]), ["2", "1"])
        self.assertEqual(list(phases), [first_phase, final_phase])
        self.assertEqual(json.dumps(finished["approvals"]["2"]), json.dumps(old["approvals"]["2"]))
        self.assertEqual(json.dumps(phases[first_phase]), json.dumps(old["phases"][first_phase]))
        self.assertEqual(phases[final_phase]["previous_phase_id"], first_phase)
        self.assertEqual(receipts.validate(shuffled(completed)), completed)
        self.assertEqual(json.dumps(receipts.update(completed, final_run)), json.dumps(completed))
        bad_run = copy.deepcopy(final_run)
        bad_run["entries"][1]["review"]["notes"] = "Private finding changed"
        with self.assertRaisesRegex(ValueError, "Accepted publication receipts changed"):
            receipts.update(partial, bad_run)

    def test_duplicate_json_members_or_private_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "receipts.json"
            file.write_text('{"runs":{"a":{},"a":{}}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate member"):
                receipts.read(file)
        bad = copy.deepcopy(self.public)
        bad["runs"]["first"]["approvals"]["1"]["private_review"] = "do not publish"
        with self.assertRaisesRegex(ValueError, "Invalid approval"):
            receipts.validate(bad)


if __name__ == "__main__":
    unittest.main()
