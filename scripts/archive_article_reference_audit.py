"""Reconcile translation links against fixed, read-only archive revisions."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess

from archive_translation_intake import (
    ARTICLE_PATH, PROJECT, archived_objects, article_link, commit_id, git,
)
from audit_missing_zhihu_references import original_key
from site_catalog import DATA_PATTERN, catalog_files, read_site_catalog
from summary_workflow import (
    canonical_hash, digest, json_text, read_json, require, staged_write, write_json,
)


DIRECT = {"zhihu", "archive"}
RECEIPT = "incremental_article_reference_scan"


def _pins(archive, base, tip):
    base, tip = commit_id(archive, base), commit_id(archive, tip)
    require(subprocess.run(["git", "-C", str(archive), "merge-base", "--is-ancestor", base, tip]).returncode == 0,
            "Archive baseline is not an ancestor of the target commit")
    return base, tip


def _paths(archive, commit):
    tree = git(archive, "ls-tree", "-rz", "--name-only", commit, "--", "article").decode().split("\0")
    return {Path(path).stem: path for path in tree if re.fullmatch(ARTICLE_PATH, path)}


def _sources(archive, commit, paths, source_ids):
    source_ids = sorted(set(source_ids) & paths.keys(), key=int)
    data = archived_objects(archive, commit, (paths[article_id] for article_id in source_ids))
    for article_id, (raw, record) in zip(source_ids, data, strict=True):
        yield article_id, ({"source_sha256": hashlib.sha256(raw).hexdigest(),
                            "content_sha256": digest(record["content"])}, record["content"])


class DirectLinks(HTMLParser):
    """Extract actual body anchors and native citation annotations."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.anchor = None
        self.ignored = []

    def handle_starttag(self, tag, pairs):
        if tag in {"script", "style"}:
            self.ignored.append(tag)
        if self.ignored:
            return
        attrs = dict(pairs)
        if tag == "a" and attrs.get("href"):
            self.anchor = (attrs["href"], [])
            self.links.append(self.anchor)
        if tag == "sup" and attrs.get("data-draft-type") == "reference" and attrs.get("data-url"):
            self.links.append((attrs["data-url"], [attrs.get("data-text") or ""]))

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag == "a":
            self.anchor = None

    def handle_data(self, text):
        if self.anchor is not None and not self.ignored:
            self.anchor[1].append(text)


def safe_original_key(url):
    try:
        return original_key(url)
    except ValueError:
        return None


def original_url_targets(graph, articles):
    targets = defaultdict(set)
    redirects = {entry["from"]: entry["to"]
                 for entry in graph["metadata"].get("known_original_url_redirects", [])}
    for article_id, row in articles.items():
        for url in row.get("original_source_urls", []):
            key = safe_original_key(redirects.get(url, url))
            if key:
                targets[key].add(article_id)
    return targets, redirects


def _observations(html, source_id, targets, original_targets, redirects):
    parser = DirectLinks()
    parser.feed(html)
    parser.close()
    direct, originals = defaultdict(list), defaultdict(list)
    for url, text in parser.links:
        evidence = {"url": url, "label": " ".join("".join(text).split())}
        matched = article_link(url)
        if matched:
            article_id, method = matched
            if article_id != source_id and article_id in targets:
                direct[article_id, method].append(evidence)
            continue
        original = safe_original_key(redirects.get(url, url))
        if original is None:
            continue
        matches = original_targets.get(original, set()) - {source_id}
        if matches & targets:
            originals[original].append(evidence)
    return direct, originals


def _scan(archive, base, tip, rows, graph):
    before, after = _paths(archive, base), _paths(archive, tip)
    touched = set(git(archive, "diff", "--no-renames", "--name-only", "-z", base, tip, "--", "article")
                  .decode().split("\0"))
    modified = {i for i in rows if before.get(i) in touched or after.get(i) in touched}
    added = (after.keys() - before.keys()) & rows.keys()
    originals, redirects = original_url_targets(graph, rows)
    source_before = dict(_sources(archive, base, before, modified))
    source_after = dict(_sources(archive, tip, after, modified))
    content_changed = {i for i in modified
                       if source_before.get(i, ({}, ""))[1] != source_after.get(i, ({}, ""))[1]
                       or (i in source_before) != (i in source_after)}
    sources, direct, candidates = [], [], []

    def record(article_id, old, new, all_targets):
        old_meta, old_html = old or (None, "")
        new_meta, new_html = new or (None, "")
        target_scope = rows.keys() if all_targets else added
        new_links, new_originals = _observations(new_html, article_id, target_scope, originals, redirects)
        if all_targets:
            old_links, old_originals = _observations(old_html, article_id, target_scope, originals, redirects)
        else:
            # Unchanged articles are read for backlinks to new targets. Both versions have the same links.
            old_links, old_originals = new_links, new_originals
        found = set(new_links)
        if all_targets:
            found.update(old_links)
        for target_id, method in sorted(found, key=lambda key: (int(key[0]), key[1])):
            key = target_id, method
            direct.append({"source_id": article_id, "target_id": target_id, "method": method,
                           "before": old_links.get(key, []), "after": new_links.get(key, [])})
        for key, occurrences in sorted(new_originals.items()):
            targets = originals[key] - {article_id}
            if all_targets and occurrences == old_originals.get(key) and not targets & added:
                continue
            candidates.append({"id": digest(article_id + "\0" + key), "source_id": article_id,
                               "normalized_url": key, "targets": sorted(targets, key=int),
                               "scope_targets": sorted(targets & target_scope, key=int), "before": old_originals.get(key, []),
                               "after": occurrences})
        if all_targets or found or new_originals:
            sources.append({"id": article_id, "path": after.get(article_id) or before[article_id],
                            "before": old_meta, "after": new_meta,
                            "change": "body_changed" if all_targets else "backlink_scan"})

    for article_id in sorted(content_changed, key=int):
        record(article_id, source_before.get(article_id), source_after.get(article_id), True)
    if added:
        for article_id, source in _sources(archive, tip, after, rows.keys() - content_changed):
            # Only targets newly in the catalog are relevant on unchanged pages.
            record(article_id, source, source, False)
    return {"archive_base": base, "archive_tip": tip,
            "catalog_scope_sha256": canonical_hash({i: rows[i].get("original_source_urls", []) for i in rows}),
            "new_target_ids": sorted(added, key=int), "changed_sources": sorted(content_changed, key=int),
            "sources": sources, "direct": direct, "original_candidates": candidates}


def _pairs(graph, rows):
    pairs = {(edge["source_id"], edge["target_id"]): edge for edge in graph["edges"]}
    require(len(pairs) == len(graph["edges"]), "Duplicate reference graph pair")
    require(all(a != b and a in rows and b in rows for a, b in pairs), "Invalid reference graph endpoints")
    return pairs


def _edge_totals(rows, pairs):
    return {"nodes": len(rows), "edges": len(pairs),
            "nodes_with_outgoing": len({source for source, _ in pairs}),
            "nodes_with_incoming": len({target for _, target in pairs}),
            "link_occurrences": sum(edge["occurrences"] for edge in pairs.values()),
            "method_edge_counts": dict(sorted(Counter(method for edge in pairs.values() for method in edge["via"]).items()))}


def _check_catalog_edges(rows, pairs):
    outgoing, incoming = {}, {}
    for article_id, row in rows.items():
        for field, count, edges, reverse in (
            ("references", "reference_count", outgoing, False),
            ("referenced_by", "referenced_by_count", incoming, True),
        ):
            links = row[field]
            require(len(links) == row[count], f"{article_id}: stale reference count")
            for link in links:
                other = link["id"]
                pair = (other, article_id) if reverse else (article_id, other)
                require(pair not in edges and other in rows and link["title"] == rows[other]["title"],
                        f"{article_id}: duplicate or stale catalog reference")
                edges[pair] = link["via"]
    expected = {pair: edge["via"] for pair, edge in pairs.items()}
    require(outgoing == incoming == expected, "Graph and catalog references differ")


def _site(root):
    root = Path(root)
    catalog, rows, payload, html, match = read_site_catalog(root)
    graph = read_json(root / "data/references.json")
    require(graph["metadata"] == catalog["metadata"]["references"], "Reference metadata differs")
    pairs = _pairs(graph, rows)
    _check_catalog_edges(rows, pairs)
    for name, total in _edge_totals(rows, pairs).items():
        require(graph["metadata"].get(name, total) == total, f"Stale reference total: {name}")
    return catalog, rows, graph, html, match, payload


def make_plan(root, archive, base, tip):
    """Pin both Git snapshots and the staged baseline graph."""
    _, rows, graph, _, _, _ = _site(root)
    base, tip = _pins(archive, base, tip)
    scan = _scan(archive, base, tip, rows, graph)
    pairs = _pairs(graph, rows)
    previous = _before_removals(pairs, scan)
    return {"schema_version": 1, "baseline_graph_sha256": canonical_hash(graph),
            "baseline_disappeared_methods": previous, **scan}


def _object(value):
    return read_json(value) if isinstance(value, (str, Path)) else value


def _review_items(items, keys, title):
    require(isinstance(items, list), f"Missing {title} decisions")
    by_key = {}
    for item in items:
        key = keys(item)
        require(key not in by_key and isinstance(item.get("reason"), str) and item["reason"].strip(),
                f"Duplicate or unexplained {title} decision: {key}")
        by_key[key] = item
    return by_key


def _direct_key(row):
    return row["source_id"], row["target_id"], row["method"]


def _before_removals(pairs, plan):
    previous = []
    for observation in plan["direct"]:
        if observation["before"] and not observation["after"]:
            source, target, method = _direct_key(observation)
            edge = pairs.get((source, target), {})
            if method in edge.get("via", []):
                previous.append({"source_id": source, "target_id": target, "method": method,
                                 "source_urls": sorted(_current_urls(edge, method))})
    return previous


def _validate(root, archive, plan, reviews):
    site = _site(root)
    _, rows, graph, _, _, _ = site
    require(plan.get("schema_version") == 1 and reviews.get("schema_version") == 1,
            "Unsupported reference plan or review schema")
    require(reviews.get("plan_sha256") == canonical_hash(plan), "Reference review is not bound to this plan")
    base, tip = _pins(archive, plan["archive_base"], plan["archive_tip"])
    expected = _scan(archive, base, tip, rows, graph)
    inputs = {"schema_version", "baseline_graph_sha256", "baseline_disappeared_methods"}
    require({k: v for k, v in plan.items() if k not in inputs} == expected,
            "Archive reference evidence, pins, or catalog scope changed")
    lost = {_direct_key(row): row for row in plan["direct"] if row["before"] and not row["after"]}
    prior = plan["baseline_disappeared_methods"]
    require(isinstance(prior, list) and len({_direct_key(row) for row in prior}) == len(prior)
            and {_direct_key(row) for row in prior} <= set(lost), "Malformed snapshot of disappeared methods")
    removals = _review_items(reviews.get("removed_direct"), _direct_key, "direct removal")
    require(set(removals) == set(lost), "Direct removal decisions must cover exactly the disappeared methods")
    require(all(row.get("decision") in {"retain", "remove"} for row in removals.values()),
            "Direct removal decisions must be retain or remove")
    original_items = {row["id"]: row for row in plan["original_candidates"]}
    originals = _review_items(reviews.get("original_candidates"), lambda row: row["id"], "original URL")
    require(set(originals) == set(original_items), "Original URL decisions must cover exactly the candidates")
    for key, row in originals.items():
        require(row.get("decision") in {"add", "dismiss"}, f"Invalid original URL decision: {key}")
        if row["decision"] == "add":
            require(row.get("target_id") in original_items[key]["scope_targets"], f"Out-of-scope original URL target: {key}")
        else:
            require(not row.get("target_id"), f"Dismissed original URL decision specifies a target: {key}")
    return site, lost, removals, original_items, originals


def _current_urls(edge, method):
    return {url for url in edge["source_urls"]
            if (matched := article_link(url)) is not None and matched == (edge["target_id"], method)}


def _remove_direct(edge, observation):
    method = observation["method"]
    if method not in edge["via"]:
        return 0
    urls = _current_urls(edge, method)
    prior_count = sum(link["url"] in urls for link in observation["before"])
    other_methods = set(edge["via"]) - {method}
    if other_methods:
        require(urls and urls <= {link["url"] for link in observation["before"]}
                and len(urls) == prior_count
                and len(edge["source_urls"]) == edge["occurrences"],
                f"{_direct_key(observation)}: mixed historical occurrence counts cannot be split from this evidence")
        removed = prior_count
    else:
        # Another historical direct URL may be from a separately verified page.
        require(urls == set(edge["source_urls"]) and urls <= {link["url"] for link in observation["before"]},
                f"{_direct_key(observation)}: archive disappearance does not establish removal of other historical URLs")
        removed = edge["occurrences"]
    edge["via"] = sorted(other_methods)
    edge["source_urls"] = [url for url in edge["source_urls"] if url not in urls]
    edge["occurrences"] -= removed
    if not other_methods & DIRECT:
        edge["match_quality"] = [quality for quality in edge["match_quality"] if quality != "direct"]
    return removed


def _add(pairs, key, method, occurrences, quality):
    edge = pairs.setdefault(key, {"source_id": key[0], "target_id": key[1],
                                  "via": [], "match_quality": [], "link_texts": [], "source_urls": [], "occurrences": 0})
    observed = occurrences if method not in edge["via"] else [
        link for link in occurrences if link["url"] not in edge["source_urls"]
    ]
    edge["via"] = sorted(set(edge["via"]) | {method})
    edge["match_quality"] = sorted(set(edge["match_quality"]) | {quality})
    edge["source_urls"] = list(dict.fromkeys([*edge["source_urls"], *(link["url"] for link in observed)]))
    edge["link_texts"] = list(dict.fromkeys([*edge["link_texts"], *(link["label"] for link in observed if link["label"])]))
    edge["occurrences"] += len(observed)
    return len(observed)


def _sync_rows(rows, old_pairs, pairs):
    changed = set(pairs) ^ set(old_pairs)
    changed.update(key for key in pairs.keys() & old_pairs.keys() if pairs[key]["via"] != old_pairs[key]["via"])
    for source_id, target_id in sorted(changed, key=lambda pair: tuple(map(int, pair))):
        edge = pairs.get((source_id, target_id))
        for article_id, other, field, count in (
            (source_id, target_id, "references", "reference_count"),
            (target_id, source_id, "referenced_by", "referenced_by_count"),
        ):
            article = rows[article_id]
            link = next((link for link in article[field] if link["id"] == other), None)
            if link is None:
                article[field].append({"id": other, "title": rows[other]["title"], "via": edge["via"]})
            elif edge is None:
                article[field].remove(link)
            else:
                link["via"] = edge["via"]
            article[count] = len(article[field])


def _receipt(plan, reviews, graph, added, removed):
    return {"archive_base": plan["archive_base"], "archive_tip": plan["archive_tip"],
            "plan_sha256": canonical_hash(plan), "reviews_sha256": canonical_hash(reviews),
            "changed_sources": len(plan["changed_sources"]), "new_article_ids": plan["new_target_ids"],
            "direct_pairs_added": added, "direct_pairs_removed": removed,
            "original_candidates_reviewed": len(plan["original_candidates"]),
            "edges_sha256": canonical_hash(graph["edges"])}


def _check_applied(graph, pairs, plan, reviews, removals, original_items, originals):
    receipt = graph["metadata"].get(RECEIPT)
    require(isinstance(receipt, dict) and receipt == _receipt(plan, reviews, graph, receipt.get("direct_pairs_added"),
                                                             receipt.get("direct_pairs_removed")),
            "Applied article reference receipt or indexed edges differ")
    previous = {_direct_key(row): row for row in plan["baseline_disappeared_methods"]}
    for row in plan["direct"]:
        key = row["source_id"], row["target_id"]
        method_key = _direct_key(row)
        methods = set(pairs.get(key, {}).get("via", []))
        if row["after"]:
            require(row["method"] in methods
                    and {link["url"] for link in row["after"]} <= set(pairs[key]["source_urls"]),
                    f"Missing verified direct article reference: {_direct_key(row)}")
        elif removals[method_key]["decision"] == "remove":
            require(row["method"] not in methods, f"Approved direct removal remains: {method_key}")
        elif method_key in previous:
            require(row["method"] in methods
                    and set(previous[method_key]["source_urls"]) <= set(pairs[key]["source_urls"]),
                    f"Reviewed historical citation was not retained: {method_key}")
    for key, review in originals.items():
        if review["decision"] == "add":
            item = original_items[key]
            edge = pairs.get((item["source_id"], review["target_id"]), {})
            require("original" in edge.get("via", []) and {link["url"] for link in item["after"]} <= set(edge["source_urls"]),
                    f"Reviewed original URL is not indexed: {key}")
    return receipt


def check(root, archive, plan, reviews):
    """Verify the applied receipt, Git evidence, reviews, graph and site data."""
    plan, reviews = _object(plan), _object(reviews)
    site, _, removals, original_items, originals = _validate(root, archive, plan, reviews)
    _, rows, graph, _, _, _ = site
    return _check_applied(graph, _pairs(graph, rows), plan, reviews, removals, original_items, originals)


def apply(root, archive, plan, reviews):
    """Apply reviewed changes in the staged catalog. Repeated runs are idempotent."""
    root, plan, reviews = Path(root), _object(plan), _object(reviews)
    require(root.resolve() != PROJECT.resolve() and not (root / ".git").exists()
            and not root.resolve().is_relative_to(Path(archive).resolve()),
            "Apply article reference changes to a staged copy outside the read-only archive")
    site, lost, removals, original_items, originals = _validate(root, archive, plan, reviews)
    catalog, rows, graph, html, match, payload = site
    old_pairs = _pairs(graph, rows)
    prior = graph["metadata"].get(RECEIPT)
    if isinstance(prior, dict) and prior.get("plan_sha256") == canonical_hash(plan):
        return _check_applied(graph, old_pairs, plan, reviews, removals, original_items, originals)
    require(canonical_hash(graph) == plan["baseline_graph_sha256"], "Reference graph changed since preparation")
    require(_before_removals(old_pairs, plan) == plan["baseline_disappeared_methods"],
            "Disappearance plan does not match the baseline graph")
    pending = {(row["id"], link["id"]) for row in rows.values() for link in row["references"]
               if link.get("pending_review")}
    pairs = copy.deepcopy(old_pairs)
    method_delta = Counter()
    for key, row in lost.items():
        if key[:2] in pairs and removals[key]["decision"] == "remove":
            require(key[:2] not in pending, f"{key}: reconcile the pending citation queue before changing this pair")
            method_delta[key[2]] -= _remove_direct(pairs[key[:2]], row)
            if not pairs[key[:2]]["via"]:
                del pairs[key[:2]]
    for row in plan["direct"]:
        if row["after"]:
            key = row["source_id"], row["target_id"]
            require(key not in pending, f"{key}: resolve existing pending citation markers before applying direct links")
            method_delta[row["method"]] += _add(pairs, key, row["method"], row["after"], "direct")
    for number, review in originals.items():
        if review["decision"] == "add":
            item = original_items[number]
            key = item["source_id"], review["target_id"]
            quality = "exact" if len(item["targets"]) == 1 else "reviewed_original"
            method_delta["original"] += _add(pairs, key, "original", item["after"], quality)
    _sync_rows(rows, old_pairs, pairs)
    _check_catalog_edges(rows, pairs)
    old_direct = {key for key, edge in old_pairs.items() if set(edge["via"]) & DIRECT}
    new_direct = {key for key, edge in pairs.items() if set(edge["via"]) & DIRECT}
    graph["edges"] = sorted(pairs.values(), key=lambda edge: (int(edge["source_id"]), int(edge["target_id"])))
    metadata = graph["metadata"]
    totals = Counter(metadata.get("method_occurrence_counts", {}))
    totals.update(method_delta)
    require(all(n >= 0 for n in totals.values()), "Reference occurrence counts would go negative")
    metadata.update(_edge_totals(rows, pairs), method_occurrence_counts=dict(sorted(totals.items())))
    if prior:
        metadata.setdefault("incremental_article_reference_history", []).append(prior)
    metadata[RECEIPT] = _receipt(plan, reviews, graph, len(new_direct - old_direct), len(old_direct - new_direct))
    metadata["missing_zhihu_reference_candidates"] = sum("original" in e["via"] and "zhihu" not in e["via"]
                                                          for e in pairs.values())
    metadata["articles_with_missing_zhihu_references"] = len({e["source_id"] for e in pairs.values()
                                                               if "original" in e["via"] and "zhihu" not in e["via"]})
    catalog["metadata"]["references"] = metadata
    receipt = _check_applied(graph, pairs, plan, reviews, removals, original_items, originals)
    outputs = {"data/references.json": json_text(graph), **catalog_files(catalog, payload, html, match)}
    staged_write({root / name: text for name, text in outputs.items()})
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "apply", "check"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--base", required=True)
            command.add_argument("--tip", required=True)
        else:
            command.add_argument("--reviews", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            require(not args.plan.exists(), "Use a new path for the pinned reference plan")
            result = make_plan(args.root, args.archive, args.base, args.tip)
            write_json(args.plan, result)
            result = {key: len(result[key]) for key in ("changed_sources", "direct", "original_candidates")}
        else:
            operation = apply if args.command == "apply" else check
            result = operation(args.root, args.archive, args.plan, args.reviews)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
