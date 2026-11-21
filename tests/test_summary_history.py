import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import summary_history as history


def review_entry(article_id):
    return {
        "id": article_id,
        "writer": {"major_points": [{"claim": "原文主张"}]},
        "writer_initial": {"verdict": "retain"},
        "reviewer_initial": {"verdict": "rewrite"},
        "review": {"verdict": "pass"},
    }


def audit_run(run_id, *article_ids):
    return {"schema_version": 1, "run_id": run_id, "entries": [review_entry(i) for i in article_ids]}


class SummaryHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.index_path = self.root / "data" / "summary-reviews.json"

    def write_payloads(self, payloads):
        for path, text in payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def shard_path(self, run_id):
        return self.index_path.with_suffix("") / (run_id + ".json")

    def install_legacy_shards(self, *runs):
        descriptors = []
        for run in runs:
            run_id = run["run_id"]
            path = self.shard_path(run_id)
            text = json.dumps(run, ensure_ascii=False)
            self.write_payloads({path: text})
            descriptors.append({"run_id": run_id, "file": str(path.relative_to(self.root)),
                                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        index = {"schema_version": 2, "latest_run_id": runs[-1]["run_id"], "runs": descriptors}
        self.write_payloads({self.index_path: json.dumps(index)})
        return index

    def test_legacy_migration_round_trips_and_checks_exact_shard_bytes(self):
        first, second = audit_run("first", "1"), audit_run("second", "2")
        audit = dict(second, previous_runs=[first])
        self.assertIsNone(history.load_audit(self.root))
        self.write_payloads({self.index_path: json.dumps(audit, ensure_ascii=False)})
        self.assertEqual(history.load_audit(self.root), audit)

        self.install_legacy_shards(first, second)
        self.assertEqual(history.load_audit(self.root), audit)

        old_shard = self.shard_path("first")
        previous = old_shard.read_bytes()
        old_shard.write_bytes(previous + b" ")
        with self.assertRaises((ValueError, OSError)):
            history.load_audit(self.root)
        old_shard.unlink()
        with self.assertRaises((ValueError, OSError)):
            history.load_audit(self.root)

    def test_rejects_unsafe_run_name_and_forged_shard_path(self):
        index = self.install_legacy_shards(audit_run("safe", "1"))
        forged = copy.deepcopy(index)
        forged["runs"][0]["run_id"] = "../outside"
        self.write_payloads({self.index_path: json.dumps(forged)})
        with self.assertRaises(ValueError):
            history.load_audit(self.root)
        index["runs"][0]["file"] = "../outside.json"
        self.write_payloads({self.index_path: json.dumps(index)})
        with self.assertRaises(ValueError):
            history.load_audit(self.root)

    def test_revision_references_deduplicate_exact_evidence_losslessly(self):
        archived = audit_run("first", "1")
        audit = dict(audit_run("second", "1"), previous_runs=[archived])
        entry = archived["entries"][0]
        old_revision = {"id": "7", "review": {"annotated_by": "original editor"}}
        revision = {
            "id": "1",
            "run_id": "first",
            "revision_id": "revision-1",
            "review": entry["review"],
            "coverage": entry["writer"]["major_points"],
            "writer_initial": entry["writer_initial"],
            "reviewer_initial": {"verdict": "retain"},
            "meta": {"some_field": "old evidence"},
        }
        ledger = {"schema_version": 1, "entries": [old_revision, revision], "origin": "legacy"}
        compact = history.compact_ledger(ledger, audit)
        self.assertEqual(compact["entries"][0], old_revision)
        self.assertEqual(set(compact["entries"][1]["evidence_ref"]["fields"]), {"review", "coverage", "writer_initial"})
        self.assertEqual(compact["entries"][1]["reviewer_initial"], revision["reviewer_initial"])
        self.assertTrue({"review", "coverage", "writer_initial"}.isdisjoint(compact["entries"][1]))
        self.assertEqual(history.expand_ledger(compact, audit), ledger)
        self.assertEqual(history.compact_ledger(history.expand_ledger(compact, audit), audit), compact)

        spoofed = copy.deepcopy(compact)
        spoofed["entries"][1]["review"] = entry["review"]
        with self.assertRaises(ValueError):
            history.expand_ledger(spoofed, audit)

        unknown_fields = copy.deepcopy(compact)
        unknown_fields["entries"][1]["evidence_ref"]["fields"].append("revision_id")
        with self.assertRaises(ValueError):
            history.expand_ledger(unknown_fields, audit)

        changed_audit = copy.deepcopy(audit)
        changed_audit["previous_runs"][0]["entries"][0]["review"]["verdict"] = "revise"
        with self.assertRaises(ValueError):
            history.expand_ledger(compact, changed_audit)
        with self.assertRaises(ValueError):
            history.expand_ledger(compact, dict(audit, previous_runs=[]))

    def test_compaction_distinguishes_json_booleans_from_numbers(self):
        audit = dict(audit_run("first", "1"), previous_runs=[])
        audit["entries"][0]["review"]["verdict"] = 1
        revision = {"id": "1", "run_id": "first", "revision_id": "revision-1", "review": {"verdict": True}}
        ledger = {"schema_version": 1, "entries": [revision]}
        restored = history.expand_ledger(history.compact_ledger(ledger, audit), audit)
        self.assertIs(restored["entries"][0]["review"]["verdict"], True)


if __name__ == "__main__":
    unittest.main()
