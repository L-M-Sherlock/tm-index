import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import summary_history as history


def review(article_id):
    return {
        "id": article_id,
        "writer_initial": {"verdict": "rewrite"},
        "reviewer_initial": {"verdict": "rewrite"},
        "review": {"verdict": "pass", "notes": ["完整审阅证据"]},
    }


def run(run_id, *article_ids):
    return {"schema_version": 1, "run_id": run_id, "entries": [review(i) for i in article_ids]}


def phase(phase_id, *article_ids, complete=False):
    return {"phase_id": phase_id, "status": "complete" if complete else "in_progress", "published_ids": list(article_ids)}


def reverse_objects(value):
    if isinstance(value, dict):
        return {key: reverse_objects(item) for key, item in reversed(value.items())}
    if isinstance(value, list):
        return [reverse_objects(item) for item in value]
    return value


class PrivateSummaryHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name).resolve()
        self.root = workspace / "checkout"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        self.path = workspace / "private" / "summary-audit.json"

    def write_payloads(self, payloads):
        for path, text in payloads.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def test_archive_round_trip_does_not_depend_on_json_object_member_order(self):
        latest = run("latest", "20", "10")
        latest["publication"] = {"status": "complete", "published_ids": ["20", "10"], "pending_ids": []}
        latest["publication_history"] = [phase("a", "20"), phase("b", "20", "10", complete=True)]
        audit = dict(latest, previous_runs=[run("earlier", "1")])
        ledger = {
            "schema_version": 1,
            "entries": [
                {"id": "20", "notes": ["旧修订，无显式 ID"]},
                {"id": "1", "run_id": "earlier", "revision_id": "rev-1", "coverage": ["历史证据"]},
                {"id": "20", "run_id": "latest", "revision_id": "rev-2", "review": {"verdict": "pass"}},
            ],
            "retired_articles": {"1": {"reason": "历史来源变更"}},
        }
        sampling = {"sample": {"run_id": "sample", "entries": [{"id": "9", "review": "抽样结果"}]}}
        reports = {"docs/summary-compact-results.md": "# 完整原稿\n\n**原始证据。**\n"}
        payloads = history.private_payload(self.path, audit=audit, ledger=ledger, sampling=sampling, reports=reports)
        self.assertEqual(list(payloads), [self.path])
        self.write_payloads(payloads)
        stored = history.load_private(self.path)
        self.assertEqual(stored["latest_run_id"], "latest")
        self.assertEqual(stored["runs"]["latest"]["previous_run_id"], "earlier")
        self.assertEqual(set(stored["runs"]["latest"]["entries"]), {"20", "10"})
        self.assertEqual(stored["runs"]["latest"]["latest_phase_id"], "b")
        self.assertEqual(stored["runs"]["latest"]["phases"]["b"]["previous_phase_id"], "a")
        self.assertEqual(stored["sampling"], sampling)
        self.assertEqual(stored["reports"], reports)
        self.assertEqual(history.load_audit(self.root, self.path), audit)
        self.assertEqual(history.load_private_ledger(self.path), ledger)
        self.assertEqual(history.private_payload(self.path, audit=audit, ledger=ledger, sampling=sampling, reports=reports), {})

        self.path.write_text(json.dumps(reverse_objects(stored), ensure_ascii=False), encoding="utf-8")
        self.assertEqual(history.load_audit(self.root, self.path), audit)
        self.assertEqual(history.load_private_ledger(self.path), ledger)

        stored["runs"]["latest"]["phases"]["b"]["previous_phase_id"] = "missing"
        self.path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            history.load_audit(self.root, self.path)

    def test_new_batches_append_and_cannot_replace_archived_evidence(self):
        first = dict(run("archive", "1"), previous_runs=[])
        self.write_payloads(history.private_payload(self.path, audit=first))
        saved = history.load_private(self.path)["runs"]["archive"]
        second = dict(run("active", "2"), previous_runs=[run("archive", "1")])
        updated = history.private_payload(self.path, audit=second)
        self.write_payloads(updated)
        stored = history.load_private(self.path)
        self.assertEqual(list(stored["runs"]), ["archive", "active"])
        self.assertEqual(stored["runs"]["archive"], saved)

        modified = copy.deepcopy(second)
        modified["previous_runs"][0]["entries"][0]["review"]["notes"] = ["替换后的证据"]
        with self.assertRaises(ValueError):
            history.private_payload(self.path, audit=modified)
        self.assertEqual(history.load_audit(self.root, self.path), second)

    def test_active_batch_can_append_phase_and_entries_only(self):
        first = dict(run("first", "20"), previous_runs=[])
        first["publication"] = {"status": "in_progress", "published_ids": ["20"], "pending_ids": ["10"]}
        first["publication_history"] = [phase("p1", "20")]
        self.write_payloads(history.private_payload(self.path, audit=first))
        old = history.load_private(self.path)["runs"]["first"]

        complete = copy.deepcopy(first)
        complete["entries"].insert(0, review("10"))
        complete["publication"] = {"status": "complete", "published_ids": ["10", "20"], "pending_ids": []}
        complete["publication_history"].append(phase("p2", "10", "20", complete=True))
        modified = copy.deepcopy(complete)
        modified["entries"][1]["review"]["verdict"] = "revise"
        with self.assertRaises(ValueError):
            history.private_payload(self.path, audit=modified)
        self.write_payloads(history.private_payload(self.path, audit=complete))
        updated = history.load_private(self.path)["runs"]["first"]
        self.assertEqual(list(updated["entries"]), ["20", "10"])
        self.assertEqual(updated["entries"]["20"], old["entries"]["20"])
        self.assertEqual(updated["phases"]["p1"], old["phases"]["p1"])
        self.assertEqual(updated["phases"]["p2"]["previous_phase_id"], "p1")
        self.assertEqual(history.load_audit(self.root, self.path), complete)

    def test_ledger_append_rejects_changed_historical_revision(self):
        past = {"id": "1", "revision_id": "rev-1", "review": {"verdict": "pass"}}
        current = {"id": "1", "revision_id": "rev-2", "coverage": ["既有证据"]}
        first = {"schema_version": 1, "entries": [past]}
        complete = {"schema_version": 1, "entries": [past, current]}
        self.write_payloads(history.private_payload(self.path, ledger=first))
        self.write_payloads(history.private_payload(self.path, ledger=complete))
        self.assertEqual(history.load_private_ledger(self.path), complete)
        modified = copy.deepcopy(complete)
        modified["entries"][0]["review"]["verdict"] = "revise"
        with self.assertRaises(ValueError):
            history.private_payload(self.path, ledger=modified)

    def test_ancillary_archives_update_only_with_new_keys(self):
        self.write_payloads(history.private_payload(self.path, sampling={"sample-1": {"entries": [1]}},
                                                   reports={"docs/report.md": "原文"}))
        self.write_payloads(history.private_payload(self.path, sampling={"sample-2": {"entries": [2]}}))
        stored = history.load_private(self.path)
        self.assertEqual(list(stored["sampling"]), ["sample-1", "sample-2"])
        self.assertEqual(stored["reports"], {"docs/report.md": "原文"})
        with self.assertRaises(ValueError):
            history.private_payload(self.path, sampling={"sample-1": {"entries": [3]}})
        with self.assertRaises(ValueError):
            history.private_payload(self.path, reports={"docs/report.md": "替换"})

    def test_private_path_rejects_tracked_checkout_locations_and_symlink_escape(self):
        self.assertEqual(history.private_audit_path(self.root, self.path), self.path)
        self.assertEqual(history.private_audit_path(self.root, Path(".work/local.json")), self.root / ".work/local.json")
        with self.assertRaises(ValueError):
            history.private_audit_path(self.root, Path("data/summary-audit.json"))
        (self.root / "data").mkdir()
        (self.root / ".work").mkdir()
        (self.root / ".work/leak").symlink_to(self.root / "data", target_is_directory=True)
        with self.assertRaises(ValueError):
            history.private_audit_path(self.root, self.root / ".work/leak/summary-audit.json")

    def test_missing_explicit_private_archive_does_not_use_tracked_legacy_files(self):
        old_audit = dict(run("legacy", "1"), previous_runs=[])
        self.write_payloads({self.root / history.AUDIT_INDEX: json.dumps(old_audit)})
        self.assertEqual(history.load_audit(self.root), old_audit)
        self.assertIsNone(history.load_audit(self.root, self.path))


if __name__ == "__main__":
    unittest.main()
