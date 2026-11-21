"""Apply evidence-backed answer backlinks from a pinned Zhihu archive delta."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import fcntl
import json
from pathlib import Path
import re
import subprocess
import unicodedata

from archive_translation_intake import archived_objects, visible_body
from answer_archive_delta import answer_snapshot, direct_pairs, make_plan, verify_plan
from site_catalog import catalog_files, read_site_catalog, verify_export_inventory
from summary_sources import VisibleStrings
from summary_workflow import (
    canonical_hash, catalog_rows, digest, file_hash, json_text,
    read_json, require, staged_write, write_json,
)


ROOT = Path(__file__).resolve().parents[1]
FILES = ("data/answer-references.json", "data/catalog.json", "data/validation.json", "index.html")


def pair(row):
    return row["answer_id"], row["article_id"]


def by_pair(entries):
    result = {}
    require(isinstance(entries, list), "Pair records must be an array")
    for row in entries:
        require(isinstance(row, dict) and all(
            isinstance(row.get(key), str) and re.fullmatch(r"[1-9]\d*", row[key])
            for key in ("answer_id", "article_id")
        ), "Invalid answer and article pair")
        key = pair(row)
        require(key not in result, f"Duplicate answer and article pair: {key}")
        result[key] = row
    return result


def edge_order(edge):
    return int(edge["article_id"]), int(edge["answer_id"])


def level_counts(edges):
    values = Counter(str(edge["level"]) if edge["level"] is not None else "unclassified" for edge in edges)
    return {key: values[key] for key in ("1", "2", "3", "unclassified")}


def answer_ids_for_article(levels, answers):
    return sorted(levels, key=lambda answer_id: (answers[answer_id]["created_time"], int(answer_id)), reverse=True)


def relation_fields(edges, answers, rows):
    levels = defaultdict(dict)
    used_answers = set()
    for (answer_id, article_id), edge in edges.items():
        require(answer_id in answers and article_id in rows, f"Unknown answer or article: {(answer_id, article_id)}")
        level = edge.get("level")
        require(level is None or type(level) is int and 1 <= level <= 3,
                f"Invalid citation level: {(answer_id, article_id)}")
        levels[article_id][answer_id] = {
            "level": level,
            "evidence": (edge.get("classification") or {}).get("evidence"),
            "direct": bool({"zhihu", "archive"}.intersection(edge["via"])),
        }
        used_answers.add(answer_id)
    require(used_answers == answers.keys(), "Answer index has dangling or missing records")
    for article_id in rows:
        values = levels.get(article_id, {})
        ids = answer_ids_for_article(values, answers)
        yield article_id, ids, {answer_id: values[answer_id] for answer_id in ids}


def validate_index(index, catalog):
    """Validate all graph relations, card counters and metadata."""
    rows = catalog_rows(catalog)
    answers = index["answers"]
    edges = by_pair(index["edges"])
    require(isinstance(answers, dict) and all(
        row.get("id") == answer_id and type(row.get("created_time")) is int
        for answer_id, row in answers.items()
    ), "Answer index has an invalid record")
    require(index["edges"] == sorted(edges.values(), key=edge_order), "Answer edges are out of order")
    for article_id, ids, levels in relation_fields(edges, answers, rows):
        row = rows[article_id]
        require(row.get("cited_by_answer_ids") == ids and row.get("cited_by_answer_count") == len(ids)
                and row.get("answer_reference_levels") == levels, f"Catalog answer backlinks differ: {article_id}")
    expected = {
        "answers_with_citations": len(answers),
        "translations_cited": len({article_id for _, article_id in edges}),
        "citation_pairs": len(edges),
        "classification_counts": level_counts(edges.values()),
    }
    metadata = index["metadata"]
    require(all(metadata.get(key) == value for key, value in expected.items()), "Answer index counts differ")
    require(catalog["metadata"].get("answer_references") == metadata, "Catalog answer metadata differs")
    return rows, edges


def read_outputs(root):
    index = read_json(root / FILES[0])
    catalog, rows, embedded, html, match = read_site_catalog(root)
    require(embedded.get("answerIndex") == index["answers"], "Embedded answer index differs from answer-references.json")
    _, edges = validate_index(index, catalog)
    verify_export_inventory(rows, embedded)
    return index, catalog, html, match, embedded, rows, edges


def prepare(root, archive, base, tip, *, changes="all", new_article_ids=()):
    """Return the mechanical scan without changing any public data."""
    index, _, _, _, _, rows, _ = read_outputs(Path(root))
    return make_plan(Path(archive), base, tip, rows, index["edges"],
                     changes=changes, new_article_ids=new_article_ids)


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def evidence_text(value, label, *, minimum=1):
    require(isinstance(value, str) and value == value.strip()
            and sum(char.isalnum() for char in value) >= minimum, f"Missing or invalid {label}")
    return value


def contains(quote, texts):
    return any(normalized(quote) in normalized(text) for text in texts)


class AnswerProse(VisibleStrings):
    """Read visible answer prose without generated citation displays."""

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        native_reference = tag == "sup" and attrs.get("data-draft-type") == "reference"
        link_card = tag == "a" and (attrs.get("data-draft-type") == "link-card"
                                    or "LinkCard" in (attrs.get("class") or "").split())
        if native_reference or link_card or (self.ignored and self.ignored[-1] == tag):
            self.ignored.append(tag)
        else:
            super().handle_starttag(tag, attributes)


def answer_prose(html):
    parser = AnswerProse()
    parser.feed(html)
    parser.close()
    return "\n".join(parser.parts)


def source_documents(archive, plan, reviews, rows):
    answer_ids = sorted({answer_id for answer_id, _ in reviews}, key=int)
    article_ids = sorted({article_id for _, article_id in reviews}, key=int)
    answer_paths = [plan["answers"][answer_id]["path"] for answer_id in answer_ids]
    article_paths = [rows[article_id]["source_path"] for article_id in article_ids]
    require(answer_paths == [f"answer/{answer_id}.json" for answer_id in answer_ids]
            and article_paths == [f"article/{article_id}.json" for article_id in article_ids],
            "Reviewed source paths do not match their answer and article IDs")
    raw = archived_objects(archive, plan["archive_tip"], [*answer_paths, *article_paths])
    documents = {}
    for path, (_, record) in zip([*answer_paths, *article_paths], raw, strict=True):
        html = record["content"]
        text = visible_body(html)
        prose = answer_prose(html) if path.startswith("answer/") else text
        documents[path] = (digest(html), text, prose)
    return documents


def review_fingerprints(key, review, source, row, article_hash):
    fingerprints = {
        "source_sha256": source["source_sha256"],
        "content_sha256": source["content_sha256"],
        "article_source_sha256": row["source_sha256"],
        "article_content_sha256": article_hash,
    }
    require(all(review.get(field) == value for field, value in fingerprints.items()),
            f"Stale answer or article evidence: {key}")
    return fingerprints


def validate_classification(key, review, answer_sources, article_sources, *, inferred=False):
    level = review.get("level")
    require("level" in review and (level is None or type(level) is int and 1 <= level <= 3),
            f"Invalid citation level: {key}")
    require(not inferred or level is not None, f"An unlinked pair requires substantive reviewed evidence: {key}")
    classification = review.get("classification")
    require(isinstance(classification, dict) and classification.get("review_status") == (
        "unclassified" if level is None else "reviewed"), f"Missing classification status: {key}")
    for field in ("reviewer_model", "reasoning_effort", "reviewer", "evidence"):
        evidence_text(classification.get(field), f"review {field}: {key}",
                      minimum=8 if inferred and field == "evidence" else 1)
    answer_quote = classification.get("answer_evidence")
    article_quote = classification.get("article_evidence")
    require(level is None or answer_quote is not None, f"Missing answer evidence: {key}")
    require(article_quote is not None or (not inferred and level not in (2, 3)), f"Missing article evidence: {key}")
    minimum = 8 if inferred or level in (2, 3) else 1
    if answer_quote is not None:
        evidence_text(answer_quote, f"answer evidence: {key}", minimum=minimum)
        require(contains(answer_quote, answer_sources), f"Answer evidence does not match the required source: {key}")
    if article_quote is not None:
        evidence_text(article_quote, f"article evidence: {key}", minimum=minimum)
        require(contains(article_quote, article_sources), f"Article evidence does not match source: {key}")
    if level == 3:
        require(normalized(answer_quote) == normalized(article_quote), f"Level 3 excerpts do not match: {key}")
    return level, classification


def reviewed_edge(key, review, candidate, plan, rows, documents, old):
    answer_id, article_id = key
    answer = plan["answers"][answer_id]
    source = answer["after"]
    row = rows[article_id]
    article_hash, article_text, _ = documents[row["source_path"]]
    answer_hash, answer_text, prose = documents[answer["path"]]
    require(answer_hash == source["content_sha256"], f"Answer source differs from plan: {answer_id}")
    fingerprints = review_fingerprints(key, review, source, row, article_hash)
    answer_sources = [prose]
    article_sources = [article_text]
    if review.get("level") in (None, 1):
        answer_sources.append(answer_text)
        answer_sources.extend(entry[field] for entry in candidate["occurrences"] for field in ("label", "context")
                              if isinstance(entry.get(field), str))
        article_sources.append(row["title"])
    level, classification = validate_classification(key, review, answer_sources, article_sources)
    result = {
        "answer_id": answer_id, "article_id": article_id, "via": candidate["via"],
        "evidence": candidate["evidence"], "level": level, "classification": classification,
        "archive_evidence": {
            "archive_tip": plan["archive_tip"], **fingerprints, "occurrences": len(candidate["occurrences"]),
        },
    }
    if old is not None:
        result["previous_evidence"] = old
    return result


def inferred_edges(archive, plan, reviews, rows, existing):
    """Read and verify separately reviewed, unlinked pairs from the pinned answer and article blobs."""
    if not reviews:
        return {}, {}
    require(not (reviews.keys() & existing.keys()), "An inferred review would overwrite an existing pair")
    answer_targets = defaultdict(set)
    new_targets = set(plan["new_article_ids"])
    for key in reviews:
        answer_id, article_id = key
        record = plan["answers"].get(answer_id)
        changed = record is not None and record["change"] in {"added", "body_changed", "metadata_only"}
        require(article_id in rows and (changed or article_id in new_targets),
                f"Inferred pair is outside the scan scope: {key}")
        require(record is None or record["after"] is not None, f"Inferred answer is absent at archive tip: {answer_id}")
        answer_targets[answer_id].add(article_id)

    answer_sources, answer_texts = {}, {}
    answer_paths = [f"answer/{answer_id}.json" for answer_id in sorted(answer_targets, key=int)]
    for raw, answer in archived_objects(archive, plan["archive_tip"], answer_paths):
        answer_id = str(answer["id"])
        snapshot = answer_snapshot(raw, answer)
        record = plan["answers"].get(answer_id)
        require(record is None or record["after"] == snapshot, f"Inferred answer differs from the plan: {answer_id}")
        require(not direct_pairs(answer_id, answer["content"], answer_targets[answer_id]),
                f"An inferred review has a direct link in the answer: {answer_id}")
        answer_sources[answer_id] = snapshot
        answer_texts[answer_id] = answer_prose(answer["content"])

    article_sources = {}
    article_ids = sorted({article_id for _, article_id in reviews}, key=int)
    paths = [rows[article_id]["source_path"] for article_id in article_ids]
    require(paths == [f"article/{article_id}.json" for article_id in article_ids],
            "Reviewed source paths do not match the inferred article IDs")
    for _, article in archived_objects(archive, plan["archive_tip"], paths):
        article_sources[str(article["id"])] = digest(article["content"]), visible_body(article["content"])

    title_counts = Counter(normalized(row["title"]) for row in rows.values())
    edges = {}
    for key, review in reviews.items():
        answer_id, article_id = key
        row = rows[article_id]
        source = answer_sources[answer_id]
        article_hash, article_text = article_sources[article_id]
        fingerprints = review_fingerprints(key, review, source, row, article_hash)
        article_evidence = [row["title"]] if review.get("level") == 1 else [article_text]
        level, classification = validate_classification(
            key, review, [answer_texts[answer_id]], article_evidence, inferred=True,
        )
        answer_quote = classification["answer_evidence"]
        article_quote = classification["article_evidence"]
        if level == 1:
            title = normalized(row["title"])
            require(title_counts[title] == 1 and normalized(answer_quote) == normalized(article_quote) == title,
                    f"An unlinked level 1 requires the full distinctive title: {key}")
        if level == 2:
            attribution = evidence_text(classification.get("contextual_attribution"),
                                        f"contextual attribution: {key}", minimum=8)
            require(normalized(attribution) not in {normalized(answer_quote), normalized(article_quote)},
                    f"Contextual attribution must explain the relationship: {key}")
        edges[key] = {
            "answer_id": answer_id, "article_id": article_id, "via": ["inferred"],
            "evidence": ["reviewed_text"], "level": level, "classification": classification,
            "archive_evidence": {
                "archive_tip": plan["archive_tip"], **fingerprints, "occurrences": 0,
                "answer_evidence_chars": len(answer_quote), "article_evidence_chars": len(article_quote),
            },
        }
    return edges, {answer_id: snapshot["answer"] for answer_id, snapshot in answer_sources.items()}


def expected_dispositions(plan, candidates):
    return {(answer_id, article_id)
            for answer_id, record in plan["answers"].items()
            for key in ("removed_direct_pairs", "recheck_inferred_pairs")
            for article_id in record[key]} - candidates.keys()


def review_pairs(bundle, plan):
    require(isinstance(bundle, dict) and bundle.get("schema_version") == 1
            and bundle.get("plan_sha256") == canonical_hash(plan), "Review bundle does not match the frozen plan")
    candidates = by_pair(plan["candidates"])
    reviews = by_pair(bundle.get("reviews"))
    require(reviews.keys() == candidates.keys(), "Reviews must cover exactly the candidate pairs")
    dispositions = by_pair(bundle.get("pair_decisions", []))
    require(dispositions.keys() == expected_dispositions(plan, candidates),
            "Dispositions must cover exactly the removed or stale pairs")
    for key, row in dispositions.items():
        require(row.get("decision") in {"retain", "remove"}, f"Unresolved answer pair disposition: {key}")
        evidence_text(row.get("reason"), f"answer pair disposition: {key}")
    inferred = by_pair(bundle.get("inferred_reviews", []))
    require(not (inferred.keys() & (candidates.keys() | dispositions.keys())),
            "An inferred review duplicates a direct candidate or pair disposition")
    return candidates, reviews, dispositions, inferred


def relevant_answers(answers, edges, plan, inferred_answers):
    affected = {answer_id for answer_id, _ in edges}
    result = {answer_id: answer for answer_id, answer in answers.items() if answer_id in affected}
    for answer_id, record in plan["answers"].items():
        if answer_id in affected and record["after"] is not None:
            result[answer_id] = record["after"]["answer"]
    result.update(inferred_answers)
    return result


def update_metadata(metadata, answers, edges, plan, receipt):
    population = plan.get("population", plan["counts"])
    for key in ("answers_scanned", "skipped_other_authors", "earliest_answer_date", "latest_answer_date"):
        metadata[key] = population[key]
    if "incremental_archive_scan" in metadata:
        metadata.setdefault("incremental_archive_history", []).append(metadata["incremental_archive_scan"])
    metadata.update({
        "answers_with_citations": len(answers),
        "translations_cited": len({article_id for _, article_id in edges}),
        "citation_pairs": len(edges),
        "classification_counts": level_counts(edges.values()),
        "incremental_archive_scan": receipt,
    })
    if plan["changes"] == "all":
        metadata["last_full_archive_tip"] = plan["archive_tip"]


def make_receipt(plan, bundle, new_edges, candidates, dispositions, old_edges, inferred):
    added_ids = sorted((answer_id for answer_id, row in plan["answers"].items() if row["change"] == "added"), key=int)
    citing_ids = {answer_id for answer_id, _ in new_edges}
    prior_citing_ids = {answer_id for answer_id, _ in old_edges}
    affected = candidates.keys() | inferred.keys()
    receipt = {
        "archive_base": plan["archive_base"], "archive_tip": plan["archive_tip"], "changes": plan["changes"],
        "new_article_ids": plan["new_article_ids"], "plan_sha256": canonical_hash(plan),
        "reviews_sha256": canonical_hash(bundle), "counts": plan["counts"],
        "added_answer_ids": added_ids,
        "added_without_citations": [answer_id for answer_id in added_ids if answer_id not in citing_ids],
        "indexed_answer_metadata_changes": sum(
            row["before"]["answer"] != row["after"]["answer"]
            for answer_id, row in plan["answers"].items()
            if answer_id in prior_citing_ids and row["before"] is not None and row["after"] is not None
        ),
        "citing_answers_added": len(citing_ids - prior_citing_ids),
        "pairs_added": sum(key not in old_edges for key in affected),
        "pairs_reclassified": sum(key in old_edges for key in candidates),
        "pairs_removed": sum(row["decision"] == "remove" for row in dispositions.values()),
        "affected_pairs_sha256": canonical_hash([new_edges[key] for key in sorted(affected)]),
        "pair_dispositions": [
            {**row, "previous_edge": old_edges[key]} for key, row in sorted(dispositions.items())
        ],
        "scope": "Direct archive links and reviewed changes to existing pairs in the selected archive scope.",
    }
    if inferred:
        receipt.update(
            inferred_pairs_added=len(inferred),
            inferred_classification_counts=level_counts(inferred.values()),
            scope="Direct archive links, reviewed changes to existing pairs and accepted unlinked relationships.",
        )
    return receipt


def already_applied(receipt, plan, bundle, answers, edges, candidates, reviews, dispositions, inferred, archive, rows):
    if receipt.get("plan_sha256") != canonical_hash(plan):
        return False
    require(receipt.get("reviews_sha256") == canonical_hash(bundle), "A different review bundle was already applied")
    affected = candidates.keys() | inferred.keys()
    require(affected <= edges.keys(), "Previously applied candidate edges are missing")
    require(receipt["affected_pairs_sha256"] == canonical_hash([edges[key] for key in sorted(affected)]),
            "Previously applied candidate edges have changed")
    previous = dict(edges)
    for key in affected:
        current = previous.pop(key)
        if "previous_evidence" in current:
            require(key in candidates, f"An inferred update overwrote existing evidence: {key}")
            previous[key] = current["previous_evidence"]
    saved = by_pair(receipt["pair_dispositions"])
    require(saved.keys() == dispositions.keys(), "Previously applied pair dispositions differ")
    for key, row in saved.items():
        if row["decision"] == "remove":
            require(key not in previous, f"Deleted pair reappeared: {key}")
            previous[key] = row["previous_edge"]
        else:
            require(previous.get(key) == row["previous_edge"], f"Retained pair changed: {key}")
    verify_plan(plan, archive, rows, previous.values())
    unlinked, inferred_answers = inferred_edges(archive, plan, inferred, rows, previous)
    require(relevant_answers(answers, edges, plan, inferred_answers) == answers,
            "Previously applied answer metadata differ")
    documents = source_documents(archive, plan, reviews, rows)
    for key, candidate in candidates.items():
        expected = reviewed_edge(key, reviews[key], candidate, plan, rows, documents, previous.get(key))
        require(edges[key] == expected, f"Previously applied evidence differs: {key}")
    require(all(edges[key] == edge for key, edge in unlinked.items()), "Previously applied inferred evidence differs")
    require(receipt == make_receipt(plan, bundle, edges, candidates, dispositions, previous, unlinked),
            "Previously applied receipt differs")
    return True


def prepare_updates(root, archive, plan, bundle):
    index, catalog, html, match, embedded, rows, existing = read_outputs(root)
    candidates, reviews, dispositions, inferred = review_pairs(bundle, plan)
    metadata = index["metadata"]
    latest = metadata.get("incremental_archive_scan", {})
    if already_applied(latest, plan, bundle, index["answers"], existing, candidates, reviews,
                       dispositions, inferred, archive, rows):
        return {}, latest
    require(not any(entry.get("plan_sha256") == canonical_hash(plan)
                    for entry in metadata.get("incremental_archive_history", [])),
            "Cannot apply an older review after a newer archive update")
    require("last_full_archive_tip" not in metadata or plan["archive_base"] == metadata["last_full_archive_tip"],
            "Archive baseline differs from the last fully indexed commit")
    verify_plan(plan, archive, rows, existing.values())
    unlinked, inferred_answers = inferred_edges(archive, plan, inferred, rows, existing)
    documents = source_documents(archive, plan, reviews, rows)
    require(dispositions.keys() <= existing.keys(), "A disposition refers to an unknown existing pair")
    edges = {key: value for key, value in existing.items()
             if dispositions.get(key, {}).get("decision") != "remove"}
    for key, candidate in candidates.items():
        require(candidate["action"] == ("recheck" if key in existing else "add"),
                f"Candidate action differs from the current answer index: {key}")
        edges[key] = reviewed_edge(key, reviews[key], candidate, plan, rows, documents, existing.get(key))
    edges.update(unlinked)
    answers = relevant_answers(index["answers"], edges, plan, inferred_answers)
    for article_id, ids, levels in relation_fields(edges, answers, rows):
        row = rows[article_id]
        row["cited_by_answer_ids"], row["cited_by_answer_count"], row["answer_reference_levels"] = ids, len(ids), levels
    receipt = make_receipt(plan, bundle, edges, candidates, dispositions, existing, unlinked)
    update_metadata(metadata, answers, edges, plan, receipt)
    index.update(answers=answers, edges=sorted(edges.values(), key=edge_order))
    catalog["metadata"]["answer_references"] = metadata
    validate_index(index, catalog)
    embedded["answerIndex"] = answers
    outputs = {FILES[0]: json_text(index), **catalog_files(catalog, embedded, html, match)}
    return {root / name: text for name, text in outputs.items()}, receipt


def check(root, archive, plan, reviews):
    """Validate the complete change without writing the public data."""
    _, result = prepare_updates(Path(root), Path(archive), plan, reviews)
    return result


def apply(root, archive, plan, reviews):
    """Validate and apply a complete batch, rolling back any failed write."""
    root = Path(root)
    require(not root.resolve().is_relative_to(Path(archive).resolve()), "The archive checkout is read-only")
    lock_path = root / ".work/answer-reference-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        fingerprints = {root / name: file_hash(root / name) for name in FILES}
        outputs, result = prepare_updates(root, Path(archive), plan, reviews)
        require(all(file_hash(path) == fingerprint for path, fingerprint in fingerprints.items()),
                "Answer index or catalog changed while preparing the update")
        if outputs:
            staged_write(outputs)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--archive", type=Path, required=True)
    command.add_argument("--base", required=True)
    command.add_argument("--tip", required=True)
    command.add_argument("--changes", default="all", choices=("all", "added"))
    command.add_argument("--new-article-ids", type=Path)
    command.add_argument("--output", type=Path, required=True)
    for name in ("check", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--reviews", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            ids = read_json(args.new_article_ids) if args.new_article_ids else ()
            plan = prepare(args.root, args.archive, args.base, args.tip, changes=args.changes, new_article_ids=ids)
            write_json(args.output, plan)
            result = {"plan_sha256": canonical_hash(plan), "counts": plan["counts"]}
        else:
            operation = check if args.command == "check" else apply
            result = operation(args.root, args.archive, read_json(args.plan), read_json(args.reviews))
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")
    print(json_text(result), end="")


if __name__ == "__main__":
    main()
