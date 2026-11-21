import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import migrate_summary_publication as migration
import summary_history
import summary_publications
import summary_workflow as workflow


class SummaryPublicationMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "source"
        self.source.mkdir()
        self.audit_file = self.directory / "private" / "audit.json"
        self.receipts_file = self.directory / "isolated" / "receipts.json"

        summary = "这段摘要已经验收。"
        digest = workflow.digest(summary)
        source = {key: workflow.digest(key) for key in summary_publications.SOURCE_HASHES}
        self.audit = {
            "schema_version": 1,
            "run_id": "accepted",
            "revision_date": "2026-11-01",
            "previous_runs": [],
            "entries": [{
                "id": "1",
                "source": source,
                "before_summary_sha256": digest,
                "decision": "retain",
                "writer": {"writer": "writer", "summary": summary, "private_notes": "保留此完整证据"},
                "review": {"verdict": "pass", "reviewer": "reviewer", "model": "model",
                           "reasoning_effort": "max", "summary_sha256": digest, **source},
            }],
        }
        self.ledger = {"schema_version": 1, "entries": []}
        self.catalog = {"articles": [{"id": "1", "summary": summary, **source}]}
        self.sampling = {f"sampling-{i}": {"run_id": f"sampling-{i}", "entries": [{"note": "逐篇理由"}]}
                         for i in range(len(migration.SAMPLING_PATHS))}
        self.reports = {relative: f"报告 {i}\r\n逐篇证据\r\n"
                        for i, relative in enumerate(migration.DETAILED_REPORT_PATHS)}
        self.write("data/summary-reviews.json", workflow.json_text(self.audit))
        self.write(migration.RECEIPTS_PATH, workflow.json_text(self.ledger))
        self.write("data/catalog.json", workflow.json_text(self.catalog))
        for relative, report in zip(migration.SAMPLING_PATHS, self.sampling.values(), strict=True):
            self.write(relative, workflow.json_text(report))
        for relative, report in self.reports.items():
            self.write(relative, report)
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com",
                 "commit", "-qm", "Legacy audit")
        self.source_rev = self.git("rev-parse", "HEAD").strip()

    def write(self, relative, text):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args], text=True)

    def migrate(self):
        return migration.migrate(self.source, self.source_rev, self.audit_file, self.receipts_file)

    def test_pinned_migration_recovers_evidence_and_is_idempotent(self):
        self.write("data/summary-reviews.json", "corrupted worktree")
        result = self.migrate()
        self.assertEqual(result["outputs_changed"], 2)
        self.assertEqual(result["review_entries"], 1)
        self.assertEqual(summary_history.load_audit(self.source, self.audit_file), self.audit)
        self.assertEqual(summary_history.load_private_ledger(self.audit_file), self.ledger)
        archive = summary_history.load_private(self.audit_file)
        self.assertEqual(archive["sampling"], self.sampling)
        self.assertEqual(archive["reports"], self.reports)
        receipts = summary_publications.read(self.receipts_file)
        self.assertEqual(summary_publications.replay(self.catalog, receipts), self.catalog)
        self.assertNotIn("保留此完整证据", json.dumps(receipts, ensure_ascii=False))
        self.assertEqual(self.migrate(), {**result, "outputs_changed": 0})
        self.assertEqual((self.source / "data/summary-reviews.json").read_text(), "corrupted worktree")

    def test_receipt_output_must_not_replace_a_source_file(self):
        with self.assertRaisesRegex(ValueError, "Receipt output must be isolated"):
            migration.migrate(self.source, self.source_rev, self.audit_file, self.source / migration.RECEIPTS_PATH)
        self.assertFalse(self.audit_file.exists())

    def test_mismatched_preexisting_receipts_leave_private_output_untouched(self):
        self.receipts_file.parent.mkdir()
        self.receipts_file.write_text("other receipts")
        with self.assertRaisesRegex(ValueError, "output already exists with different content"):
            self.migrate()
        self.assertFalse(self.audit_file.exists())
        self.assertEqual(self.receipts_file.read_text(), "other receipts")
