"""Prepare additions from a pinned Zhihu archive and stage reviewed translation records."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from audit_missing_zhihu_references import original_key, unwrap_url, zhihu_article_id
from summary_workflow import (
    DATA_PATTERN, catalog_rows, digest, file_hash, json_text, read_json, require, staged_write, write_json,
)
from summary_sources import VisibleStrings


PROJECT = Path(__file__).resolve().parents[1]
AUTHOR_FIELDS = (
    "original_authors", "original_author_source", "original_author_status",
    "original_author_evidence", "original_author_notes", "author_entities",
    "adaptation_authors", "original_source_urls", "original_author_aliases",
    "author_extraction_agent",
)
ALLOWED_DECISIONS = {"translation", "translated_excerpt", "translated_adaptation"}
SHANGHAI = timezone(timedelta(hours=8))
ARTICLE_PATH = r"article/([0-9]+)\.json"
ARCHIVE_SITE = "https://l-m-sherlock.github.io/ZhiHuArchive"
DISPLAY_FIELDS = ("title", "publisher", "publisher_token", "column", "date", "voteup_count")


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


def article_link(raw):
    """Use the same exact-host/path rules as the existing reference graph."""
    try:
        article_id = zhihu_article_id(raw)
        if article_id:
            return article_id, "zhihu"
        parsed = urlsplit(unwrap_url(raw))
        if (parsed.scheme in {"http", "https"} and parsed.hostname == "l-m-sherlock.github.io"
                and not parsed.username and not parsed.password and parsed.port in {None, 80, 443}):
            match = re.fullmatch(r"/ZhiHuArchive/([0-9]+)\.html", parsed.path)
            if match:
                return match.group(1), "archive"
    except ValueError:
        return None
    return None


def git(archive, *args):
    return subprocess.check_output(["git", "-C", str(archive), *args])


def archived_objects(archive, commit, paths):
    """Yield exact Git blob bytes and a decoded answer or article for each path."""
    command = ["git", "-C", str(archive), "cat-file", "--batch"]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        try:
            for path in paths:
                require(re.fullmatch(r"(?:article|answer)/[0-9]+\.json", path),
                        f"Invalid archive path: {path}")
                process.stdin.write(f"{commit}:{path}\n".encode())
                process.stdin.flush()
                header = process.stdout.readline().split()
                require(len(header) == 3 and header[1] == b"blob", f"Missing archived object: {path}")
                length = int(header[2])
                raw = process.stdout.read(length)
                require(len(raw) == length and process.stdout.read(1) == b"\n", f"Cannot read archived blob: {path}")
                record = json.loads(raw)
                require(isinstance(record, dict) and str(record.get("id")) == Path(path).stem
                        and isinstance(record.get("content"), str), f"Archived identity/content mismatch: {path}")
                yield raw, record
        finally:
            process.stdin.close()


def commit_id(archive, revision):
    return git(archive, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").decode().strip()


def pinned_additions(archive, baseline, commit):
    base, tip = commit_id(archive, baseline), commit_id(archive, commit)
    require(subprocess.run(["git", "-C", str(archive), "merge-base", "--is-ancestor", base, tip]).returncode == 0,
            "Archive baseline is not an ancestor of the target commit")
    paths = git(archive, "diff", "--no-renames", "--diff-filter=A", "--name-only", "-z",
                base, tip, "--", "article").decode().split("\0")
    return base, tip, [path for path in paths if re.fullmatch(ARTICLE_PATH, path)]


def visible_body(html):
    parser = VisibleStrings()
    parser.feed(html)
    parser.close()
    return "\n".join(parser.parts)


def archive_record(path, raw, record):
    article_id = Path(path).stem
    author = record["author"]
    require(isinstance(record["title"], str) and record["title"].strip(), f"{path}: missing title")
    require(isinstance(author, dict) and all(isinstance(author.get(key), str) and author[key].strip()
            for key in ("name", "url_token")), f"{path}: invalid publisher")
    require(type(record["created"]) is int, f"{path}: invalid creation time")
    column = record.get("column") or {}
    require(isinstance(column, dict) and isinstance(column.get("title", ""), str), f"{path}: invalid column")
    body = visible_body(record["content"])
    voteup = record.get("voteup_count")
    require(voteup is None or type(voteup) is int and voteup >= 0, f"{path}: invalid vote count")
    row = {
        "id": article_id, "title": record["title"], "publisher": author["name"],
        "publisher_token": author["url_token"], "column": column.get("title", ""),
        "date": datetime.fromtimestamp(record["created"], SHANGHAI).date().isoformat(),
        "voteup_count": voteup, "url": f"https://zhuanlan.zhihu.com/p/{article_id}",
        "archive_url": f"{ARCHIVE_SITE}/{article_id}.html", "source_path": path,
        "source_sha256": hashlib.sha256(raw).hexdigest(), "content_sha256": digest(record["content"]),
        "summary": "", "read_chars": len(body),
    }
    source = {key: row[key] for key in ("source_sha256", "content_sha256")}
    source.update(text_sha256=digest(body), text_chars=len(body))
    return row, source, body


def prepare(archive, baseline, commit, work):
    base, tip, paths = pinned_additions(archive, baseline, commit)
    rows, sources = [], {}
    require(not work.resolve().is_relative_to(archive.resolve()), "The archive checkout is read-only")
    require(not work.exists(), "Use a new work directory")
    work.mkdir(parents=True, exist_ok=False)
    for name in ("raw", "body"):
        (work / name).mkdir()
    for path, (raw, record) in zip(paths, archived_objects(archive, tip, paths), strict=True):
        row, source, body = archive_record(path, raw, record)
        article_id = row["id"]
        (work / "raw" / f"{article_id}.json").write_bytes(raw)
        (work / "body" / f"{article_id}.txt").write_text(body, encoding="utf-8")
        rows.append(row)
        sources[article_id] = source
    manifest = {"schema_version": 1, "archive_baseline": base, "archive_commit": tip,
                "article_ids": [row["id"] for row in rows]}
    write_json(work / "base-rows.json", rows)
    write_json(work / "source-manifest.json", sources)
    write_json(work / "intake-manifest.json", manifest)
    return manifest


def validate_intake(work, paths, rows, exclusions, pinned_raw, old_articles, metadata):
    ids = [row["id"] for row in rows]
    require(len(ids) == len(set(ids)), "Duplicate intake IDs")
    require(not set(ids) & set(old_articles), "Staging would replace an existing article")
    publisher_tokens = {row["publisher"]: row["publisher_token"] for row in old_articles.values()}
    aliases = metadata["keyword_aliases"]
    expected_rows, sources, bodies = {}, {}, {}
    for path in paths:
        article_id = Path(path).stem
        raw, archived = pinned_raw[article_id]
        expected, sources[article_id], bodies[article_id] = archive_record(path, raw, archived)
        require(expected["publisher_token"] == publisher_tokens.get(expected["publisher"]),
                f"{article_id}: backup publisher identity is outside the catalog")
        require(file_hash(work / "raw" / f"{article_id}.json") == expected["source_sha256"]
                and file_hash(work / "body" / f"{article_id}.txt") == sources[article_id]["text_sha256"],
                f"{article_id}: prepared source bytes changed")
        expected_rows[article_id] = expected
    require(read_json(work / "base-rows.json") == list(expected_rows.values())
            and read_json(work / "source-manifest.json") == sources, "Prepared source manifest changed")
    chars = sum(row["read_chars"] for row in expected_rows.values())
    excluded_publishers = Counter(expected_rows[article_id]["publisher"] for article_id in exclusions)
    for row in rows:
        article_id = row["id"]
        expected = expected_rows[article_id]
        require(all(row.get(field) == value for field, value in expected.items()),
                f"{article_id}: metadata, source hashes or blank summary differ from pinned archive")
        require(row.get("decision") in ALLOWED_DECISIONS, f"{article_id}: invalid translation decision")
        require(isinstance(row.get("scope_reason"), str) and row["scope_reason"].strip()
                and isinstance(row.get("evidence"), str) and row["evidence"].strip()
                and row["evidence"] in bodies[article_id],
                f"{article_id}: missing or mismatched translation evidence")
        require(all(field in row for field in AUTHOR_FIELDS), f"{article_id}: incomplete author metadata")
        require(row["original_author_status"] in {"resolved", "partial", "unresolved"},
                f"{article_id}: unrecognized attribution status")
        require(isinstance(row["original_author_source"], str) and row["original_author_source"].strip(),
                f"{article_id}: missing attribution provenance")
        require(all(isinstance(row[field], list) for field in (
            "original_authors", "author_entities", "original_author_evidence", "adaptation_authors",
            "original_source_urls", "original_author_aliases",
        )), f"{article_id}: invalid author metadata")
        require(all(isinstance(value, str) and value.strip()
                    for field in ("original_authors", "original_source_urls", "original_author_aliases")
                    for value in row[field]), f"{article_id}: invalid author names or source URLs")
        require(all(isinstance(entity, dict)
                    and all(isinstance(entity.get(field), str) and entity[field].strip()
                            for field in ("name", "type", "role")) for entity in row["author_entities"]),
                f"{article_id}: invalid author entities")
        if row["original_author_status"] == "resolved":
            require(row["original_authors"] and row["author_entities"] and row["original_author_evidence"],
                    f"{article_id}: resolved attribution lacks evidence")
        keywords = row["keywords"]
        require(isinstance(keywords, list) and keywords
                and all(isinstance(word, str) and word.strip() for word in keywords),
                f"{article_id}: missing/invalid keywords")
        normalized = list(dict.fromkeys(aliases.get(word, word) for word in keywords))
        if keywords != normalized:
            row.setdefault("original_keywords", keywords)
            row["keywords"] = normalized
        row.update({
            "reference_live_status": "unverified", "reference_checked_at": None,
            "references": [], "referenced_by": [], "reference_count": 0, "referenced_by_count": 0,
            "missing_zhihu_reference_count": 0, "cited_by_answer_ids": [], "cited_by_answer_count": 0,
            "answer_reference_levels": {},
        })
        require(not any(token in json_text(row) for token in ("/Users/", "file://", "localhost")),
                f"{article_id}: non-public path in article metadata")
    return chars, excluded_publishers


def append_author_index(index, rows):
    by_name = {entry["name"]: entry for entry in index}
    for row in rows:
        entities = defaultdict(list)
        for entity in row["author_entities"]:
            entities[entity["name"]].append(entity)
        for name, matching in entities.items():
            entry = by_name.setdefault(name, {
                "name": name, "entity_types": [], "roles": [], "article_ids": [], "resolved_articles": 0,
                "partial_articles": 0, "aliases": [], "article_count": 0,
            })
            entry["entity_types"] = sorted(set(entry["entity_types"]) | {entity["type"] for entity in matching})
            entry["roles"] = sorted(set(entry["roles"]) | {entity["role"] for entity in matching})
            entry["article_ids"] = sorted([*entry["article_ids"], row["id"]], key=int)
            entry["resolved_articles"] += row["original_author_status"] == "resolved"
            entry["partial_articles"] += row["original_author_status"] == "partial"
            if row["original_authors"] == [name]:
                entry["aliases"] = list(dict.fromkeys([
                    *entry["aliases"], *(alias for alias in row["original_author_aliases"] if alias != name)
                ]))
            entry["article_count"] = len(entry["article_ids"])
    return sorted(by_name.values(), key=lambda entry: (-entry["article_count"], entry["name"]))


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


def append_article_references(graph, articles, new, archive, pin, pinned_raw):
    new_ids = set(new)
    connections = {}
    method_occurrences = Counter()
    new_links_examined = new_self_ignored = new_nontranslations = 0
    old_ids = [article_id for article_id in articles if article_id not in new_ids]

    def record(source, html):
        nonlocal new_links_examined, new_self_ignored, new_nontranslations
        source_is_new = source in new_ids
        parser = DirectLinks()
        parser.feed(html)
        parser.close()
        if source_is_new:
            new_links_examined += len(parser.links)
        for url, label in parser.links:
            resolved = article_link(url)
            if resolved is None:
                continue
            target, method = resolved
            if target == source:
                if source_is_new:
                    new_self_ignored += 1
                continue
            if target not in articles:
                if source_is_new:
                    new_nontranslations += 1
                continue
            if not source_is_new and target not in new_ids:
                continue
            edge = connections.setdefault((source, target), {
                "source_id": source, "target_id": target, "via": [], "match_quality": ["direct"],
                "link_texts": [], "source_urls": [], "occurrences": 0,
            })
            title = " ".join("".join(label).split())
            for key, value in (("via", method), ("link_texts", title), ("source_urls", url)):
                if value and value not in edge[key]:
                    edge[key].append(value)
            edge["occurrences"] += 1
            method_occurrences[method] += 1

    missing = []
    if new_ids:
        paths = {article_id: articles[article_id]["source_path"] for article_id in old_ids}
        archive_paths = set(git(archive, "ls-tree", "-rz", "--name-only", pin, "--", "article").decode().split("\0"))
        present = {article_id: path for article_id, path in paths.items() if path in archive_paths}
        missing = sorted(set(paths) - set(present), key=int)
        for (_, archived), article_id in zip(archived_objects(archive, pin, present.values()), present, strict=True):
            record(article_id, archived["content"])
        for article_id in new:
            record(article_id, pinned_raw[article_id][1]["content"])
    for (source, target), edge in connections.items():
        edge["via"].sort()
        articles[source]["references"].append({"id": target, "title": articles[target]["title"], "via": edge["via"]})
        articles[target]["referenced_by"].append({"id": source, "title": articles[source]["title"], "via": edge["via"]})
        articles[source]["reference_count"] += 1
        articles[target]["referenced_by_count"] += 1
    graph["edges"] = sorted([*graph["edges"], *connections.values()],
                            key=lambda edge: (int(edge["source_id"]), int(edge["target_id"])))
    final_pairs = {(edge["source_id"], edge["target_id"]) for edge in graph["edges"]}
    require(len(final_pairs) == len(graph["edges"]) and all(a != b for a, b in final_pairs),
            "Duplicate or self-referential translation links")
    outgoing = {(row["id"], link["id"]) for row in articles.values() for link in row["references"]}
    incoming = {(link["id"], row["id"]) for row in articles.values() for link in row["referenced_by"]}
    require(outgoing == incoming == final_pairs and all(a in articles and b in articles for a, b in final_pairs),
            "Article reference graph and reverse index do not agree")
    metadata = graph["metadata"]
    metadata.update(nodes=len(articles), edges=len(final_pairs),
                    link_occurrences=sum(edge["occurrences"] for edge in graph["edges"]),
                    nodes_with_outgoing=len({a for a, _ in final_pairs}), nodes_with_incoming=len({b for _, b in final_pairs}),
                    method_edge_counts=dict(sorted(Counter(via for edge in graph["edges"] for via in edge["via"]).items())))
    metadata["link_records_examined"] += new_links_examined
    metadata["self_links_ignored"] += new_self_ignored
    metadata["direct_links_to_nontranslations"] += new_nontranslations
    for method, count in method_occurrences.items():
        metadata["method_occurrence_counts"][method] = metadata["method_occurrence_counts"].get(method, 0) + count
    metadata["incremental_archive_scan"] = {
        "archive_commit": pin,
        "new_translations": len(new),
        "outgoing_links_examined_in_new_articles": new_links_examined,
        "existing_translation_sources_checked_for_backlinks": len(old_ids) - len(missing) if new_ids else 0,
        "missing_existing_translation_sources": missing,
        "direct_edges_added": len(connections),
        "direct_occurrences_added": sum(method_occurrences.values()),
        "scope": "补录与新增译文有关的知乎直链、存档直链和原生引用注释。原文链接及其他改动由本批引用审阅处理。",
    }
    live = metadata.get("live_public_verification")
    if live:
        live["scope_articles"] = len(articles)
        live["unverified_articles"] = sum(row.get("reference_live_status") != "verified" for row in articles.values())
        live["archive_only_articles"] = live.get("archive_only_articles", 0) + len(new)
    return connections, missing


def read_site_catalog(root):
    catalog = read_json(root / "data/catalog.json")
    metadata = catalog["metadata"]
    require(metadata == read_json(root / "data/validation.json"), "Catalog and validation metadata differ")
    html = (root / "index.html").read_text(encoding="utf-8")
    match = DATA_PATTERN.search(html)
    require(match and len(list(DATA_PATTERN.finditer(html))) == 1, "Expected exactly one embedded catalog")
    embedded = json.loads(match[2])
    require(embedded["records"] == catalog["articles"] and embedded["report"] == metadata,
            "Embedded and canonical catalogs differ")
    articles = catalog_rows(catalog)
    export_order = embedded["exportOrder"]
    require(len(export_order) == len(articles) and set(export_order) == set(articles), "Invalid existing export order")
    return catalog, articles, embedded, html, match


def catalog_files(catalog, embedded, html, match):
    metadata = catalog["metadata"]
    embedded["records"] = catalog["articles"]
    embedded["report"] = metadata
    body = json.dumps(embedded, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return {"data/catalog.json": json_text(catalog), "data/validation.json": json_text(metadata),
            "index.html": html[:match.start(2)] + body + html[match.end(2):]}


def staging_outputs(root, work, archive, base_pin, pin, paths, rows, exclusions, annotation_date):
    data = root / "data"
    catalog, articles, embedded, html, match = read_site_catalog(root)
    metadata = catalog["metadata"]
    pinned_raw = {Path(path).stem: record for path, record in zip(paths, archived_objects(archive, pin, paths), strict=True)}
    chars, excluded_publishers = validate_intake(work, paths, rows, exclusions, pinned_raw, articles, metadata)
    new = {row["id"]: row for row in sorted(
        rows, key=lambda row: (row["date"], pinned_raw[row["id"]][1]["created"], int(row["id"])), reverse=True
    )}
    articles.update(new)
    graph = read_json(data / "references.json")
    require(graph["metadata"] == metadata["references"], "Reference metadata is out of sync")
    connections, missing = append_article_references(graph, articles, new, archive, pin, pinned_raw)
    authors = append_author_index(read_json(data / "authors.json"), rows)
    author_lines = (data / "original-authors.jsonl").read_text(encoding="utf-8")
    require(len(author_lines.splitlines()) == len(articles) - len(rows), "Incomplete existing attribution records")
    author_record_ids = {json.loads(line)["id"] for line in author_lines.splitlines()}
    require(author_record_ids == set(articles) - new.keys(), "Existing attribution IDs differ from catalog")
    new_author_lines = "".join(json.dumps(
        {"id": article_id, **{field: new[article_id][field] for field in AUTHOR_FIELDS}}, ensure_ascii=False
    ) + "\n" for article_id in sorted(new, key=int))
    old_keyword_rank = {item["keyword"]: n for n, item in enumerate(read_json(data / "keywords.json"))}
    keyword_counts = Counter(keyword for row in articles.values() for keyword in set(row["keywords"]))
    keywords = [{"keyword": word, "count": keyword_counts[word]} for word in sorted(
        keyword_counts, key=lambda word: (-keyword_counts[word], old_keyword_rank.get(word, len(old_keyword_rank)), word)
    )]

    included_publisher_counts = dict(Counter(row["publisher"] for row in articles.values()))
    included_delta = Counter(row["publisher"] for row in rows)
    publisher_counts = dict(Counter(metadata["publisher_counts"]) + included_delta + excluded_publishers)
    decision_counts = dict(Counter(row["decision"] for row in articles.values()))
    decision_counts["excluded"] = metadata["decision_counts"]["excluded"] + len(exclusions)
    metadata.update({
        "source_articles": metadata["source_articles"] + len(paths),
        "annotated_articles": metadata["annotated_articles"] + len(paths),
        "included_articles": len(articles), "decision_counts": decision_counts,
        "publisher_counts": publisher_counts, "included_publisher_counts": included_publisher_counts,
        "covered_visible_text_chars": metadata["covered_visible_text_chars"] + chars,
        "unique_keywords": len(keywords),
        "keyword_normalized_articles": sum("original_keywords" in row for row in articles.values()),
        "references": graph["metadata"], "latest_incremental_annotation_date": annotation_date,
        "incremental_archive_intake": {
            "archive_commit": pin, "baseline_archive_commit": base_pin,
            "source_articles_added": len(paths), "translations_added": len(rows),
            "excluded_article_ids": sorted(exclusions, key=int),
            "exclusion_reasons": dict(sorted(exclusions.items(), key=lambda pair: int(pair[0]))),
        },
    })
    require(sum(publisher_counts.values()) == metadata["source_articles"] == sum(decision_counts.values()),
            "Inventory totals do not agree")
    metadata["original_authors"].update({
        "articles": len(articles),
        "status_counts": dict(Counter(row["original_author_status"] for row in articles.values())),
        "source_counts": dict(Counter(row["original_author_source"] for row in articles.values())),
        "distinct_credited_names": len(authors),
    })
    # Stable sorting preserves the established relative order of all prior articles.
    catalog["articles"] = sorted([*new.values(), *catalog["articles"]], key=lambda row: row["date"], reverse=True)
    embedded["exportOrder"] = sorted([*embedded["exportOrder"], *reversed(new)], key=lambda article_id: articles[article_id]["date"])
    outputs = {
        **catalog_files(catalog, embedded, html, match),
        "data/authors.json": json_text(authors),
        "data/original-authors.jsonl": author_lines + new_author_lines,
        "data/keywords.json": json_text(keywords),
        "data/references.json": json_text(graph),
    }
    receipt = {
        "new_translations": len(rows), "included_articles": len(articles),
        "new_direct_references": len(connections), "source_articles": metadata["source_articles"],
        "archive_baseline": base_pin, "archive_commit": pin,
        "included_article_ids": list(new), "excluded_articles": exclusions,
        "missing_existing_translation_sources": missing,
    }
    return outputs, receipt


def site_hashes(root):
    paths = [*sorted((root / "data").rglob("*")), root / "index.html", root / "archive-export.js",
             *sorted((root / "docs").glob("missing-zhihu-reference*.md")),
             *sorted((root / "vendor").rglob("*"))]
    return {path.relative_to(root).as_posix(): file_hash(path) for path in paths if path.is_file()}


def stage(root, stage_root, work, row_path, exclusions_path, archive, annotation_date):
    date.fromisoformat(annotation_date)
    root, stage_root, work = root.resolve(), stage_root.resolve(), work.resolve()
    require(not stage_root.exists() and not stage_root.is_relative_to(archive.resolve()),
            "Use a new staged site outside the read-only archive")
    require(not any(stage_root.is_relative_to(root / folder) for folder in ("data", "vendor")),
            "The staged site would overlap its input files")
    require(not work.is_relative_to(archive.resolve())
            and not any(work.is_relative_to(root / folder) for folder in ("data", "vendor")),
            "Use a work directory outside source data")
    manifest = read_json(work / "intake-manifest.json")
    base_pin, pin, paths = pinned_additions(archive, manifest["archive_baseline"], manifest["archive_commit"])
    article_ids = [Path(path).stem for path in paths]
    require(manifest == {"schema_version": 1, "archive_baseline": base_pin, "archive_commit": pin,
                         "article_ids": article_ids}, "Intake manifest does not match the pinned Git additions")
    rows, exclusions = read_json(row_path), read_json(exclusions_path)
    require(isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get("id"), str)
                                         for row in rows), "Intake rows must be a JSON array of article records")
    require(isinstance(exclusions, dict)
            and all(isinstance(reason, str) and reason.strip() for reason in exclusions.values()),
            "Exclusions must map article IDs to specific reasons")
    new_ids = {row["id"] for row in rows}
    require(len(new_ids) == len(rows), "Duplicate intake IDs")
    require(not new_ids & set(exclusions) and new_ids | set(exclusions) == set(article_ids),
            "Included and excluded IDs must cover the pinned archive additions exactly")

    hashes = site_hashes(root)
    outputs, receipt = staging_outputs(root, work, archive, base_pin, pin, paths, rows, exclusions, annotation_date)
    stage_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=stage_root.parent, prefix=".translation-intake-") as temporary:
        copy = Path(temporary) / "site"
        for relative in hashes:
            destination = copy / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, destination)
        require(site_hashes(root) == hashes == site_hashes(copy), "The starting site changed while staging")
        staged_write({copy / relative: text for relative, text in outputs.items()})
        require(not stage_root.exists(), "The destination already exists")
        copy.rename(stage_root)
    receipt.update(baseline_sha256=hashes, staged_sha256=site_hashes(stage_root))
    write_json(work / "translation-staging.json", receipt)
    return receipt


def refresh_metadata(root, archive, commit, article_ids):
    root = root.resolve()
    require(root != PROJECT.resolve() and not (root / ".git").exists() and not root.is_relative_to(archive.resolve()),
            "Refresh display metadata only in an isolated site copy")
    require(isinstance(article_ids, list)
            and all(isinstance(article_id, str) and re.fullmatch("[0-9]+", article_id) for article_id in article_ids)
            and len(article_ids) == len(set(article_ids)), "Provide a JSON array of unique article IDs")
    pin = commit_id(archive, commit)
    hashes = site_hashes(root)
    catalog, articles, embedded, html, match = read_site_catalog(root)
    require(set(article_ids) <= articles.keys(), "The metadata refresh includes an uncataloged article")
    known_publishers = {(row["publisher"], row["publisher_token"]) for row in articles.values()}
    paths = [articles[article_id]["source_path"] for article_id in article_ids]
    receipt, changed_titles, publisher_delta = {}, {}, Counter()
    date_changed = False
    for path, article_id, (raw, record) in zip(paths, article_ids, archived_objects(archive, pin, paths), strict=True):
        updated, source, _ = archive_record(path, raw, record)
        require(updated["id"] == article_id, f"{article_id}: archived metadata is for another article")
        require((updated["publisher"], updated["publisher_token"]) in known_publishers,
                f"{article_id}: publisher identity requires separate scope review")
        current = articles[article_id]
        changes = {field: updated[field] for field in DISPLAY_FIELDS if updated[field] != current[field]}
        if "publisher" in changes:
            publisher_delta[current["publisher"]] -= 1
            publisher_delta[changes["publisher"]] += 1
        if "title" in changes:
            changed_titles[article_id] = changes["title"]
        date_changed |= "date" in changes
        current.update(changes)
        receipt[article_id] = {
            "source_sha256": source["source_sha256"], "content_sha256": source["content_sha256"],
            "catalog_content_sha256": current["content_sha256"], "updated_fields": list(changes),
        }
    for row in articles.values():
        for relation in (*row["references"], *row["referenced_by"]):
            if relation["id"] in changed_titles:
                relation["title"] = changed_titles[relation["id"]]
    if publisher_delta:
        metadata = catalog["metadata"]
        publishers = metadata["publisher_counts"]
        for publisher, delta in publisher_delta.items():
            publishers[publisher] = publishers.get(publisher, 0) + delta
        require(all(count >= 0 for count in publishers.values()), "Publisher totals are inconsistent")
        metadata["included_publisher_counts"] = dict(Counter(row["publisher"] for row in articles.values()))
    if date_changed:
        catalog["articles"].sort(key=lambda row: row["date"], reverse=True)
        embedded["exportOrder"].sort(key=lambda article_id: articles[article_id]["date"])
    outputs = catalog_files(catalog, embedded, html, match)
    require(site_hashes(root) == hashes, "The staged site changed during metadata refresh")
    if any(entry["updated_fields"] for entry in receipt.values()):
        staged_write({root / relative: text for relative, text in outputs.items()})
    return {"archive_commit": pin, "reviewed": receipt,
            "updated_articles": sum(bool(item["updated_fields"]) for item in receipt.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pin = commands.add_parser("prepare", help="freeze added archive article records for editorial review")
    pin.add_argument("--archive", type=Path, required=True)
    pin.add_argument("--archive-baseline", required=True)
    pin.add_argument("--archive-commit", required=True)
    pin.add_argument("--work-dir", type=Path, required=True)
    staged = commands.add_parser("stage", help="build a new site copy from reviewed article records")
    staged.add_argument("--root", type=Path, default=PROJECT)
    staged.add_argument("--stage-root", type=Path, required=True)
    staged.add_argument("--work-dir", type=Path, required=True)
    staged.add_argument("--rows", type=Path, required=True)
    staged.add_argument("--exclusions", type=Path, required=True)
    staged.add_argument("--archive", type=Path, required=True)
    staged.add_argument("--date", required=True)
    refresh = commands.add_parser("refresh-metadata", help="update selected existing translations' display fields")
    refresh.add_argument("--stage-root", type=Path, required=True)
    refresh.add_argument("--archive", type=Path, required=True)
    refresh.add_argument("--archive-commit", required=True)
    refresh.add_argument("--article-ids-file", type=Path, required=True)
    refresh.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.archive, args.archive_baseline, args.archive_commit, args.work_dir)
        elif args.command == "stage":
            result = stage(args.root, args.stage_root, args.work_dir, args.rows, args.exclusions, args.archive, args.date)
        else:
            require(args.receipt is None or not args.receipt.resolve().is_relative_to(args.archive.resolve()),
                    "The archive checkout is read-only")
            result = refresh_metadata(args.stage_root, args.archive, args.archive_commit, read_json(args.article_ids_file))
            if args.receipt is not None:
                write_json(args.receipt, result)
                result = {key: result[key] for key in ("archive_commit", "updated_articles")}
    except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
