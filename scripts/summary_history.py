"""Store review batches once and resolve revision evidence without changing its logical shape."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


AUDIT_INDEX = Path("data/summary-reviews.json")
AUDIT_RUNS = Path("data/summary-reviews")
EVIDENCE_REF = "evidence_ref"
ENTRY_FIELDS = (
    "review", "writer_initial", "reviewer_initial", "review_origin",
    "reused_audit", "prior_review", "request", "adjudication",
)
WRITER_FIELDS = {
    "coverage": "major_points",
    "previous_summary_issues": "issues",
    "decision_reason": "decision_reason",
    "length_reason": "length_reason",
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _canonical_hash(value):
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _same_json(left, right):
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_json(item, right[key]) for key, item in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right))
    return left == right


def _run_file(run_id):
    _require(isinstance(run_id, str) and bool(run_id) and run_id == run_id.strip()
             and run_id not in {".", ".."} and re.fullmatch(r"[\w. -]+", run_id),
             f"Invalid summary review run ID: {run_id!r}")
    return AUDIT_RUNS / f"{run_id}.json"


def _local_file(root, relative):
    path = root / relative
    _require(path.is_relative_to(root) and path.resolve() == path,
             f"Summary history path escapes its storage location: {relative}")
    return path


def _load_index(root, value):
    descriptors = value.get("runs")
    _require(set(value) == {"schema_version", "runs", "latest_run_id"}
             and isinstance(descriptors, list) and descriptors,
             "Invalid summary review index")
    runs, seen = [], set()
    for descriptor in descriptors:
        _require(isinstance(descriptor, dict) and set(descriptor) == {"run_id", "file", "sha256"},
                 "Invalid summary review index entry")
        run_id = descriptor["run_id"]
        relative = _run_file(run_id)
        _require(run_id.casefold() not in seen and descriptor["file"] == relative.as_posix(),
                 f"Duplicate run or invalid review file: {run_id}")
        path = _local_file(root, relative)
        _require(path.is_file(), f"Missing summary review batch: {run_id}")
        raw = path.read_bytes()
        _require(_sha256(raw) == descriptor["sha256"], f"Summary review batch hash mismatch: {run_id}")
        run = json.loads(raw)
        _require(isinstance(run, dict) and run.get("run_id") == run_id and "previous_runs" not in run
                 and isinstance(run.get("entries"), list), f"Invalid summary review batch: {run_id}")
        runs.append(run)
        seen.add(run_id.casefold())
    _require(value["latest_run_id"] == runs[-1]["run_id"], "Invalid latest summary review run")
    return runs


def _audit_index(root):
    path = _local_file(root, AUDIT_INDEX)
    if not path.exists():
        return None
    value = json.loads(path.read_bytes())
    _require(isinstance(value, dict) and type(value.get("schema_version", 1)) is int
             and value.get("schema_version", 1) in {1, 2},
             "Unsupported summary review storage schema")
    return value


def load_audit(root):
    """Read either storage format as the complete, legacy-shaped audit."""
    root = Path(root).resolve()
    value = _audit_index(root)
    if value is None or value.get("schema_version") != 2:
        return value
    runs = _load_index(root, value)
    return {**runs[-1], "previous_runs": runs[:-1]}


def _audit_runs(audit):
    if not audit or "run_id" not in audit:
        return []
    prior = audit.get("previous_runs", [])
    _require(isinstance(prior, list), "Invalid summary review history")
    runs = [*prior, {key: value for key, value in audit.items() if key != "previous_runs"}]
    identifiers = set()
    for run in runs:
        _require(isinstance(run, dict) and "previous_runs" not in run and isinstance(run.get("entries"), list),
                 "Invalid summary review batch")
        run_id = run.get("run_id")
        _run_file(run_id)
        _require(run_id.casefold() not in identifiers, f"Duplicate summary review batch: {run_id}")
        identifiers.add(run_id.casefold())
    return runs


def _entries_by_id(run):
    result = {}
    for entry in run["entries"]:
        _require(isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"] not in result,
                 f"Invalid or duplicate summary review entry in {run['run_id']}")
        result[entry["id"]] = entry
    return result


def _can_extend(old, new):
    publication = old.get("publication", {})
    next_publication = new.get("publication", {})
    if (not isinstance(publication, dict) or not isinstance(next_publication, dict)
            or publication.get("status") != "in_progress"
            or next_publication.get("status") not in {"in_progress", "complete"}):
        return False
    old_entries = _entries_by_id(old)
    entries = _entries_by_id(new)
    history = old.get("publication_history", [])
    next_history = new.get("publication_history", [])
    old_published = publication.get("published_ids", [])
    published = next_publication.get("published_ids")
    return (_same_json(old.get("scope"), new.get("scope"))
            and isinstance(history, list) and isinstance(next_history, list)
            and _same_json(next_history[:len(history)], history)
            and isinstance(old_published, list) and all(isinstance(key, str) for key in old_published)
            and isinstance(published, list) and all(isinstance(key, str) for key in published)
            and set(old_published) <= set(published) == entries.keys()
            and all(_same_json(entries.get(key), entry) for key, entry in old_entries.items()))


def audit_payloads(root, audit):
    """Return only changed files, preserving old run bytes and protecting the active run."""
    root = Path(root).resolve()
    runs = _audit_runs(audit)
    _require(bool(runs), "Missing summary review batch")
    current = _audit_index(root)
    old_runs = []
    if current is not None:
        old_runs = _load_index(root, current) if current.get("schema_version") == 2 else _audit_runs(current)
    _require(len(runs) >= len(old_runs), "Summary review history lost a batch")
    extendable = None
    for index, (old, new) in enumerate(zip(old_runs, runs)):
        if _same_json(new, old):
            continue
        active = index == len(old_runs) - 1 == len(runs) - 1
        _require(active and old["run_id"] == new["run_id"] and _can_extend(old, new),
                 f"Accepted summary review batch changed: {old['run_id']}")
        extendable = old["run_id"]

    old_descriptors = ({entry["run_id"]: entry for entry in current["runs"]}
                       if current is not None and current.get("schema_version") == 2 else {})
    files, descriptors = {}, []
    for run in runs:
        run_id = run["run_id"]
        relative = _run_file(run_id)
        path = _local_file(root, relative)
        if run_id in old_descriptors and run_id != extendable:
            descriptors.append(old_descriptors[run_id])
            continue
        text = _json_text(run)
        raw = text.encode("utf-8")
        existing = path.read_bytes() if path.exists() else None
        _require(existing in (None, raw) or run_id == extendable,
                 f"Summary review batch already exists with different content: {run_id}")
        if existing != raw:
            files[path] = text
        descriptors.append({"run_id": run_id, "file": relative.as_posix(), "sha256": _sha256(raw)})
    index = {"schema_version": 2, "runs": descriptors, "latest_run_id": runs[-1]["run_id"]}
    if current != index:
        files[_local_file(root, AUDIT_INDEX)] = _json_text(index)
    return files


def _audit_entries(audit):
    return {(run["run_id"], article_id): entry for run in _audit_runs(audit)
            for article_id, entry in _entries_by_id(run).items()}


def _shareable_fields(entry):
    shared = {key: entry[key] for key in ENTRY_FIELDS if key in entry}
    writer = entry.get("writer")
    if isinstance(writer, dict):
        shared.update({key: writer[source] for key, source in WRITER_FIELDS.items() if source in writer})
    return shared


def _expand_event(event, entries):
    if EVIDENCE_REF not in event:
        return event
    ref = event[EVIDENCE_REF]
    key = event.get("run_id"), event.get("id")
    _require(isinstance(ref, dict) and set(ref) == {"entry_sha256", "fields"},
             f"Invalid summary revision evidence reference: {key}")
    entry = entries.get(key)
    _require(entry is not None, f"Missing audit entry for summary revision: {key}")
    _require(ref["entry_sha256"] == _canonical_hash(entry),
             f"Summary revision evidence hash mismatch: {key}")
    fields = ref["fields"]
    shared = _shareable_fields(entry)
    _require(isinstance(fields, list) and bool(fields) and all(isinstance(field, str) for field in fields)
             and len(set(fields)) == len(fields) and set(fields) <= shared.keys() and not set(fields) & event.keys(),
             f"Invalid or overlapping summary revision evidence fields: {key}")
    return {**{name: value for name, value in event.items() if name != EVIDENCE_REF},
            **{name: shared[name] for name in fields}}


def expand_ledger(ledger, audit):
    """Hydrate shared evidence for existing replay and verification callers."""
    if not any(EVIDENCE_REF in event for event in ledger["entries"]):
        return ledger
    entries = _audit_entries(audit)
    return {**ledger, "entries": [_expand_event(event, entries) for event in ledger["entries"]]}


def compact_ledger(ledger, audit):
    """Replace exact evidence copies with hash-bound references, keeping legacy differences inline."""
    entries = _audit_entries(audit)
    events = []
    for stored in ledger["entries"]:
        event = _expand_event(stored, entries)
        entry = entries.get((event.get("run_id"), event.get("id")))
        if "revision_id" not in event or entry is None:
            events.append(event)
            continue
        fields = [name for name, value in _shareable_fields(entry).items()
                  if name in event and _same_json(event[name], value)]
        if fields:
            event = {name: value for name, value in event.items() if name not in fields}
            event[EVIDENCE_REF] = {"entry_sha256": _canonical_hash(entry), "fields": fields}
        events.append(event)
    return {**ledger, "entries": events}
