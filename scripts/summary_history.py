"""Store complete review evidence privately and read legacy publication history."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path


AUDIT_INDEX = Path("data/summary-reviews.json")
AUDIT_RUNS = Path("data/summary-reviews")
DEFAULT_AUDIT_FILE = Path("~/.local/share/tm-index/summary-audit.json")
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


def load_audit(root, audit_file=None):
    """Read private evidence, or read the old tracked format for migration."""
    root = Path(root).resolve()
    if audit_file is not None:
        return _private_audit(load_private(private_audit_path(root, audit_file)))
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


def private_audit_path(root, path=None):
    """Allow a local archive outside a checkout, or inside its ignored .work directory."""
    root = Path(root).resolve()
    path = Path(path if path is not None else DEFAULT_AUDIT_FILE).expanduser()
    path = (root / path).resolve()
    _require(path.suffix == ".json", "The private summary audit must be a JSON file")
    for checkout in {root, *(folder for folder in path.parents if (folder / ".git").exists())}:
        if path.is_relative_to(checkout):
            _require(path.is_relative_to(checkout / ".work"),
                     "Private summary audit must be outside Git or under .work/")
    return path


@contextmanager
def private_lock(path):
    """Serialize caller transactions that read and update the same private archive."""
    path = _private_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _private_path(path):
    path = Path(path).expanduser().resolve()
    checkout = next((folder for folder in path.parents if (folder / ".git").exists()), None)
    if checkout is not None:
        private_audit_path(checkout, path)
    _require(path.suffix == ".json", "The private summary audit must be a JSON file")
    return path


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"Duplicate key in private summary audit: {key}")
        result[key] = value
    return result


def load_private(path):
    """Read the single private archive as maps keyed by stable IDs."""
    path = Path(path)
    if not path.exists():
        return {}
    value = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    _require(isinstance(value, dict) and set(value) == {
        "schema_version", "latest_run_id", "runs", "ledger", "sampling", "reports",
    } and type(value["schema_version"]) is int and value["schema_version"] == 1
        and all(isinstance(value[key], dict) for key in ("runs", "sampling", "reports")),
        "Invalid private summary archive")
    return value


def _chain(nodes, latest, predecessor, label):
    """Resolve a complete linear chain without relying on object member order."""
    chain, visited = [], set()
    while latest is not None:
        _require(isinstance(latest, str) and latest in nodes and latest not in visited,
                 f"Invalid or cyclic {label} chain")
        node = nodes[latest]
        _require(isinstance(node, dict) and predecessor in node, f"Missing {label} predecessor")
        chain.append((latest, node))
        visited.add(latest)
        latest = node[predecessor]
    _require(len(visited) == len(nodes), f"Disconnected {label} chain")
    return reversed(chain)


def _phase_id(record):
    _require(isinstance(record, dict), "Invalid publication phase")
    identifier = record["phase_id"] if "phase_id" in record else _canonical_hash(record)
    _require(isinstance(identifier, str) and identifier, "Missing publication phase ID")
    return identifier


def _encode_run(run, previous_run_id):
    entries = _entries_by_id(run)
    node = {
        "previous_run_id": previous_run_id,
        "fields": {key: value for key, value in run.items() if key not in {"entries", "publication_history"}},
        "entry_order": list(entries),
        "entries": entries,
    }
    if "publication_history" in run:
        history = run["publication_history"]
        _require(isinstance(history, list), "Invalid publication phase history")
        latest, phases = None, {}
        for phase in history:
            identifier = _phase_id(phase)
            _require(identifier not in phases, "Duplicate publication phase ID")
            phases[identifier] = {"previous_phase_id": latest, "record": phase}
            latest = identifier
        node.update(latest_phase_id=latest, phases=phases)
    return node


def _decode_run(run_id, node):
    fields = node.get("fields")
    entries, order = node.get("entries"), node.get("entry_order")
    _require(isinstance(fields, dict) and fields.get("run_id") == run_id
             and not {"entries", "publication_history", "previous_runs"} & fields.keys()
             and isinstance(entries, dict) and isinstance(order, list)
             and all(isinstance(article_id, str) for article_id in order)
             and len(order) == len(entries) and entries.keys() == set(order),
             f"Invalid private review run: {run_id}")
    run = {**fields, "entries": [entries[article_id] for article_id in order]}
    _require(_entries_by_id(run).keys() == entries.keys()
             and all(entry.get("id") == article_id for article_id, entry in entries.items()),
             f"Private review article ID mismatch: {run_id}")
    if "phases" in node or "latest_phase_id" in node:
        _require(isinstance(node.get("phases"), dict) and "latest_phase_id" in node,
                 f"Invalid private publication history: {run_id}")
        phases = []
        for identifier, phase in _chain(node["phases"], node["latest_phase_id"], "previous_phase_id", "publication phase"):
            record = phase.get("record")
            _require(identifier == _phase_id(record), f"Private publication phase ID mismatch: {run_id}")
            phases.append(record)
        run["publication_history"] = phases
    return run


def _private_audit(state):
    if not state:
        return None
    runs, identifiers = [], set()
    for run_id, node in _chain(state["runs"], state["latest_run_id"], "previous_run_id", "review run"):
        _run_file(run_id)
        _require(run_id.casefold() not in identifiers, f"Duplicate private review run: {run_id}")
        identifiers.add(run_id.casefold())
        runs.append(_decode_run(run_id, node))
    return {**runs[-1], "previous_runs": runs[:-1]} if runs else None


def _revision_key(event):
    _require(isinstance(event, dict), "Invalid private revision event")
    key = event["revision_id"] if "revision_id" in event else "legacy-" + _canonical_hash(event)
    _require(isinstance(key, str) and key, "Missing private revision ID")
    return key


def _decode_ledger(state):
    node = state.get("ledger")
    if node is None:
        return None
    _require(isinstance(node, dict) and isinstance(node.get("fields"), dict)
             and "entries" not in node["fields"] and isinstance(node.get("revisions"), dict)
             and "latest_revision_id" in node, "Invalid private revision ledger")
    events = []
    for key, revision in _chain(node["revisions"], node["latest_revision_id"], "previous_revision_id", "revision"):
        event = revision.get("event")
        _require(key == _revision_key(event), "Private revision ID mismatch")
        events.append(event)
    return {**node["fields"], "entries": events}


def load_private_ledger(path):
    """Recover the complete original ledger from the private archive."""
    return _decode_ledger(load_private(path))


def _store_audit(state, audit):
    current = _audit_runs(_private_audit(state))
    incoming = _audit_runs(audit)
    _require(incoming and len(incoming) >= len(current), "Private review history lost a batch")
    for index, (old, new) in enumerate(zip(current, incoming)):
        active = index == len(current) - 1 == len(incoming) - 1
        _require(_same_json(old, new) or active and old["run_id"] == new["run_id"] and _can_extend(old, new),
                 f"Accepted summary review batch changed: {old['run_id']}")
    previous = None
    for run in incoming:
        run_id = run["run_id"]
        node = _encode_run(run, previous)
        old = state["runs"].get(run_id)
        if not _same_json(old, node):
            if old is not None:
                node["entries"] = {**old["entries"], **{
                    key: value for key, value in node["entries"].items() if key not in old["entries"]
                }}
                if "phases" in node:
                    phases = old.get("phases", {})
                    node["phases"] = {**phases, **{
                        key: value for key, value in node["phases"].items() if key not in phases
                    }}
            state["runs"][run_id] = node
        previous = run_id
    state["latest_run_id"] = previous


def _store_ledger(state, ledger):
    _require(isinstance(ledger, dict) and isinstance(ledger.get("entries"), list), "Invalid revision ledger")
    archived = _decode_ledger(state)
    old_events = archived["entries"] if archived is not None else []
    events = ledger["entries"]
    _require(_same_json(old_events, events[:len(old_events)]), "Accepted private revision history changed")
    if archived is not None:
        retired = ledger.get("retired_articles", {})
        _require(all(_same_json(receipt, retired.get(article_id))
                     for article_id, receipt in archived.get("retired_articles", {}).items()),
                 "Accepted private revision retirement changed")
    node = state["ledger"] or {"revisions": {}, "latest_revision_id": None}
    revisions = node["revisions"]
    latest = node["latest_revision_id"]
    for event in events[len(old_events):]:
        key = _revision_key(event)
        _require(key not in revisions, "Duplicate private revision ID")
        revisions[key] = {"previous_revision_id": latest, "event": event}
        latest = key
    state["ledger"] = {
        "fields": {key: value for key, value in ledger.items() if key != "entries"},
        "latest_revision_id": latest,
        "revisions": revisions,
    }


def _store_records(state, key, records):
    _require(isinstance(records, dict), f"Invalid private {key} records")
    stored = state[key]
    for identifier, value in records.items():
        _require(isinstance(identifier, str) and identifier, f"Invalid private {key} record ID")
        if identifier in stored:
            _require(_same_json(value, stored[identifier]), f"Accepted private {key} record changed: {identifier}")
        else:
            stored[identifier] = value


def private_payload(path, *, audit=None, ledger=None, sampling=None, reports=None):
    """Prepare one private file update, for use in the caller's staged transaction."""
    path = _private_path(path)
    state = load_private(path)
    original = _canonical_hash(state)
    if not state:
        state = {"schema_version": 1, "latest_run_id": None, "runs": {}, "ledger": None,
                 "sampling": {}, "reports": {}}
    if audit is not None:
        _store_audit(state, audit)
    if ledger is not None:
        _store_ledger(state, ledger)
    if sampling is not None:
        _store_records(state, "sampling", sampling)
    if reports is not None:
        _store_records(state, "reports", reports)
    return {} if _canonical_hash(state) == original else {path: _json_text(state)}


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
