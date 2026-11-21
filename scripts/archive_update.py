#!/usr/bin/env python3
"""Pin and verify a complete tm-index update from a read-only Zhihu backup."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from archive_translation_intake import archived_objects, visible_body
from check_summary_publication import check as check_summary_publication
from site_catalog import read_site_catalog, verify_export_inventory
import summary_publications
from summary_workflow import (
    ROOT, canonical_hash, digest, file_hash, json_text, load_ledger,
    read_json, replay_revisions, require, staged_write,
)


STATE = Path("data/archive-update-state.json")
OUTPUT_FILES = (
    "data/catalog.json", "data/validation.json", "data/references.json", "data/answer-references.json",
    "data/authors.json", "data/original-authors.jsonl", "data/keywords.json",
    "data/summary-revisions.json", "index.html",
)
RECORD_PATH = re.compile(r"(article|answer)/([0-9]+)\.json")
SHA256 = re.compile(r"[0-9a-f]{64}")
SHANGHAI = timezone(timedelta(hours=8))


def output_hashes(root):
    """Fingerprint site data and the citation queue's published reports."""
    data = (root / "data").rglob("*")
    optional = [path for path in data if path.is_file() and path.suffix in {".json", ".jsonl", ".csv"}]
    optional.extend((root / "docs").glob("missing-zhihu-reference*.md"))
    paths = {*OUTPUT_FILES, *(path.relative_to(root).as_posix() for path in optional)}
    paths.discard(STATE.as_posix())
    return {name: file_hash(root / name) for name in sorted(paths)}


def git(archive, *args):
    command = ["git", "-C", str(archive), *args]
    result = subprocess.run(command, capture_output=True, check=False)
    require(result.returncode == 0, result.stderr.decode(errors="replace").strip()
            or f"Archive Git command failed: {args[0]}")
    return result.stdout


def commit(archive, revision):
    require(isinstance(revision, str) and revision, "Missing archive revision")
    return git(archive, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()


def display_fields(kind, record):
    author = record["author"]
    created = record["created" if kind == "article" else "created_time"]
    require(type(created) is int, "Invalid publication time in archive record")
    result = {
        "publisher": author["name"],
        "publisher_token": author["url_token"],
        "date": datetime.fromtimestamp(created, SHANGHAI).date().isoformat(),
        "voteup_count": record.get("voteup_count"),
    }
    if kind == "article":
        result["title"] = record["title"]
        column = record.get("column")
        result["column"] = (column or {}).get("title", "")
    else:
        result.update(title=record["question"]["title"], question_id=str(record["question"]["id"]),
                      created_time=created)
    return result


def version_info(kind, raw, record):
    content = record["content"]
    require(isinstance(content, str), f"{kind} {record['id']}: missing archive content")
    version = {
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(content),
        "display": display_fields(kind, record),
    }
    body = visible_body(content)
    version.update(text_sha256=digest(body), read_chars=len(body))
    return version


def changed_paths(archive, base, tip):
    rows = git(archive, "diff", "--name-status", "-z", "--no-renames", base, tip, "--", "article", "answer")
    fields = rows.decode().split("\0")
    require(fields[-1] == "" and len(fields) % 2 == 1, "Malformed archive Git diff")
    paths = []
    for operation, name in zip(fields[::2], fields[1::2]):
        match = RECORD_PATH.fullmatch(name)
        if match is None:
            continue
        require(operation in {"A", "M", "D"}, f"Unsupported source change for {name}: {operation}")
        paths.append((match[1], match[2], name, operation))
    return sorted(paths)


def make_plan(archive, base, tip):
    base, tip = commit(archive, base), commit(archive, tip)
    require(base != tip, "No new archive commit")
    git(archive, "merge-base", "--is-ancestor", base, tip)
    paths = changed_paths(archive, base, tip)
    versions = []
    for revision, excluded in ((base, "A"), (tip, "D")):
        selected = [p for p in paths if p[3] != excluded]
        objects = archived_objects(archive, revision, [p[2] for p in selected])
        versions.append({path: version_info(kind, raw, row)
                         for (kind, _, path, _), (raw, row) in zip(selected, objects, strict=True)})
    changes = []
    counts = {kind: Counter() for kind in ("article", "answer")}
    for kind, record_id, path, operation in paths:
        before, after = (objects.get(path) for objects in versions)
        if operation == "A":
            change = "added"
        elif operation == "D":
            change = "removed"
        else:
            change = "body" if before["content_sha256"] != after["content_sha256"] else "metadata"
        display_changes = [] if before is None or after is None else sorted(
            field for field, value in before["display"].items() if after["display"][field] != value
        )
        visible_text_changed = change == "body" and before["text_sha256"] != after["text_sha256"]
        manual = (change in {"added", "removed"} or visible_text_changed
                  or bool({"publisher", "publisher_token"} & set(display_changes)))
        changes.append({"kind": kind, "id": record_id, "path": path, "change": change,
                        "before": before, "after": after, "display_changes": display_changes,
                        "visible_text_changed": visible_text_changed, "manual_decision": manual})
        counts[kind][change] += 1
        if change == "metadata":
            counts[kind]["metadata_with_display_changes" if display_changes else "metadata_without_display_changes"] += 1
            if display_changes == ["voteup_count"]:
                counts[kind]["metadata_vote_count_only"] += 1
        if change == "body":
            counts[kind]["visible_text_changes" if visible_text_changed else "html_only_changes"] += 1
        if manual:
            counts[kind]["manual_decisions"] += 1
    return {"schema_version": 1, "archive_base": base, "archive_tip": tip,
            "counts": {kind: dict(count) for kind, count in counts.items()}, "changes": changes}


def unique_by(rows, key, label):
    index = {key(row): row for row in rows}
    require(len(index) == len(rows), f"Duplicate {label}")
    return index


def verify_site(root):
    from answer_reference_update import validate_index

    catalog, rows, embedded, _, _ = read_site_catalog(root)
    validation = catalog["metadata"]
    references = read_json(root / "data/references.json")
    answers = read_json(root / "data/answer-references.json")
    require(embedded["answerIndex"] == answers["answers"], "Embedded answer index differs from answer-references.json")
    export_order = verify_export_inventory(rows, embedded)
    require(export_order == sorted(export_order, key=lambda key: rows[key]["date"]),
            "Invalid export inventory or publication date order")
    require(validation["references"] == references["metadata"], "Reference graph metadata differs from catalog")
    require(validation["answer_references"] == answers["metadata"], "Answer metadata differs from catalog")
    require(validation["included_articles"] == len(rows), "Included article count differs from catalog")
    if "decision_counts" in validation:
        expected = Counter(row["decision"] for row in rows.values())
        require(all(count == validation["decision_counts"].get(decision) for decision, count in expected.items())
                and sum(validation["decision_counts"].values()) == validation["source_articles"]
                == validation["annotated_articles"], "Translation scope counts disagree")

    graph = unique_by(references["edges"], lambda edge: (edge["source_id"], edge["target_id"]), "translation edges")
    reverse = defaultdict(dict)
    outgoing = defaultdict(dict)
    for (source, target), edge in graph.items():
        require(source in rows and target in rows and source != target, "Invalid translation edge endpoints")
        reverse[target][source] = edge["via"]
        outgoing[source][target] = edge["via"]
    require(references["metadata"]["nodes"] == len(rows)
            and references["metadata"]["edges"] == len(graph), "Translation reference graph counts disagree")

    _, answer_graph = validate_index(answers, catalog)

    keywords = Counter()
    authors = defaultdict(set)
    pending = set()
    for article_id, row in rows.items():
        for field, count, expected in (
            ("references", "reference_count", outgoing[article_id]),
            ("referenced_by", "referenced_by_count", reverse[article_id]),
        ):
            linked = unique_by(row[field], lambda link: link["id"], f"{article_id} {field}")
            require(row[count] == len(expected) and linked.keys() == expected.keys()
                    and all(link["via"] == expected[key] and link["title"] == rows[key]["title"]
                            for key, link in linked.items()), f"{article_id}: {field} differs from translation graph")
        held = {link["id"] for link in row["references"] if link.get("pending_review") is True}
        require(row.get("missing_zhihu_reference_count", 0) == len(held),
                f"{article_id}: pending reference counter differs")
        pending.update((article_id, target) for target in held)
        require(isinstance(row["summary"], str) and row["summary"].strip() and row["keywords"],
                f"{article_id}: missing summary or keywords")
        keywords.update(set(row["keywords"]))
        for entity in row["author_entities"]:
            authors[entity["name"]].add(article_id)

    queue = root / "data/missing-zhihu-references.json"
    if queue.exists():
        report = read_json(queue)
        candidate_index = unique_by(report["edges"], lambda edge: (edge["source_id"], edge["target_id"]),
                                    "pending translation edges")
        require(candidate_index.keys() == pending
                and report["metadata"]["candidate_edges"] == len(pending)
                and report["metadata"]["source_articles"] == len({source for source, _ in pending}),
                "Pending translation index differs from catalog")
    else:
        require(not pending, "Missing pending translation index")
    by_keyword = unique_by(read_json(root / "data/keywords.json"), lambda e: e["keyword"], "keywords")
    require({keyword: entry["count"] for keyword, entry in by_keyword.items()} == keywords,
            "Keyword index differs from catalog")
    by_author = unique_by(read_json(root / "data/authors.json"), lambda e: e["name"], "authors")
    require(by_author.keys() == authors.keys()
            and all(len(entry["article_ids"]) == entry["article_count"] == len(authors[name])
                    and set(entry["article_ids"]) == authors[name] for name, entry in by_author.items()),
            "Author index differs from catalog")
    author_lines = (root / "data/original-authors.jsonl").read_text(encoding="utf-8").splitlines()
    author_records = unique_by([json.loads(line) for line in author_lines], lambda e: e["id"], "author records")
    require(author_records.keys() == rows.keys()
            and all(all(rows[key].get(field) == value for field, value in entry.items())
                    and "author_entities" in entry and "original_author_status" in entry
                    for key, entry in author_records.items()), "Per-article authorship differs from catalog")
    projected = {"articles": copy.deepcopy(catalog["articles"])}
    require(replay_revisions(projected, check_summary_publication(root))["articles"] == catalog["articles"],
            "Catalog does not contain the latest accepted summary revision")
    return rows, graph, answers, answer_graph


def accepted_versions(root):
    return summary_publications.accepted_versions(check_summary_publication(root))


def validate_decisions(plan, decisions):
    require(isinstance(decisions, dict) and isinstance(decisions.get("items"), list), "Invalid archive update decisions")
    require(decisions.get("schema_version") == 1 and decisions.get("plan_sha256") == canonical_hash(plan),
            "Decisions are not bound to this archive update plan")
    items = unique_by(decisions["items"], lambda item: (item["kind"], item["id"]), "archive update decisions")
    changed = {(v["kind"], v["id"]): v for v in plan["changes"]}
    require(items.keys() <= changed.keys(), "Unexpected archive update decision")
    missing = {key for key, change in changed.items() if change["manual_decision"]} - items.keys()
    require(not missing, f"Missing {len(missing)} archive update decisions, including {sorted(missing)[:3]}")
    for key, item in items.items():
        require(isinstance(item.get("reason"), str) and item["reason"].strip(),
                f"{key}: archive update decision has no reason")
        if "newly_indexed" in item:
            require(type(item["newly_indexed"]) is bool and key[0] == "article" and item["status"] == "included",
                    f"{key}: invalid newly indexed marker")
    return items


def verify_exclusions(archive, plan, decisions):
    excluded = [change for change in plan["changes"] if change["kind"] == "article"
                and decisions.get(("article", change["id"]), {}).get("status") == "excluded"]
    paths = [entry["path"] for entry in excluded]
    for change, (_, record) in zip(excluded, archived_objects(archive, plan["archive_tip"], paths), strict=True):
        item = decisions["article", change["id"]]
        evidence = item.get("source_evidence")
        require(isinstance(evidence, str) and sum(c.isalnum() for c in evidence) >= 8
                and (evidence in record["title"] or evidence in visible_body(record["content"])),
                f"{change['id']}: exclusion needs specific evidence from the pinned article")


def validate_article(change, decision, row, approved_versions):
    article_id = change["id"]
    after = change["after"]
    if decision is not None:
        status = decision.get("status")
        allowed = {"removed"} if after is None else {"included", "excluded"}
        require(status in allowed, f"{article_id}: invalid article disposition")
        require((row is not None) == (status == "included"), f"{article_id}: article disposition conflicts with catalog")
    if row is None:
        return
    require(after is not None, f"{article_id}: deleted archive article remains in catalog")
    for field, value in after["display"].items():
        if field in change["display_changes"] or change["change"] != "metadata":
            require(row[field] == value, f"{article_id}: stale article {field}")
    if change["change"] == "metadata":
        return
    require(row["source_path"] == change["path"] and row["source_sha256"] == after["source_sha256"]
            and row["content_sha256"] == after["content_sha256"] and row["read_chars"] == after["read_chars"],
            f"{article_id}: catalog article has stale source or length")
    before = change["before"]
    if before is None or change["visible_text_changed"]:
        require((article_id, after["source_sha256"], after["content_sha256"],
                 digest(row["summary"])) in approved_versions,
                f"{article_id}: latest source and summary lack an accepted independent review")


def validate_answer(change, decision, row, translation_publishers):
    answer_id = change["id"]
    after = change["after"]
    if decision is not None:
        status = decision.get("status")
        if after is None:
            require(status == "removed", f"{answer_id}: invalid removed answer disposition")
        elif translation_publishers.get(after["display"]["publisher"]) != after["display"]["publisher_token"]:
            require(status == "out_of_scope", f"{answer_id}: author is outside answer scope")
        else:
            require(status in {"indexed", "no_references"}, f"{answer_id}: invalid answer disposition")
        require((row is not None) == (status == "indexed"), f"{answer_id}: answer disposition conflicts with index")
    if row is None:
        return
    require(after is not None, f"{answer_id}: deleted archive answer remains indexed")
    for field, value in after["display"].items():
        if field == "publisher_token":
            continue
        if field in change["display_changes"] or change["change"] != "metadata":
            actual = row["author" if field == "publisher" else field]
            require(actual == value, f"{answer_id}: stale indexed answer {field}")


def verify_answer_scan(root, archive, plan, answers, new_articles, answer_plan, reviews):
    if not new_articles and not any(change["kind"] == "answer" for change in plan["changes"]):
        return
    from answer_reference_update import check as check_answers

    require(answer_plan is not None and reviews is not None, "Pass the pinned answer plan and its reviews")
    scan = answers["metadata"].get("incremental_archive_scan", {})
    require(scan.get("archive_base") == plan["archive_base"] and scan.get("archive_tip") == plan["archive_tip"]
            and scan.get("changes") == "all", "Answer backlinks lack a complete scan of the pinned archive update")
    require(set(scan.get("new_article_ids", [])) == new_articles,
            "Answer scan did not cover old answers linking to this batch of newly indexed translations")
    require(all(isinstance(scan.get(key), str) and SHA256.fullmatch(scan[key])
                for key in ("plan_sha256", "reviews_sha256")), "Missing pinned answer scan review evidence")
    require(scan.get("plan_sha256") == canonical_hash(answer_plan)
            and scan.get("reviews_sha256") == canonical_hash(reviews)
            and scan == check_answers(root, archive, answer_plan, reviews),
            "Reviewed answer update differs from the applied full scan")


def verify_article_scan(root, archive, plan, new_articles, article_plan, reviews):
    if not new_articles and not any(change["kind"] == "article" for change in plan["changes"]):
        return
    from archive_article_reference_audit import check as check_article_references

    require(article_plan is not None and reviews is not None, "Pass the pinned translation reference plan and its reviews")
    scan = read_json(root / "data/references.json")["metadata"].get("incremental_article_reference_scan", {})
    require(scan.get("archive_base") == plan["archive_base"] and scan.get("archive_tip") == plan["archive_tip"]
            and scan.get("plan_sha256") == canonical_hash(article_plan)
            and scan.get("reviews_sha256") == canonical_hash(reviews),
            "Translation references lack an applied scan of the pinned archive update")
    require(set(scan.get("new_article_ids", [])) == new_articles
            and set(article_plan.get("new_target_ids", [])) == new_articles,
            "Translation reference scan did not cover backlinks to all new translations")
    require(scan == check_article_references(root, archive, article_plan, reviews),
            "Reviewed translation reference update differs from the applied scan")


def check(root, archive, plan, decisions, *, answer_plan=None, answer_reviews=None,
          article_reference_plan=None, article_reference_reviews=None):
    require(plan.get("schema_version") == 1, "Unsupported archive update plan schema")
    require(plan == make_plan(archive, plan["archive_base"], plan["archive_tip"]),
            "Archive update plan differs from the pinned Git changes")
    items = validate_decisions(plan, decisions)
    verify_exclusions(archive, plan, items)
    rows, _, answer_index, _ = verify_site(root)
    versions = accepted_versions(root)
    retired = load_ledger(root).get("retired_articles", {})
    translation_publishers = {r["publisher"]: r["publisher_token"] for r in rows.values()}
    new_articles = set()
    for change in plan["changes"]:
        kind, record_id = change["kind"], change["id"]
        decision = items.get((kind, record_id))
        if kind == "article":
            if record_id in rows and (change["change"] == "added" or (decision or {}).get("newly_indexed") is True):
                new_articles.add(record_id)
            validate_article(change, decision, rows.get(record_id), versions)
            if decision is not None and decision["status"] in {"removed", "excluded"} and record_id in retired:
                receipt = retired[record_id]
                require(receipt["archive_base"] == plan["archive_base"] and receipt["archive_tip"] == plan["archive_tip"],
                        f"{record_id}: summary retirement differs from the pinned archive update")
        else:
            validate_answer(change, decision, answer_index["answers"].get(record_id), translation_publishers)
    if new_articles or any((change["kind"] == "answer" or change["id"] in rows)
                            and (change["change"] in {"added", "removed"} or change["visible_text_changed"])
                            for change in plan["changes"]):
        unlinked = decisions.get("unlinked_answer_review", {})
        require(isinstance(unlinked, dict)
                and all(isinstance(unlinked.get(key), str) and unlinked[key].strip()
                        for key in ("scope", "method", "result", "limitations")),
                "Record the review scope, method, result and limitations for unlinked relationships")
    verify_answer_scan(root, archive, plan, answer_index, new_articles, answer_plan, answer_reviews)
    verify_article_scan(root, archive, plan, new_articles, article_reference_plan, article_reference_reviews)
    return {"archive_base": plan["archive_base"], "archive_tip": plan["archive_tip"], "counts": plan["counts"],
            "plan_sha256": canonical_hash(plan), "decisions_sha256": canonical_hash(decisions),
            "answer_plan_sha256": canonical_hash(answer_plan) if answer_plan is not None else None,
            "answer_reviews_sha256": canonical_hash(answer_reviews) if answer_reviews is not None else None,
            "article_reference_plan_sha256": canonical_hash(article_reference_plan)
            if article_reference_plan is not None else None,
            "article_reference_reviews_sha256": canonical_hash(article_reference_reviews)
            if article_reference_reviews is not None else None}


def baseline(root, archive, explicit):
    path = root / STATE
    if path.exists():
        state = read_json(path)
        require(state["schema_version"] == 1, "Unsupported archive checkpoint schema")
        pin = commit(archive, state["archive_commit"])
        require(explicit is None or commit(archive, explicit) == pin, "Base differs from last complete archive update")
        return pin
    require(explicit is not None, "Pass --base to bootstrap the first complete archive update")
    return commit(archive, explicit)


def complete(root, archive, plan, decisions, **phases):
    state_path = root / STATE
    require(not state_path.resolve().is_relative_to(Path(archive).resolve()), "The archive checkout is read-only")
    lock = root / ".work/archive-update.lock"
    require(not lock.resolve().is_relative_to(Path(archive).resolve()), "The archive checkout is read-only")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = read_json(state_path) if state_path.exists() else None
        repeat = state is not None and state["archive_commit"] == plan["archive_tip"]
        if not repeat:
            baseline(root, archive, plan["archive_base"])
        before = output_hashes(root)
        result = check(root, archive, plan, decisions, **phases)
        require(before == output_hashes(root),
                "Project outputs changed during archive update verification")
        result["outputs_sha256"] = before
        prior = state["completed_runs"] if state is not None else []
        if repeat:
            require(prior and prior[-1] == result, "Archive tip already completed with different inputs or outputs")
        else:
            state = {"schema_version": 1, "archive_commit": result["archive_tip"], "completed_runs": [*prior, result]}
            staged_write({state_path: json_text(state)})
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "check", "complete"):
        command = commands.add_parser(name)
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--base")
            command.add_argument("--tip", required=True)
        else:
            command.add_argument("--decisions", type=Path, required=True)
            command.add_argument("--answer-plan", type=Path)
            command.add_argument("--answer-reviews", type=Path)
            command.add_argument("--article-reference-plan", type=Path)
            command.add_argument("--article-reference-reviews", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            base = baseline(args.root, args.archive, args.base)
            plan = make_plan(args.archive, base, args.tip)
            require(not args.plan.resolve().is_relative_to(args.archive.resolve()), "The archive checkout is read-only")
            require(not args.plan.exists(), "Plan path already exists; use a fresh path")
            args.plan.parent.mkdir(parents=True, exist_ok=True)
            args.plan.write_text(json_text(plan), encoding="utf-8")
            result = {key: plan[key] for key in ("archive_base", "archive_tip", "counts")}
            result["plan_sha256"] = canonical_hash(plan)
        else:
            plan = read_json(args.plan)
            phases = {name: read_json(value) for name in
                      ("answer_plan", "answer_reviews", "article_reference_plan", "article_reference_reviews")
                      if (value := getattr(args, name)) is not None}
            if args.command == "complete":
                result = complete(args.root, args.archive, plan, read_json(args.decisions), **phases)
            else:
                baseline(args.root, args.archive, plan["archive_base"])
                result = check(args.root, args.archive, plan, read_json(args.decisions), **phases)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
