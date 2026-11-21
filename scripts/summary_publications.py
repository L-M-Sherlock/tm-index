"""Compact, self-contained receipts for released translation summaries."""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import re

from site_catalog import catalog_rows, require


SCHEMA_VERSION = 2
RECEIPT_FILE = "data/summary-revisions.json"
SOURCE_HASHES = ("source_sha256", "content_sha256", "body_sha256")
PROVENANCE = ("reviewer", "writer", "model", "reasoning_effort")
STATUS = ("in_progress", "complete")
RUN_KEYS = {"previous_run_id", "revision_date", "status", "reviewed", "target_scope",
            "audit_sha256", "approvals", "phases", "latest_phase_id"}
APPROVAL_KEYS = {*SOURCE_HASHES, "before_summary_sha256", "summary_sha256", "basis",
                 "decision", "evidence_sha256"}
PHASE_KEYS = {"previous_phase_id", "status", "published_ids", "newly_published_ids"}
REVISION_KEYS = {*SOURCE_HASHES, "article_id", "run_id", "previous_revision_id",
                 "before_summary_sha256", "summary_sha256", "summary", "basis", "revision_date", "reviewer"}
TRANSITION_KEYS = {"previous_source_sha256", "previous_content_sha256", "previous_summary_sha256",
                   "previous_catalog_sha256"}
RETIREMENT_HASHES = ("last_source_sha256", "last_content_sha256", "last_summary_sha256",
                     "previous_catalog_sha256")


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_hash(value):
    return _digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _text(value):
    return isinstance(value, str) and bool(value) and value.strip() == value \
        and not any(ord(character) < 32 for character in value)


def _hash(value, lengths=(64,)):
    return isinstance(value, str) and len(value) in lengths and re.fullmatch(r"[0-9a-f]+", value) is not None


def _date(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except (ValueError, TypeError):
        return False


def _fields(value, required, optional=()):
    return isinstance(value, dict) and set(required) <= value.keys() <= set(required) | set(optional)


def _strings(value):
    return isinstance(value, list) and all(_text(item) for item in value) and len(set(value)) == len(value)


def _source(value):
    return value["source_sha256"], value["content_sha256"]


def empty():
    return {"schema_version": SCHEMA_VERSION, "latest_run_id": None, "runs": {}, "revisions": {},
            "latest_revision_by_article": {}, "retired_articles": {}}


def is_public(value):
    return (isinstance(value, dict) and type(value.get("schema_version")) is int
            and value["schema_version"] == SCHEMA_VERSION)


def read(path):
    """Read receipt JSON without allowing duplicate members to hide record identities."""
    def unique(members):
        value = dict(members)
        require(len(value) == len(members), "Duplicate member in publication receipt JSON")
        return value

    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)


def _chain(records, latest, predecessor, label):
    visited = set()
    result = []
    while latest is not None:
        require(_text(latest) and latest not in visited and latest in records, f"Broken {label} chain")
        visited.add(latest)
        row = records[latest]
        require(isinstance(row, dict) and predecessor in row, f"Invalid {label} record: {latest}")
        result.append((latest, row))
        latest = row[predecessor]
    require(visited == records.keys(), f"Orphaned {label} record")
    result.reverse()
    return result


def _validate_approval(approval, label):
    require(_fields(approval, APPROVAL_KEYS, (*PROVENANCE, "sampling_receipt")), f"Invalid approval: {label}")
    require(all(_hash(approval[key]) for key in (*SOURCE_HASHES, "before_summary_sha256",
                                                 "summary_sha256", "evidence_sha256")),
            f"Invalid approval hashes: {label}")
    require(approval["decision"] in ("retain", "rewrite"), f"Invalid approval decision: {label}")
    if approval["decision"] == "retain":
        require(approval["before_summary_sha256"] == approval["summary_sha256"],
                f"Retained summary changed: {label}")
    fields = set(approval) - APPROVAL_KEYS
    if approval["basis"] == "independent_review":
        require(fields == set(PROVENANCE) and all(_text(approval[key]) for key in fields)
                and approval["writer"] != approval["reviewer"], f"Invalid independent review provenance: {label}")
    else:
        require(approval["basis"] == "sampling_reuse_adequate" and approval["decision"] == "retain"
                and fields == {"sampling_receipt"}, f"Invalid sampling review basis: {label}")
        receipt = approval["sampling_receipt"]
        require(_fields(receipt, ("run_id", "report_sha256", "entry_sha256"))
                and _text(receipt["run_id"]) and _hash(receipt["report_sha256"]) and _hash(receipt["entry_sha256"]),
                f"Invalid sample reuse receipt: {label}")


def _validate_run(run_id, run):
    require(_fields(run, RUN_KEYS) and _date(run["revision_date"]) and _hash(run["audit_sha256"])
            and run["status"] in STATUS and isinstance(run["approvals"], dict)
            and type(run["reviewed"]) is int and run["reviewed"] == len(run["approvals"])
            and type(run["target_scope"]) is int and run["target_scope"] >= run["reviewed"] > 0,
            f"Invalid publication run: {run_id}")
    require((run["status"] == "complete") == (run["target_scope"] == run["reviewed"]),
            f"Publication run scope conflicts with status: {run_id}")
    approvals = run["approvals"]
    for article_id, approval in approvals.items():
        require(_text(article_id), f"Invalid approved article ID: {run_id}")
        _validate_approval(approval, f"{run_id}/{article_id}")
    require(isinstance(run["phases"], dict), f"Invalid publication phases: {run_id}")
    prior = set()
    last_status = None
    for phase_id, stage in _chain(run["phases"], run["latest_phase_id"], "previous_phase_id", "publication phase"):
        require(_hash(phase_id) and _fields(stage, PHASE_KEYS) and stage["status"] in STATUS
                and _strings(stage["published_ids"]) and _strings(stage["newly_published_ids"]),
                f"Invalid publication phase: {run_id}/{phase_id}")
        published = set(stage["published_ids"])
        require(prior < published and published <= approvals.keys()
                and set(stage["newly_published_ids"]) == published - prior,
                f"Publication phase is not cumulative: {run_id}/{phase_id}")
        require((stage["status"] == "complete") == (len(published) == run["target_scope"]),
                f"Publication phase scope conflicts with status: {run_id}/{phase_id}")
        prior, last_status = published, stage["status"]
    if run["phases"] or run["status"] == "in_progress":
        require(prior == approvals.keys() and last_status == run["status"],
                f"Latest phase differs from publication approvals: {run_id}")


def _revision_id(revision):
    if revision["basis"] == "legacy_codex":
        keys = ("article_id", *SOURCE_HASHES, "before_summary_sha256", "summary_sha256", "revision_date")
        return "legacy-" + _canonical_hash({key: revision[key] for key in keys if key in revision})
    keys = ("run_id", "article_id", "source_sha256", "body_sha256", "before_summary_sha256", "summary_sha256")
    fingerprints = [revision[key] for key in keys]
    if "source_transition" in revision:
        fingerprints.append(_canonical_hash(revision["source_transition"]))
    return _digest("\n".join(fingerprints))


def _validate_revision(revision_id, row, runs):
    legacy = isinstance(row, dict) and row.get("basis") == "legacy_codex"
    fields = REVISION_KEYS - {"body_sha256"} if legacy else REVISION_KEYS
    optional = ("body_sha256", *PROVENANCE) if legacy else (*PROVENANCE, "source_transition")
    require(_fields(row, fields, optional) and _text(row["article_id"])
            and _date(row["revision_date"]) and _text(row["reviewer"])
            and isinstance(row["summary"], str) and bool(row["summary"].strip())
            and all(_hash(row[key]) for key in (*SOURCE_HASHES, "before_summary_sha256", "summary_sha256") if key in row)
            and _digest(row["summary"]) == row["summary_sha256"], f"Invalid summary revision: {revision_id}")
    if legacy:
        require(row["run_id"] is None and row["reviewer"] == "Codex"
                and all(_text(row[key]) for key in PROVENANCE if key in row),
                f"Invalid legacy Codex receipt: {revision_id}")
    else:
        run_id = row["run_id"]
        require(row["basis"] == "independent_review" and _text(run_id) and run_id in runs,
                f"Missing publication run: {revision_id}")
        run = runs[run_id]
        approval = run["approvals"].get(row["article_id"])
        fields = (*SOURCE_HASHES, *PROVENANCE, "basis", "before_summary_sha256", "summary_sha256")
        require(approval is not None and all(row.get(key) == approval.get(key) for key in fields)
                and approval["decision"] == ("retain" if row["before_summary_sha256"] == row["summary_sha256"]
                                              else "rewrite")
                and row["revision_date"] == run["revision_date"], f"Revision differs from its approval: {revision_id}")
        require(approval["decision"] == "rewrite" or "source_transition" in row,
                f"Unchanged revision has no source transition: {revision_id}")
    if "source_transition" in row:
        transition = row["source_transition"]
        require(_fields(transition, TRANSITION_KEYS) and all(_hash(value) for value in transition.values())
                and transition["previous_source_sha256"] != row["source_sha256"]
                and transition["previous_content_sha256"] != row["content_sha256"]
                and transition["previous_summary_sha256"] == row["before_summary_sha256"],
                f"Invalid revision source transition: {revision_id}")
    require(revision_id == _revision_id(row), f"Summary revision ID does not match its receipt: {revision_id}")


def _validate_retirement(article_id, receipt, last):
    require(_fields(receipt, (*RETIREMENT_HASHES, "archive_base", "archive_tip", "reason"))
            and all(_hash(receipt[key]) for key in RETIREMENT_HASHES) and _text(receipt["reason"])
            and all(_hash(receipt[key], (40, 64)) for key in ("archive_base", "archive_tip"))
            and receipt["archive_base"] != receipt["archive_tip"],
            f"{article_id}: invalid summary retirement receipt")
    require((receipt["last_source_sha256"], receipt["last_content_sha256"]) == _source(last)
            and receipt["last_summary_sha256"] == last["summary_sha256"],
            f"{article_id}: retirement differs from the last accepted summary or source")


def _validated_chains(value):
    require(_fields(value, empty().keys()) and is_public(value)
            and all(isinstance(value[key], dict)
                    for key in ("runs", "revisions", "latest_revision_by_article", "retired_articles")),
            "Invalid summary publication receipt file")
    runs = value["runs"]
    run_positions = {}
    for position, (run_id, run) in enumerate(
            _chain(runs, value["latest_run_id"], "previous_run_id", "summary publication")):
        run_positions[run_id] = position
        _validate_run(run_id, run)
    by_article = {}
    approved_revisions = set()
    for revision_id, revision in value["revisions"].items():
        _validate_revision(revision_id, revision, runs)
        by_article.setdefault(revision["article_id"], {})[revision_id] = revision
        if revision["run_id"] is not None:
            approval_key = (revision["run_id"], revision["article_id"])
            require(approval_key not in approved_revisions, f"Duplicate summary revision for {approval_key}")
            approved_revisions.add(approval_key)
    for run_id, run in runs.items():
        for article_id, approval in run["approvals"].items():
            require(approval["decision"] != "rewrite" or (run_id, article_id) in approved_revisions,
                    f"Missing accepted revision: {run_id}/{article_id}")
    latest = value["latest_revision_by_article"]
    retired = value["retired_articles"]
    require(latest.keys() == by_article.keys() and retired.keys() <= by_article.keys(),
            "Latest revision or retirement index differs from its article chains")
    chains = {}
    for article_id in sorted(by_article):
        chain = _chain(by_article[article_id], latest[article_id], "previous_revision_id",
                       f"revision history for {article_id}")
        previous = None
        previous_position = -2
        for _, revision in chain:
            position = run_positions.get(revision["run_id"], -1)
            require(position > previous_position, f"{article_id}: revision parents conflict with publication history")
            transition = revision.get("source_transition")
            if previous is not None:
                require(previous["summary_sha256"] == revision["before_summary_sha256"],
                        f"{article_id}: broken revision history")
                expected_source = ((transition["previous_source_sha256"], transition["previous_content_sha256"])
                                   if transition is not None else _source(revision))
                require(_source(previous) == expected_source,
                        f"{article_id}: broken revision source transition")
            previous, previous_position = revision, position
        if article_id in retired:
            _validate_retirement(article_id, retired[article_id], previous)
        chains[article_id] = chain
    return chains


def validate(value):
    """Reject dangling, duplicated, conflicting, or non-compact release receipts."""
    _validated_chains(value)
    return value


def _approval(entry):
    writer, review = entry.get("writer"), entry.get("review")
    decision = entry.get("decision", writer.get("decision") if isinstance(writer, dict) else None)
    source = entry["source"]
    before = entry["before_summary_sha256"]
    result = {**{key: source[key] for key in SOURCE_HASHES}, "before_summary_sha256": before,
              "decision": decision, "evidence_sha256": _canonical_hash(entry)}
    if isinstance(review, dict) and review.get("verdict") == "pass" and isinstance(writer, dict):
        summary_hash = _digest(writer["summary"])
        require(review["summary_sha256"] == summary_hash and review["source_sha256"] == source["source_sha256"]
                and review["body_sha256"] == source["body_sha256"],
                f"{entry['id']}: independent review differs from its input or accepted summary")
        return {**result, "basis": "independent_review", "summary_sha256": summary_hash,
                "reviewer": review["reviewer"], "writer": writer["writer"], "model": review["model"],
                "reasoning_effort": review["reasoning_effort"]}
    sample, prior = entry.get("reused_audit"), entry.get("prior_review")
    require(writer is None and review is None and entry.get("review_origin") == "sampling_reuse_adequate"
            and isinstance(sample, dict) and isinstance(prior, dict) and sample["final_verdict"] == "adequate"
            and prior["final_verdict"] == "adequate" and _canonical_hash(prior) == sample["entry_sha256"]
            and prior["summary_sha256"] == before, f"{entry['id']}: no accepted summary review evidence")
    return {**result, "basis": "sampling_reuse_adequate", "summary_sha256": before,
            "sampling_receipt": {key: sample[key] for key in ("run_id", "report_sha256", "entry_sha256")}}


def _run_receipt(audit, previous_run_id):
    phases, latest = {}, None
    for phase in audit.get("publication_history", []):
        phase_id = phase["phase_id"]
        require(phase_id not in phases, "Duplicate publication phase IDs")
        phases[phase_id] = {"previous_phase_id": latest, **{key: phase[key] for key in
                                                           ("status", "published_ids", "newly_published_ids")}}
        latest = phase_id
    approvals = {}
    for entry in audit["entries"]:
        article_id = entry["id"]
        require(article_id not in approvals, f"Duplicate approved article: {article_id}")
        approvals[article_id] = _approval(entry)
    published = audit.get("publication", {})
    scope = audit.get("scope", {}).get("article_ids", list(approvals))
    require(_strings(scope) and approvals.keys() <= set(scope), "Invalid publication scope")
    if published:
        require(published.get("target_scope") == len(scope)
                and _strings(published.get("published_ids")) and set(published["published_ids"]) == approvals.keys()
                and _strings(published.get("pending_ids"))
                and set(published["pending_ids"]) == set(scope) - approvals.keys(),
                "Published receipt differs from audit approvals")
    return {"previous_run_id": previous_run_id, "revision_date": audit["revision_date"],
            "status": published.get("status", "complete"), "reviewed": len(approvals),
            "target_scope": len(scope), "audit_sha256": _canonical_hash(
                {key: item for key, item in audit.items() if key != "previous_runs"}),
            "approvals": approvals, "phases": phases, "latest_phase_id": latest}


def _add_revision(value, event):
    article_id = event["id"]
    run_id = event.get("run_id")
    source, after = event["source"], event["after"]
    legacy = "revision_id" not in event
    row = {"article_id": article_id, "run_id": run_id, "previous_revision_id": None,
           "source_sha256": source["source_sha256"], "content_sha256": source["content_sha256"],
           "before_summary_sha256": _digest(event["before"]["summary"]),
           "summary_sha256": _digest(after["summary"]), "summary": after["summary"],
           "basis": "legacy_codex" if legacy else "independent_review", "revision_date": after["revision_date"],
           **{key: after[key] for key in PROVENANCE if key in after}}
    if "visible_text_sha256" in source:
        row["body_sha256"] = source["visible_text_sha256"]
    if "source_transition" in event:
        row["source_transition"] = copy.deepcopy(event["source_transition"])
    revision_id = _revision_id(row) if legacy else event["revision_id"]
    existing = value["revisions"].get(revision_id)
    if existing is not None:
        row["previous_revision_id"] = existing["previous_revision_id"]
        require(row == existing, f"Accepted revision differs from receipt: {revision_id}")
        return
    require(article_id not in value["retired_articles"], f"Cannot append revision for retired article: {article_id}")
    row["previous_revision_id"] = value["latest_revision_by_article"].get(article_id)
    value["latest_revision_by_article"][article_id] = revision_id
    value["revisions"][revision_id] = row


def migrate(ledger, audit):
    """Project the validated legacy ledger and complete public audit into compact receipts."""
    value = empty()
    if audit is not None:
        for run in [*audit.get("previous_runs", []), audit]:
            run_id = run["run_id"]
            require(run_id not in value["runs"], f"Duplicate publication run: {run_id}")
            value["runs"][run_id] = _run_receipt(run, value["latest_run_id"])
            value["latest_run_id"] = run_id
    for event in ledger["entries"]:
        size = len(value["revisions"])
        _add_revision(value, event)
        require(len(value["revisions"]) > size, "Duplicate legacy ledger revision")
    value["retired_articles"] = copy.deepcopy(ledger.get("retired_articles", {}))
    return validate(value)


def update(value, audit, new_events=(), retired_articles=None):
    """Append accepted results or continue the current phase without rewriting prior receipts."""
    result = copy.deepcopy(validate(value))
    if audit is not None:
        run_id = audit["run_id"]
        latest = result["latest_run_id"]
        runs = result["runs"]
        prior = runs.get(run_id)
        candidate = _run_receipt(audit, latest if prior is None else prior["previous_run_id"])
        if prior is not None:
            require(run_id == latest, "Cannot apply an archived review run")
            if prior != candidate:
                require(prior["status"] == "in_progress" and prior["target_scope"] == candidate["target_scope"]
                        and prior["revision_date"] == candidate["revision_date"]
                        and all(candidate["approvals"].get(key) == item for key, item in prior["approvals"].items())
                        and all(candidate["phases"].get(key) == item for key, item in prior["phases"].items()),
                        f"Accepted publication receipts changed: {run_id}")
            for field in ("approvals", "phases"):
                previous = prior[field]
                candidate[field] = {**previous, **{key: item for key, item in candidate[field].items()
                                                  if key not in previous}}
            candidate = {key: candidate[key] for key in prior}
        runs[run_id] = candidate
        result["latest_run_id"] = run_id
    for event in new_events:
        require(audit is not None and event.get("run_id") == audit["run_id"],
                "Revision event does not belong to this publication run")
        _add_revision(result, event)
    if retired_articles is not None:
        require(all(retired_articles.get(key) == item for key, item in result["retired_articles"].items()),
                "Accepted retirement receipts changed")
        result["retired_articles"] = copy.deepcopy(retired_articles)
    return validate(result)


def _adapter(revision_id, row):
    event = {"id": row["article_id"], "run_id": row["run_id"], "source": {
        "source_sha256": row["source_sha256"], "content_sha256": row["content_sha256"]},
        "before": {"summary_sha256": row["before_summary_sha256"]},
        "after": {key: row[key] for key in ("summary", "summary_sha256", "revision_date", *PROVENANCE) if key in row}}
    if "body_sha256" in row:
        event["source"]["visible_text_sha256"] = row["body_sha256"]
    if row["basis"] != "legacy_codex":
        event["revision_id"] = revision_id
    if "source_transition" in row:
        event["source_transition"] = row["source_transition"]
    return event


def ordered_revisions(value):
    """Return minimal chronological events per article for existing maintenance callers."""
    return [_adapter(revision_id, row) for chain in _validated_chains(value).values()
            for revision_id, row in chain]


def accepted_versions(value):
    """Return source-bound versions with current-workflow approval for archive validation."""
    validate(value)
    return {(article_id, entry["source_sha256"], entry["content_sha256"], entry["summary_sha256"])
            for run in value["runs"].values() for article_id, entry in run["approvals"].items()}


def replay(catalog, value):
    """Restore known published versions without requiring private review evidence."""
    rows = catalog_rows(catalog)
    chains = _validated_chains(value)
    retired = value["retired_articles"]
    require(not (retired.keys() & rows.keys()), "Retired article still appears in the catalog")
    replacements = {}
    for article_id, events in chains.items():
        if article_id in retired:
            continue
        require(article_id in rows, f"Revision target absent from import: {article_id}")
        revision_id, latest = events[-1]
        row = rows[article_id]
        require(_source(latest) == _source(row), f"{article_id}: revision source conflicts with import")
        known = set()
        for _, event in events:
            if "source_transition" in event:
                known.clear()
            known.update((event["before_summary_sha256"], event["summary_sha256"]))
        require(_digest(row["summary"]) in known,
                f"{article_id}: imported summary is outside accepted history; review required")
        tag = {"date": latest["revision_date"], "reviewer": latest["reviewer"], "review_file": RECEIPT_FILE,
               **{key: latest[key] for key in ("writer", "model", "reasoning_effort") if key in latest}}
        if latest["basis"] != "legacy_codex":
            tag["revision_id"] = revision_id
        replacements[article_id] = (latest["summary"], tag)
    retained = {}
    for _, run in _chain(value["runs"], value["latest_run_id"], "previous_run_id", "summary publication"):
        for article_id, approval in run["approvals"].items():
            if article_id in rows and article_id not in chains and _source(approval) == _source(rows[article_id]):
                retained[article_id] = approval["summary_sha256"]
    for article_id, summary_hash in retained.items():
        require(_digest(rows[article_id]["summary"]) == summary_hash,
                f"{article_id}: retained summary differs from its approval")
    for article_id, (summary, tag) in replacements.items():
        rows[article_id].update(summary=summary, summary_revision=tag)
    return catalog
