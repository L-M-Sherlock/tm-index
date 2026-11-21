"""Prepare and revalidate answer citation changes from read-only Git snapshots."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from archive_translation_intake import archived_objects, article_link, git
from audit_missing_zhihu_references import ReferenceLinks, unwrap_url


ACCOUNT_NAMES = {
    "4c592f496dc33822b560b382907ff1d0": "Thoughts Memo",
    "3c9990a12cdbcd92e20b1387b160f0a3": "Jarrett Ye",
}
SHANGHAI = timezone(timedelta(hours=8))
ANSWER_PATH = re.compile(r"answer/([0-9]+)\.json")
CONTEXT_TAGS = {
    "blockquote", "caption", "dd", "div", "dt", "figcaption", "h1", "h2", "h3",
    "h4", "h5", "h6", "li", "p", "pre", "td", "th",
}
NON_CITATION_IMAGE_ATTRS = {
    "img": {"data-actualsrc", "data-default-watermark-src", "data-original", "data-thumbnail", "src"},
    "a": {"data-image"},
}
ZHIHU_ANSWER_LINK_PATHS = {
    "zhuanlan.zhihu.com": r"/appview/p/([0-9]+)/?",
    "zhihu.com": r"/(?:(?:appview/)?p|api/v4/articles)/([0-9]+)/?",
    "www.zhihu.com": r"/(?:(?:appview/)?p|api/v4/articles)/([0-9]+)/?",
    "api.zhihu.com": r"/articles/([0-9]+)/?",
}


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _fingerprint(value):
    return _sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8"))


def _text(value):
    return " ".join(value.split())


class CitationMarkup(HTMLParser):
    """Preserve text, links, annotation fields and context markup for equality checks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        ignored = NON_CITATION_IMAGE_ATTRS.get(tag, ())
        self.tokens.append((tag, tuple((key, value) for key, value in attrs if key not in ignored)))

    def handle_endtag(self, tag):
        self.tokens.append(("/" + tag,))

    def handle_data(self, data):
        self.tokens.append(data)


def _citation_markup(html):
    parser = CitationMarkup()
    parser.feed(html)
    parser.close()
    return parser.tokens


def _answer_article_link(url):
    url = url.strip()
    direct = article_link(url)
    if direct is not None:
        return direct
    try:
        parsed = urlsplit(unwrap_url(url))
        pattern = ZHIHU_ANSWER_LINK_PATHS.get(parsed.hostname)
        if (pattern is None or parsed.scheme not in {"http", "https"} or parsed.username
                or parsed.password or parsed.port not in {None, 80, 443}):
            return None
    except ValueError:
        return None
    match = re.fullmatch(pattern, parsed.path)
    return (match.group(1), "zhihu") if match else None


class AnswerLinks(ReferenceLinks):
    """Capture paragraph context for an anchor or a native citation annotation."""

    def __init__(self):
        super().__init__()
        self._blocks = [{"tag": "body", "index": 0, "parts": []}]
        self._next_block = 1
        self._locations = []
        self._anchor = None
        self._reference = None

    def handle_starttag(self, tag, pairs):
        prior = len(self.links)
        super().handle_starttag(tag, pairs)
        if self.ignored:
            return
        if tag in CONTEXT_TAGS:
            self._blocks.append({"tag": tag, "index": self._next_block, "parts": []})
            self._next_block += 1
        if len(self.links) == prior:
            return
        attrs = dict(pairs)
        link = self.links[-1]
        if tag == "a" and self._reference is not None:
            enclosing_link = _answer_article_link(self._reference)
            if enclosing_link is not None and _answer_article_link(link["url"]) == enclosing_link:
                return
        block = self._blocks[-1]
        location = {
            "link": link,
            "block": block,
            "index": len(block["parts"]),
            "context_tag": "blockquote" if any(b["tag"] == "blockquote" for b in self._blocks)
            else block["tag"],
            "label": attrs.get("data-text" if tag == "sup" else "data-draft-title") or "",
            "evidence": ("native_reference" if tag == "sup" else
                         "link_card" if attrs.get("data-draft-type") == "link-card" else "link"),
            "label_parts": [],
        }
        self._locations.append(location)
        if tag == "a":
            self._anchor = location
        elif tag == "sup":
            self._reference = link["url"]

    def handle_endtag(self, tag):
        if self.ignored:
            super().handle_endtag(tag)
            return
        if tag == "a":
            self._anchor = None
        elif tag == "sup":
            self._reference = None
        if tag in CONTEXT_TAGS:
            for index in range(len(self._blocks) - 1, 0, -1):
                if self._blocks[index]["tag"] == tag:
                    del self._blocks[index:]
                    break

    def handle_data(self, text):
        if self.ignored or self._reference:
            return
        self._blocks[-1]["parts"].append(text)
        if self._anchor is not None:
            self._anchor["label_parts"].append(text)

    def occurrences(self):
        for location in self._locations:
            block = location["block"]
            parts = block["parts"]
            index = location["index"]
            before = _text("".join(parts[:index]))
            after = _text("".join(parts[index:]))
            label = "".join(location["label_parts"]) or location["label"]
            yield {
                **location["link"],
                "context_index": block["index"],
                "context_tag": location["context_tag"],
                "context": " ".join(part for part in (before[-360:], after[:360]) if part),
                "label": _text(label),
                "evidence": location["evidence"],
            }


def direct_pairs(answer_id, html, article_ids):
    """Group all verified direct occurrences by answer and catalog article."""
    parser = AnswerLinks()
    parser.feed(html)
    parser.close()
    pairs = {}
    for occurrence in parser.occurrences():
        match = _answer_article_link(occurrence["url"])
        if match is None or match[0] not in article_ids:
            continue
        article_id, method = match
        pair = pairs.setdefault(article_id, {
            "answer_id": answer_id, "article_id": article_id,
            "via": set(), "evidence": set(), "occurrences": [],
        })
        pair["via"].add(method)
        pair["evidence"].add(occurrence["evidence"])
        pair["occurrences"].append(occurrence)
    for pair in pairs.values():
        pair["via"] = sorted(pair["via"])
        pair["evidence"] = sorted(pair["evidence"])
    return [pairs[article_id] for article_id in sorted(pairs, key=int)]


def _paths(archive, revision):
    raw = git(archive, "ls-tree", "-r", "--name-only", "-z", revision, "--", "answer/")
    return {path for path in raw.decode("utf-8").split("\0") if ANSWER_PATH.fullmatch(path)}


def _commit(archive, revision):
    return git(archive, "rev-parse", "--verify", "--end-of-options",
               f"{revision}^{{commit}}").decode("ascii").strip()


def _target_author(record):
    author = record.get("author")
    if not isinstance(author, dict):
        raise ValueError(f"{record['id']}: answer author metadata is missing")
    known_name = ACCOUNT_NAMES.get(author.get("id"))
    if known_name is not None:
        if known_name != author.get("name"):
            raise ValueError(f"{record['id']}: answer author name does not match the known account ID")
        return known_name
    if author.get("name") in ACCOUNT_NAMES.values():
        raise ValueError(f"{record['id']}: answer author ID does not match the known account name")
    return None


def _snapshot(answer_id, raw, record, author):
    question = record.get("question")
    question_id = str(question.get("id")) if isinstance(question, dict) else ""
    title = question.get("title") if isinstance(question, dict) else None
    timestamp = record.get("created_time")
    votes = record.get("voteup_count")
    if (not question_id.isdecimal() or not isinstance(title, str) or not title.strip()
            or type(timestamp) is not int or timestamp <= 0
            or (votes is not None and (type(votes) is not int or votes < 0))):
        raise ValueError(f"{answer_id}: invalid answer index metadata")
    return {
        "source_sha256": _sha256(raw),
        "content_sha256": _sha256(record["content"].encode("utf-8")),
        "answer": {
            "id": answer_id, "question_id": question_id, "title": title,
            "author": author, "created_time": timestamp, "voteup_count": votes,
            "date": datetime.fromtimestamp(timestamp, SHANGHAI).date().isoformat(),
            "url": f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}",
            "archive_url": f"https://l-m-sherlock.github.io/ZhiHuArchive/{answer_id}.html",
        },
    }


def answer_snapshot(raw, record):
    """Build an index snapshot for an identity-checked target account answer."""
    author = _target_author(record)
    if author is None:
        raise ValueError(f"{record['id']}: answer is outside the indexed accounts")
    return _snapshot(str(record["id"]), raw, record, author)


def _answer_snapshot(archive, revision, path):
    if not isinstance(path, str) or not ANSWER_PATH.fullmatch(path):
        raise ValueError(f"Invalid archive answer path: {path}")
    [(raw, record)] = archived_objects(archive, revision, [path])
    return raw, record


def answer_content(archive, revision, path):
    """Return exact archived answer HTML from a committed snapshot."""
    _, record = _answer_snapshot(archive, _commit(archive, revision), path)
    return record["content"]


def _indexed_edges(existing_edges, article_ids):
    indexed = defaultdict(dict)
    for edge in existing_edges:
        answer_id, article_id = edge["answer_id"], edge["article_id"]
        if (not isinstance(answer_id, str) or not answer_id.isdecimal()
                or article_id not in article_ids or article_id in indexed[answer_id]):
            raise ValueError(f"Invalid or duplicate indexed answer pair: {answer_id}/{article_id}")
        indexed[answer_id][article_id] = edge
    ordered = [indexed[answer_id][article_id] for answer_id in sorted(indexed, key=int)
               for article_id in sorted(indexed[answer_id], key=int)]
    return indexed, _fingerprint(ordered)


def _ids(values, name):
    if isinstance(values, str):
        raise ValueError(f"{name} must contain numeric article ID strings")
    result = set(values)
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value) for value in result):
        raise ValueError(f"{name} must contain numeric article ID strings")
    return result


def _answer_change(answer_id, path, before, after, before_html, html,
                   article_ids, new_ids, previous):
    if before is None and after is None:
        return None, []
    if before is None:
        change = "added"
    elif after is None:
        change = "deleted"
    elif before["content_sha256"] != after["content_sha256"]:
        change = "body_changed"
    elif before["source_sha256"] != after["source_sha256"]:
        change = "metadata_only"
    else:
        change = "unchanged_backlinks"
    citation_equivalent = before is not None and after is not None and (
        change in {"metadata_only", "unchanged_backlinks"}
        or _citation_markup(before_html) == _citation_markup(html)
    )
    targets = article_ids if change in {"added", "body_changed"} else new_ids
    pairs = direct_pairs(answer_id, html, targets) if after is not None and targets else []
    if change == "unchanged_backlinks" and not pairs:
        return None, []

    old_direct = {article_id for article_id, edge in previous.items()
                  if {"zhihu", "archive"}.intersection(edge["via"])}
    matched = {pair["article_id"] for pair in pairs}
    old_inferred = set(previous) - old_direct
    if change in {"body_changed", "deleted"}:
        removed_direct = old_direct - matched
        retained_direct = old_direct & matched
        recheck_inferred = set() if citation_equivalent else old_inferred - matched
    else:
        removed_direct = set()
        retained_direct = old_direct
        recheck_inferred = set()
    candidates = []
    for pair in pairs:
        article_id = pair["article_id"]
        if article_id in previous and (change not in {"added", "body_changed"}
                                       or (citation_equivalent and article_id in old_direct)):
            continue
        candidates.append({
            **pair,
            "action": "recheck" if article_id in previous else "add",
            "answer_path": path,
            "source_sha256": after["source_sha256"],
            "content_sha256": after["content_sha256"],
        })
    return {
        "id": answer_id, "path": path, "change": change, "before": before, "after": after,
        "citation_equivalent": citation_equivalent,
        "direct_pairs": pairs,
        "added_pairs": sorted(matched - previous.keys(), key=int),
        "removed_direct_pairs": sorted(removed_direct, key=int),
        "retained_direct_pairs": sorted(retained_direct, key=int),
        "recheck_inferred_pairs": sorted(recheck_inferred, key=int),
    }, candidates


def make_plan(archive: Path, base: str, tip: str, article_ids, existing_edges=(), *,
              new_article_ids=(), changes="all"):
    """Freeze the answer delta and link evidence for the supplied catalog scope."""
    if changes not in {"all", "added"}:
        raise ValueError("Answer change scope must be 'all' or 'added'")
    archive_base, archive_tip = _commit(archive, base), _commit(archive, tip)
    try:
        git(archive, "merge-base", "--is-ancestor", archive_base, archive_tip)
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        raise ValueError("Archive base is not an ancestor of the tip") from error
    article_ids = _ids(article_ids, "article_ids")
    new_ids = _ids(new_article_ids, "new_article_ids")
    if not new_ids <= article_ids:
        raise ValueError("New article targets must exist in the supplied catalog")
    if changes == "added" and new_ids:
        raise ValueError("An added-answer-only plan cannot close backlinks for newly indexed articles")
    indexed, edges_sha256 = _indexed_edges(existing_edges, article_ids)

    old_paths, tip_paths = _paths(archive, archive_base), _paths(archive, archive_tip)
    diff = git(archive, "diff", "--name-only", "--no-renames", "-z", archive_base, archive_tip,
               "--", "answer/").decode("utf-8").split("\0")
    changed_paths = {path for path in diff if ANSWER_PATH.fullmatch(path)}
    selected = changed_paths if changes == "all" else tip_paths - old_paths
    deferred = sorted(changed_paths - selected)
    old_snapshots = {}
    selected_before = sorted(selected & old_paths)
    for path, (raw, record) in zip(selected_before, archived_objects(archive, archive_base, selected_before),
                                   strict=True):
        actor = _target_author(record)
        answer_id = str(record["id"])
        snapshot = _snapshot(answer_id, raw, record, actor) if actor is not None else None
        old_snapshots[path] = snapshot, record["content"]

    answers, excluded, candidates, days = {}, {}, [], []
    paths = sorted(tip_paths)
    for path, (raw, record) in zip(paths, archived_objects(archive, archive_tip, paths), strict=True):
        answer_id = str(record["id"])
        actor = _target_author(record)
        before, before_html = old_snapshots.get(path, (None, None))
        after = _snapshot(answer_id, raw, record, actor) if actor is not None else None
        if after is not None:
            days.append(after["answer"]["date"])
        if path not in selected:
            if after is None or not new_ids:
                continue
            before = after
        if before is None and after is None:
            excluded[answer_id] = {"id": answer_id, "path": path, "reason": "other_author"}
            continue
        row, review = _answer_change(answer_id, path, before, after, before_html, record["content"],
                                     article_ids, new_ids, indexed.get(answer_id, {}))
        if row is not None:
            answers[answer_id] = row
            candidates.extend(review)
    for path in sorted(selected - tip_paths):
        answer_id = Path(path).stem
        before, before_html = old_snapshots[path]
        if before is None:
            excluded[answer_id] = {"id": answer_id, "path": path, "reason": "other_author"}
            continue
        row, _ = _answer_change(answer_id, path, before, None, before_html, None, article_ids,
                                new_ids, indexed.get(answer_id, {}))
        answers[answer_id] = row
    population = {
        "answers_scanned": len(days), "skipped_other_authors": len(tip_paths) - len(days),
        "earliest_answer_date": min(days, default=None), "latest_answer_date": max(days, default=None),
    }
    action_counts = Counter(row["change"] for row in answers.values())
    pair_counts = Counter(row["action"] for row in candidates)
    return {
        "schema_version": 1, "archive_base": archive_base, "archive_tip": archive_tip,
        "changes": changes, "new_article_ids": sorted(new_ids, key=int),
        "article_ids_sha256": _fingerprint(sorted(article_ids, key=int)),
        "existing_edges_sha256": edges_sha256,
        "answers": dict(sorted(answers.items(), key=lambda pair: int(pair[0]))),
        "excluded": dict(sorted(excluded.items(), key=lambda pair: int(pair[0]))),
        "deferred": deferred,
        "candidates": sorted(candidates, key=lambda row: (int(row["answer_id"]), int(row["article_id"]))),
        "population": population,
        "counts": {
            **population, "changed_paths": len(changed_paths), "deferred": len(deferred),
            "excluded": len(excluded), "changes": dict(sorted(action_counts.items())),
            "candidates": dict(sorted(pair_counts.items())),
            "citation_equivalent_body_changes": sum(row["change"] == "body_changed"
                                                    and row["citation_equivalent"] for row in answers.values()),
            "removed_direct_pairs": sum(len(row["removed_direct_pairs"]) for row in answers.values()),
            "recheck_inferred_pairs": sum(len(row["recheck_inferred_pairs"]) for row in answers.values()),
        },
    }


def verify_plan(plan, archive, article_ids, existing_edges=()):
    """Rebuild the entire evidence set at the pinned versions and reject any mismatch."""
    expected = make_plan(archive, plan["archive_base"], plan["archive_tip"], article_ids,
                         existing_edges, new_article_ids=plan["new_article_ids"], changes=plan["changes"])
    if expected != plan:
        raise ValueError("Answer plan does not match the fixed archive and index inputs")
    return expected
