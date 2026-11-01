#!/usr/bin/env python3
"""Prepare, validate and apply human/agent-authored summaries. No model calls."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-6-luna"
EFFORT = "max"
DATA_PATTERN = re.compile(r'(<script id="catalog-data" type="application/json">)(.*?)(</script>)', re.S)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_text(value), encoding="utf-8")


def confined(work, relative):
    path = (work / relative).resolve()
    require(path.is_relative_to(work.resolve()), f"Path escapes work directory: {relative}")
    return path


def catalog_rows(catalog):
    require(isinstance(catalog, dict) and isinstance(catalog.get("articles"), list), "Invalid catalog")
    rows = {row["id"]: row for row in catalog["articles"]}
    require(len(rows) == len(catalog["articles"]), "Duplicate catalog IDs")
    return rows


def provenance(row):
    return {key: row[key] for key in ("model", "reasoning_effort", "worker_agent", "worker_shard") if key in row}


def assign_articles(items):
    assignments = {}
    for role, count in (("writer", 6), ("reviewer", 2)):
        loads, groups = [0] * count, [[] for _ in range(count)]
        for item in items:
            index = min(range(count), key=lambda i: loads[i])
            groups[index].append(item["id"])
            loads[index] += item["read_chars"]
        assignments[role] = {f"long_{role}_{index}": ids for index, ids in enumerate(groups, 1)}
    return assignments


def prepare(root, source_dir, source_manifest, work, run_id, revision_date, minimum, chunk_chars=6000):
    require(0 < chunk_chars <= 6000, "chunk_chars must be between 1 and 6000")
    require(minimum > 0, "min_body_chars must be positive")
    require(bool(run_id.strip()), "run_id must not be empty")
    date.fromisoformat(revision_date)
    require(not (work / "manifest.json").exists(), "Work manifest already exists; resume with check/apply or use a new work directory")
    catalog = read_json(root / "data/catalog.json")
    source_data = read_json(source_manifest)
    baseline = {row["id"]: row for row in source_data["articles"]} if "articles" in source_data else source_data
    selected = sorted((row for row in catalog["articles"] if row["read_chars"] >= minimum), key=lambda row: (-row["read_chars"], row["id"]))
    require(selected, "No articles match the requested scope")
    manifest = {"schema_version": 1, "run_id": run_id, "revision_date": revision_date,
                "min_body_chars": minimum, "chunk_chars": chunk_chars, "model": MODEL,
                "reasoning_effort": EFFORT, "articles": []}
    prepared = []
    for row in selected:
        text = (source_dir / (row["id"] + ".txt")).read_text(encoding="utf-8")
        header = json.loads(text.splitlines()[0])
        start, end = "【完整可见正文开始】\n", "\n【完整可见正文结束】"
        require(text.count(start) == text.count(end) == 1, f"{row['id']}: missing/duplicate body markers")
        body = text.split(start, 1)[1].split(end, 1)[0]
        require(header["id"] == row["id"] and header["title"] == row["title"], f"{row['id']}: snapshot identity mismatch")
        require(header["text_chars"] == row["read_chars"] == len(body), f"{row['id']}: body length mismatch")
        expected = baseline.get(row["id"])
        require(isinstance(expected, dict), f"{row['id']}: source manifest needs source/content/text hashes")
        require(expected.get("source_sha256") == row["source_sha256"], f"{row['id']}: source hash mismatch")
        require(expected.get("content_sha256") == row["content_sha256"], f"{row['id']}: content hash mismatch")
        require(expected.get("text_sha256") == digest(body) and expected.get("text_chars") == len(body), f"{row['id']}: snapshot text differs from trusted source manifest")
        for field in ("publisher", "publisher_token"):
            require(header.get(field) == row.get(field), f"{row['id']}: publisher mismatch")
        item = {key: row[key] for key in ("id", "title", "url", "archive_url", "source_path", "source_sha256", "content_sha256", "read_chars", "summary")}
        item.update(old_summary_sha256=digest(row["summary"]), body_sha256=digest(body), initial_provenance=provenance(row), chunks=[])
        chunks = []
        for index, offset in enumerate(range(0, len(body), chunk_chars)):
            relative = f"inputs/{row['id']}/chunk-{index:02}.txt"
            part = body[offset:offset + chunk_chars]
            item["chunks"].append({"index": index, "start": offset, "end": offset + len(part), "path": relative})
            chunks.append((relative, part))
        manifest["articles"].append(item)
        prepared.append((item, chunks))
    # Finish checking every source before creating work files.
    for item, chunks in prepared:
        for relative, part in chunks:
            path = confined(work, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(part, encoding="utf-8")
        write_json(work / "inputs" / item["id"] / "article.json", item)
    manifest["assignments"] = assign_articles(manifest["articles"])
    write_json(work / "manifest.json", manifest)
    for role, groups in manifest["assignments"].items():
        for agent, assignment in groups.items():
            index = agent.rsplit("_", 1)[1]
            write_json(work / f"{role}-{index}-assignment.json", assignment)
    return {"run_id": run_id, "articles": len(selected), "body_characters": sum(row["read_chars"] for row in selected)}


def validate_body(work, item):
    require(read_json(work / "inputs" / item["id"] / "article.json") == item, f"{item['id']}: article input differs from manifest")
    require(digest(item["summary"]) == item["old_summary_sha256"], f"{item['id']}: old summary hash mismatch")
    offset, parts = 0, []
    for index, chunk in enumerate(item["chunks"]):
        require(chunk["index"] == index and chunk["start"] == offset, f"{item['id']}: noncontiguous chunks")
        text = confined(work, chunk["path"]).read_text(encoding="utf-8")
        require(0 < len(text) <= 6000 and chunk["end"] == offset + len(text), f"{item['id']}: invalid chunk length")
        parts.append(text)
        offset = chunk["end"]
    body = "".join(parts)
    require(offset == item["read_chars"] and digest(body) == item["body_sha256"], f"{item['id']}: body hash/length mismatch")
    return body


def nonempty(value, message):
    require(isinstance(value, str) and bool(value.strip()), message)


def validate_reading(record, item):
    expected = list(range(len(item["chunks"])))
    chunks = record.get("read_chunks")
    require(isinstance(chunks, list) and all(type(i) is int for i in chunks), f"{item['id']}: invalid read_chunks")
    require(sorted(chunks) == expected, f"{item['id']}: incomplete/duplicate read_chunks")
    for key in ("source_sha256", "body_sha256"):
        require(record.get(key) == item[key], f"{item['id']}: {key} mismatch")
    require(record.get("id") == item["id"], f"{item['id']}: result ID mismatch")
    require(record.get("model") == MODEL and record.get("reasoning_effort") == EFFORT, f"{item['id']}: incorrect model provenance")


def validate_spans(point, item):
    spans = point.get("source_spans")
    require(isinstance(spans, list) and bool(spans), f"{item['id']}: missing source spans")
    for span in spans:
        start, end = span.get("start"), span.get("end")
        require(type(start) is int and type(end) is int and 0 <= start < end <= item["read_chars"], f"{item['id']}: source span outside body")
        require(end - start >= 8, f"{item['id']}: source span is too short to support a major point")


def validate_evidence(text, summary, item):
    nonempty(text, f"{item['id']}: missing summary evidence")
    require(sum(char.isalnum() for char in text) >= 8, f"{item['id']}: summary evidence is too short to be substantive")
    require(text in summary, f"{item['id']}: evidence is absent from reviewed summary: {text[:70]}")


def validate_writer(writer, item):
    validate_reading(writer, item)
    nonempty(writer.get("writer"), f"{item['id']}: missing writer identity")
    require(writer.get("old_summary_sha256") == item["old_summary_sha256"], f"{item['id']}: writer used an outdated summary")
    for key in ("genre", "decision_reason", "summary", "length_reason"):
        nonempty(writer.get(key), f"{item['id']}: missing {key}")
    decision = writer.get("decision")
    require(decision in {"retain", "rewrite"}, f"{item['id']}: invalid decision")
    require((writer["summary"] == item["summary"]) == (decision == "retain"), f"{item['id']}: decision does not match summary change")
    points = writer.get("major_points")
    require(isinstance(points, list) and bool(points), f"{item['id']}: no major points")
    keys = []
    for point in points:
        nonempty(point.get("key"), f"{item['id']}: missing major point key")
        nonempty(point.get("point"), f"{item['id']}: empty major point")
        keys.append(point["key"])
        validate_spans(point, item)
        require(point.get("old_coverage") in {"covered", "partial", "missing", "distorted"}, f"{item['id']}: invalid old coverage")
        nonempty(point.get("old_coverage_reason"), f"{item['id']}: missing old coverage rationale")
        validate_evidence(point.get("summary_evidence"), writer["summary"], item)
    require(len(keys) == len(set(keys)), f"{item['id']}: duplicate major point keys")
    issues = writer.get("issues")
    require(isinstance(issues, list), f"{item['id']}: invalid issues")
    require(decision != "rewrite" or bool(issues), f"{item['id']}: rewrite has no explanation")
    for issue in issues:
        nonempty(issue.get("problem"), f"{item['id']}: empty issue")
        nonempty(issue.get("fix"), f"{item['id']}: empty fix")
    require(decision != "retain" or all(p["old_coverage"] == "covered" for p in points), f"{item['id']}: retained summary has uncovered major points")


def validate_reviewer(review, writer, item):
    validate_reading(review, item)
    nonempty(review.get("reviewer"), f"{item['id']}: missing reviewer identity")
    require(review["reviewer"] != writer["writer"], f"{item['id']}: reviewer is not independent")
    require(review.get("summary_sha256") == digest(writer["summary"]), f"{item['id']}: review is for a stale draft")
    require(review.get("verdict") == "pass", f"{item['id']}: review has not passed")
    checks = review.get("major_point_checks")
    require(isinstance(checks, list), f"{item['id']}: missing major point checks")
    expected = {p["key"] for p in writer["major_points"]}
    require(len(checks) == len(expected) and {p.get("key") for p in checks} == expected, f"{item['id']}: missing/duplicate point checks")
    for point in checks:
        require(point.get("verdict") == "covered", f"{item['id']}: unresolved major point")
        validate_evidence(point.get("evidence"), writer["summary"], item)
    independent = review.get("independent_major_points")
    require(isinstance(independent, list) and bool(independent), f"{item['id']}: missing independent outline")
    for point in independent:
        nonempty(point.get("point"), f"{item['id']}: empty independent point")
        validate_spans(point, item)
        validate_evidence(point.get("summary_evidence"), writer["summary"], item)
    require(review.get("additional_major_points") == [], f"{item['id']}: additional major points remain uncovered")
    require(isinstance(review.get("notes"), list) and bool(review["notes"]), f"{item['id']}: missing review conclusion")


def load_ledger(root):
    path = root / "data/summary-revisions.json"
    return read_json(path) if path.exists() else {"schema_version": 1, "entries": []}


def event_id(item, summary, run_id):
    return digest("\n".join((run_id, item["id"], item["source_sha256"], item["body_sha256"], item["old_summary_sha256"], digest(summary))))


def check(root, work, allow_incomplete=False):
    manifest = read_json(work / "manifest.json")
    require(manifest.get("model") == MODEL and manifest.get("reasoning_effort") == EFFORT, "Invalid manifest model")
    date.fromisoformat(manifest["revision_date"])
    catalog = read_json(root / "data/catalog.json")
    rows = catalog_rows(catalog)
    items = manifest["articles"]
    ids = [item["id"] for item in items]
    require(bool(ids) and len(ids) == len(set(ids)), "Empty/duplicate review scope")
    require(set(ids) == {row["id"] for row in rows.values() if row["read_chars"] >= manifest["min_body_chars"]}, "Catalog review scope changed; prepare a new run")
    assignments = manifest.get("assignments")
    require(isinstance(assignments, dict) and set(assignments) == {"writer", "reviewer"}, "Missing actor assignments")
    owners = {}
    for role in ("writer", "reviewer"):
        groups = assignments[role]
        require(isinstance(groups, dict) and bool(groups), f"Missing {role} assignments")
        allocated = [article_id for assigned in groups.values() for article_id in assigned]
        require(len(allocated) == len(ids) and set(allocated) == set(ids), f"Missing/duplicate {role} assignments")
        owners[role] = {article_id: agent for agent, assigned in groups.items() for article_id in assigned}
        for agent, assigned in groups.items():
            require(agent.startswith(f"long_{role}_"), f"Invalid {role} agent identity")
            index = agent.rsplit("_", 1)[1]
            require(read_json(work / f"{role}-{index}-assignment.json") == assigned, f"{agent}: assignment file changed")
    ledger = load_ledger(root)
    for event in ledger["entries"]:
        validate_history_review(event)
    existing_events = {event.get("revision_id"): event for event in ledger["entries"] if event.get("revision_id")}
    pairs, pending = [], []
    for item in items:
        body = validate_body(work, item)
        current = rows[item["id"]]
        for key in ("source_sha256", "content_sha256", "read_chars", "title", "url", "archive_url", "source_path"):
            require(current[key] == item[key], f"{item['id']}: catalog source changed")
        require(provenance(current) == item["initial_provenance"], f"{item['id']}: initial provenance changed")
        writer_path = work / "writers" / (item["id"] + ".json")
        review_path = work / "reviewers" / (item["id"] + ".json")
        if not writer_path.exists():
            require(digest(current["summary"]) == item["old_summary_sha256"], f"{item['id']}: old summary changed")
            pending.append(item["id"])
            continue
        writer = read_json(writer_path)
        validate_writer(writer, item)
        require(writer["writer"] == owners["writer"][item["id"]], f"{item['id']}: unexpected writer")
        current_hash = digest(current["summary"])
        already_applied = existing_events.get(event_id(item, writer["summary"], manifest["run_id"]))
        require(current_hash == item["old_summary_sha256"] or (
            already_applied is not None and current_hash == digest(writer["summary"])
            and already_applied["after"]["summary"] == writer["summary"]
        ), f"{item['id']}: current summary changed since preparation")
        if not review_path.exists():
            pending.append(item["id"])
            continue
        review = read_json(review_path)
        validate_reviewer(review, writer, item)
        require(review["reviewer"] == owners["reviewer"][item["id"]], f"{item['id']}: unexpected reviewer")
        for point in [*writer["major_points"], *review["independent_major_points"]]:
            for span in point["source_spans"]:
                require(sum(char.isalnum() for char in body[span["start"]:span["end"]]) >= 8, f"{item['id']}: source span has no substantive text")
        pairs.append((item, writer, review))
    for directory, expected_ids in (("writers", set(ids)), ("reviewers", set(ids))):
        result_ids = {p.stem for p in (work / directory).glob("*.json") if not p.stem.endswith("-outline")}
        require(result_ids <= expected_ids, f"Unexpected result files in {directory}: {sorted(result_ids - expected_ids)}")
    require(allow_incomplete or not pending, f"Missing writer/reviewer results for {len(pending)} articles: {', '.join(pending)}")
    return manifest, pairs, pending


def revision_tag(event):
    after = event["after"]
    tag = {"date": after.get("revision_date", ""), "reviewer": after.get("reviewer", ""), "review_file": "data/summary-revisions.json"}
    for key in ("writer", "model", "reasoning_effort"):
        if key in after:
            tag[key] = after[key]
    if "revision_id" in event:
        tag["revision_id"] = event["revision_id"]
    return tag


def validate_history_review(event):
    # The two pre-workflow Codex revisions have no revision_id or independent review.
    if "revision_id" not in event:
        require(event.get("after", {}).get("reviewer") == "Codex", f"{event['id']}: unrecognized legacy revision")
        return
    review = event.get("review")
    require(isinstance(review, dict), f"{event['id']}: historical review is missing")
    source, after = event["source"], event["after"]
    count = source.get("chunk_count")
    require(type(count) is int and count > 0, f"{event['id']}: historical chunk count is missing")
    item = {"id": event["id"], "source_sha256": source["source_sha256"], "body_sha256": source["visible_text_sha256"],
            "read_chars": source["read_chars"], "chunks": [{}] * count}
    writer = {"writer": after["writer"], "summary": after["summary"], "major_points": event["coverage"]}
    try:
        validate_reviewer(review, writer, item)
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{event['id']}: invalid historical review: {error}") from error


def revision_metadata(ledger):
    dates = [event["after"]["revision_date"] for event in ledger["entries"]]
    return {"latest_date": max(dates) if dates else ledger["revision_date"], "articles": len({event["id"] for event in ledger["entries"]}),
            "events": len(ledger["entries"]), "review_file": "data/summary-revisions.json",
            "model_fields": "model 与 reasoning_effort 保留初始标注来源；修订来源见 summary_revision、逐篇审阅与修订历史。"}


def review_metadata(audit):
    return {"run_id": audit["run_id"], "latest_date": audit["revision_date"], **audit["statistics"],
            "model": MODEL, "reasoning_effort": EFFORT, "review_file": "data/summary-reviews.json"}


def replay_revisions(catalog, ledger):
    rows = catalog_rows(catalog)
    by_id = defaultdict(list)
    for event in ledger["entries"]:
        validate_history_review(event)
        by_id[event["id"]].append(event)
    for article_id, events in by_id.items():
        require(article_id in rows, f"Revision target absent from import: {article_id}")
        row = rows[article_id]
        previous_after = None
        known = set()
        for event in events:
            source = event["source"]
            require(source["source_sha256"] == row["source_sha256"] and source["content_sha256"] == row["content_sha256"], f"{article_id}: revision source conflicts with import")
            before, after = event["before"]["summary"], event["after"]["summary"]
            require(previous_after is None or before == previous_after, f"{article_id}: broken revision history")
            known.update((digest(before), digest(after)))
            previous_after = after
        require(digest(row["summary"]) in known, f"{article_id}: imported summary is outside accepted history; review required")
        # Every known version is an ancestor of the validated linear history.
        row["summary"] = events[-1]["after"]["summary"]
        row["summary_revision"] = revision_tag(events[-1])
    return catalog


def guarded_import(current, imported, ledger):
    original, incoming = catalog_rows(current), catalog_rows(imported)
    require(set(original) == set(incoming), "Imported catalog inventory differs; review inventory changes separately")
    for article_id in original:
        a, b = copy.deepcopy(original[article_id]), copy.deepcopy(incoming[article_id])
        for row in (a, b):
            row.pop("summary", None)
            row.pop("summary_revision", None)
        require(a == b, f"{article_id}: import changes fields outside summaries; review those changes separately")
    result = copy.deepcopy(current)
    for row in result["articles"]:
        row["summary"] = incoming[row["id"]]["summary"]
    # Non-revised summaries cannot be overwritten either.
    revised = {event["id"] for event in ledger["entries"]}
    for row in result["articles"]:
        require(row["id"] in revised or row["summary"] == original[row["id"]]["summary"], f"{row['id']}: import contains an unreviewed summary")
    return replay_revisions(result, ledger)


def staged_write(outputs):
    staged = []
    try:
        for path, text in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".summary-", delete=False) as stream:
                stream.write(text)
                staged.append((Path(stream.name), path))
        for temporary, target in staged:
            temporary.replace(target)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def sync_import(root, import_catalog):
    """Replay accepted public history without depending on private work files."""
    current = read_json(root / "data/catalog.json")
    ledger = load_ledger(root)
    require(bool(ledger["entries"]), "No accepted summary history is available")
    result = guarded_import(current, read_json(import_catalog), ledger)
    html = (root / "index.html").read_text(encoding="utf-8")
    match = DATA_PATTERN.search(html)
    require(match is not None, "Catalog JSON script not found")
    embedded = json.loads(match[2])
    require(embedded["records"] == current["articles"], "Embedded records differ from catalog")
    require(embedded["report"] == current["metadata"] == read_json(root / "data/validation.json"), "Metadata differs across outputs")
    result["metadata"]["summary_revisions"] = revision_metadata(ledger)
    audit_path = root / "data/summary-reviews.json"
    if audit_path.exists():
        result["metadata"]["summary_reviews"] = review_metadata(read_json(audit_path))
    embedded["records"], embedded["report"] = result["articles"], result["metadata"]
    payload = json.dumps(embedded, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    html = html[:match.start(2)] + payload + html[match.end(2):]
    outputs = {root / "data/catalog.json": json_text(result), root / "data/validation.json": json_text(result["metadata"]), root / "index.html": html}
    for text in outputs.values():
        require(not any(token in text for token in ("/Users/", "file://", "localhost")), "Non-public path in synchronized output")
    staged_write(outputs)
    return {"synchronized": len(result["articles"]), "replayed_articles": len({event["id"] for event in ledger["entries"]})}


def apply(root, work, import_catalog=None):
    manifest, pairs, _ = check(root, work)
    current = read_json(root / "data/catalog.json")
    html = (root / "index.html").read_text(encoding="utf-8")
    match = DATA_PATTERN.search(html)
    require(match is not None, "Catalog JSON script not found")
    embedded = json.loads(match[2])
    require(embedded["records"] == current["articles"], "Embedded records differ from catalog")
    require(embedded["report"] == current["metadata"] == read_json(root / "data/validation.json"), "Metadata differs across outputs")
    ledger = copy.deepcopy(load_ledger(root))
    catalog = guarded_import(current, read_json(import_catalog), ledger) if import_catalog else copy.deepcopy(current)
    rows = catalog_rows(catalog)
    existing = {event.get("revision_id") for event in ledger["entries"] if event.get("revision_id")}
    entries = []
    counts = Counter(writer["decision"] for _, writer, _ in pairs)
    for item, writer, review in pairs:
        entries.append({"id": item["id"], "title": item["title"], "url": item["url"],
                        "source": {key: item[key] for key in ("source_path", "source_sha256", "content_sha256", "body_sha256", "read_chars")},
                        "before_summary_sha256": item["old_summary_sha256"], "before_characters": len(item["summary"]),
                        "after_characters": len(writer["summary"]), "writer": writer, "review": review})
        if writer["decision"] == "retain":
            continue
        key = event_id(item, writer["summary"], manifest["run_id"])
        if key not in existing:
            ledger["entries"].append({"revision_id": key, "run_id": manifest["run_id"], "id": item["id"], "title": item["title"],
                "url": item["url"], "archive_url": item["archive_url"],
                "source": {"path": item["source_path"], "source_sha256": item["source_sha256"], "content_sha256": item["content_sha256"],
                           "visible_text_sha256": item["body_sha256"], "read_chars": item["read_chars"], "chunk_count": len(item["chunks"])},
                "before": {"summary": item["summary"], "characters": len(item["summary"]), "provenance": item["initial_provenance"]},
                "after": {"summary": writer["summary"], "characters": len(writer["summary"]), "paragraphs": len(writer["summary"].split("\n\n")),
                          "writer": writer["writer"], "reviewer": review["reviewer"], "model": MODEL, "reasoning_effort": EFFORT,
                          "revision_date": manifest["revision_date"]},
                "coverage": writer["major_points"], "previous_summary_issues": writer["issues"],
                "decision_reason": writer["decision_reason"], "length_reason": writer["length_reason"], "review": review})
            existing.add(key)
        rows[item["id"]]["summary"] = writer["summary"]
        event = next(event for event in ledger["entries"] if event.get("revision_id") == key)
        rows[item["id"]]["summary_revision"] = revision_tag(event)
    # Verify all historical edges, including the two revisions predating this run.
    replayed = replay_revisions(copy.deepcopy(catalog), ledger)
    require(replayed["articles"] == catalog["articles"], "Applied summaries do not match the latest accepted history")
    ledger["revision_date"] = manifest["revision_date"]
    ledger["reviewer"] = "Codex orchestration; gpt-6-luna max independent review"
    stats = {"reviewed": len(pairs), "rewritten": counts["rewrite"], "retained": counts["retain"],
             "body_characters": sum(item["read_chars"] for item, _, _ in pairs),
             "before_summary_characters": sum(len(item["summary"]) for item, _, _ in pairs),
             "after_summary_characters": sum(len(writer["summary"]) for _, writer, _ in pairs)}
    audit = {"schema_version": 1, "run_id": manifest["run_id"], "revision_date": manifest["revision_date"],
             "scope": {"min_body_chars": manifest["min_body_chars"], "article_ids": [item["id"] for item in manifest["articles"]]},
             "assignments": manifest["assignments"], "assignment_changes": manifest.get("assignment_changes", []),
             "model": MODEL, "reasoning_effort": EFFORT, "statistics": stats,
             "validation_note": "结构、哈希和片段检查不代替语义复核；独立语义结论由实际阅读全文的复核代理记录。",
             "entries": entries}
    prior_audit_path = root / "data/summary-reviews.json"
    audit["previous_runs"] = []
    if prior_audit_path.exists():
        prior_audit = read_json(prior_audit_path)
        audit["previous_runs"] = prior_audit.get("previous_runs", [])
        if prior_audit["run_id"] != manifest["run_id"]:
            audit["previous_runs"] = [*audit["previous_runs"], {key: value for key, value in prior_audit.items() if key != "previous_runs"}]
        else:
            require(prior_audit["entries"] == entries, "Accepted results for this run changed; create a new run for further revisions")
    metadata = catalog["metadata"]
    metadata["summary_reviews"] = review_metadata(audit)
    metadata["summary_revisions"] = revision_metadata(ledger)
    embedded["records"], embedded["report"] = catalog["articles"], metadata
    payload = json.dumps(embedded, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    html = html[:match.start(2)] + payload + html[match.end(2):]
    outputs = {root / "data/catalog.json": json_text(catalog), root / "data/validation.json": json_text(metadata),
               root / "data/summary-reviews.json": json_text(audit), root / "data/summary-revisions.json": json_text(ledger), root / "index.html": html}
    for path, text in outputs.items():
        if path.suffix == ".json":
            json.loads(text)
        require(not any(token in text for token in ("/Users/", "file://", "localhost")), f"Non-public path in output: {path.name}")
    staged_write(outputs)
    return {"run_id": manifest["run_id"], **stats}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--source-dir", type=Path, required=True)
    p.add_argument("--source-manifest", type=Path, required=True)
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--min-body-chars", type=int, default=20000)
    p.add_argument("--chunk-chars", type=int, default=6000)
    p = sub.add_parser("check")
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--allow-incomplete", action="store_true")
    p = sub.add_parser("apply")
    p.add_argument("--work-dir", type=Path)
    p.add_argument("--import-catalog", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.root, args.source_dir, args.source_manifest, args.work_dir, args.run_id, args.date, args.min_body_chars, args.chunk_chars)
        elif args.command == "check":
            manifest, pairs, pending = check(args.root, args.work_dir, args.allow_incomplete)
            result = {"run_id": manifest["run_id"], "passed": len(pairs), "pending": len(pending)}
        else:
            require(args.work_dir is not None or args.import_catalog is not None, "apply needs --work-dir or --import-catalog")
            result = apply(args.root, args.work_dir, args.import_catalog) if args.work_dir else sync_import(args.root, args.import_catalog)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"summary_workflow: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
