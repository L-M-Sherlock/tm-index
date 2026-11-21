#!/usr/bin/env python3
"""Move a pinned published review archive to private storage without changing its results."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import summary_history
import summary_publications
import summary_workflow as workflow


RECEIPTS_PATH = "data/summary-revisions.json"
SAMPLING_PATHS = (
    "data/summary-sampling-audit.json",
    "data/summary-sampling-audit-1000-8000-20261103.json",
)
DETAILED_REPORT_PATHS = (
    "docs/summary-compact-articles.csv",
    "docs/summary-compact-results.md",
    "docs/summary-midlength-progress.md",
    "docs/summary-midlength-results.md",
    "docs/summary-sampling-1000-8000-results.md",
)
PURGE_PATHS = (
    summary_history.AUDIT_INDEX.as_posix(),
    summary_history.AUDIT_RUNS.as_posix() + "/",
    RECEIPTS_PATH,
    *SAMPLING_PATHS,
    *DETAILED_REPORT_PATHS,
)


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def _read_legacy(source_root, commit, scratch):
    audit_index, audit_directory = PURGE_PATHS[:2]
    paths = _git(source_root, "ls-tree", "-r", "-z", "--name-only", commit, "--",
                 audit_index, audit_directory.rstrip("/")).decode("utf-8").split("\0")
    for relative in filter(None, paths):
        staged = workflow.confined(scratch, relative)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(_git(source_root, "show", f"{commit}:{relative}"))
    audit = summary_history.load_audit(scratch)
    workflow.require(audit is not None, "Pinned source has no legacy summary review archive")
    compact_ledger = json.loads(_git(source_root, "show", f"{commit}:{RECEIPTS_PATH}"))
    return audit, summary_history.expand_ledger(compact_ledger, audit)


def _read_reports(source_root, commit):
    reports = {relative: _git(source_root, "show", f"{commit}:{relative}").decode("utf-8")
               for relative in DETAILED_REPORT_PATHS}
    sampling = {}
    for relative in SAMPLING_PATHS:
        raw = _git(source_root, "show", f"{commit}:{relative}")
        report = json.loads(raw)
        run_id = report["run_id"]
        workflow.require(run_id not in sampling, f"Duplicate legacy sampling run: {run_id}")
        sampling[run_id] = report
    return sampling, reports


def _verify_private(root, path, audit, ledger, sampling, reports):
    state = summary_history.load_private(path)
    restored = (summary_history.load_audit(root, path), summary_history.load_private_ledger(path),
                state.get("sampling"), state.get("reports"))
    for actual, expected, label in zip(restored, (audit, ledger, sampling, reports),
                                      ("reviews", "revisions", "sampling", "reports"), strict=True):
        workflow.require(workflow.canonical_hash(actual) == workflow.canonical_hash(expected),
                         f"Private archive failed to reconstruct legacy {label}")


def migrate(source_root, source_rev, audit_file, receipts_output):
    """Verify both projections of a legacy Git snapshot, then write the isolated outputs."""
    source_root = Path(source_root).resolve()
    private = summary_history.private_audit_path(source_root, audit_file)
    public = Path(receipts_output).expanduser().resolve()
    workflow.require(public != private and public.suffix == ".json",
                     "Use distinct JSON output paths for audit evidence and receipts")
    workflow.require(not public.is_relative_to(source_root) or public.is_relative_to(source_root / ".work"),
                     "Receipt output must be isolated from the source checkout")
    commit = _git(source_root, "rev-parse", "--verify", "--end-of-options",
                  f"{source_rev}^{{commit}}").decode("ascii").strip()

    with tempfile.TemporaryDirectory(prefix="tm-index-summary-migration-") as temporary:
        scratch = Path(temporary)
        audit, ledger = _read_legacy(source_root, commit, scratch)
        sampling, reports = _read_reports(source_root, commit)
        catalog = json.loads(_git(source_root, "show", f"{commit}:data/catalog.json"))
        workflow.require(workflow.replay_revisions(copy.deepcopy(catalog), ledger) == catalog,
                         "Pinned catalog differs from the complete legacy revision history")
        receipts = summary_publications.migrate(ledger, audit)
        workflow.require(summary_publications.replay(copy.deepcopy(catalog), receipts) == catalog,
                         "Publication receipts do not reconstruct the pinned catalog")
        receipt_text = workflow.json_text(receipts)
        workflow.require(not public.exists() or public.read_bytes() == receipt_text.encode("utf-8"),
                         "Isolated receipt output already exists with different content")

        with summary_history.private_lock(private):
            outputs = summary_history.private_payload(
                private, audit=audit, ledger=ledger, sampling=sampling, reports=reports,
            )
            staged_private = scratch / ".work" / "summary-audit.json"
            staged_private.parent.mkdir()
            if private in outputs:
                staged_private.write_bytes(outputs[private].encode("utf-8"))
            else:
                staged_private.write_bytes(private.read_bytes())
            _verify_private(scratch, staged_private, audit, ledger, sampling, reports)
            if not public.exists():
                outputs[public] = receipt_text
            workflow.staged_write(outputs)

    runs = [*audit.get("previous_runs", []), audit]
    return {
        "source_commit": commit,
        "review_batches": len(runs),
        "review_entries": sum(len(run["entries"]) for run in runs),
        "revision_events": len(ledger["entries"]),
        "sampling_reports": len(sampling),
        "detailed_reports": len(reports),
        "private_bytes": private.stat().st_size,
        "private_sha256": workflow.file_hash(private),
        "receipt_bytes": public.stat().st_size,
        "receipt_sha256": workflow.file_hash(public),
        "outputs_changed": len(outputs),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-rev", required=True)
    parser.add_argument("--audit-file", required=True, type=Path)
    parser.add_argument("--receipts-output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = migrate(args.source_root, args.source_rev, args.audit_file, args.receipts_output)
    except (KeyError, OSError, TypeError, ValueError) as error:
        print(f"summary migration: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"summary migration: {error.stderr.decode('utf-8', errors='replace').strip()}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
