"""Reject private summary evidence in the public site input."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import summary_publications


def _contains_private_records(value):
    if not isinstance(value, dict):
        return False
    if "run_id" in value and "entries" in value and ("statistics" in value or "sampling" in value):
        return True
    if "runs" in value and any(key in value for key in ("latest_run_id", "sampling", "reports")):
        return True
    entries = value.get("entries", [])
    return isinstance(entries, list) and any(
        isinstance(entry, dict) and any(key in entry for key in ("evidence_ref", "writer_initial", "reviewer_initial"))
        for entry in entries
    )


def check(root):
    root = Path(root)
    candidates = (
        root / "data/summary-reviews.json",
        root / "data/summary-reviews",
        root / "docs/summary-compact-articles.csv",
        root / "docs/summary-compact-results.md",
        root / "docs/summary-midlength-progress.md",
        root / "docs/summary-midlength-results.md",
        root / "docs/summary-sampling-1000-8000-results.md",
        *root.glob("data/summary-sampling-audit*.json"),
        *root.glob("data/**/summary-audit.json"),
    )
    present = [path.relative_to(root).as_posix() for path in candidates if path.exists() or path.is_symlink()]
    if present:
        raise ValueError(f"Private summary evidence is present in public paths: {', '.join(sorted(present))}")
    receipt_path = root / "data/summary-revisions.json"
    receipts = summary_publications.read(receipt_path)
    summary_publications.validate(receipts)
    for path in (root / "data").rglob("*.json"):
        if path != receipt_path and _contains_private_records(json.loads(path.read_bytes())):
            raise ValueError(f"Private summary evidence is present in public paths: {path.relative_to(root)}")
    return receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    try:
        check(parser.parse_args().root)
    except (ValueError, OSError, TypeError, KeyError) as error:
        print(f"summary publication: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
