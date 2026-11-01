"""Shared, read-only source snapshot verification for summary workflows."""
from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class VisibleStrings(HTMLParser):
    """Collect visible text nodes using the audit snapshot's get_text-style join."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.ignored = [], []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored.append(tag)

    def handle_endtag(self, tag):
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()

    def handle_data(self, text):
        if not self.ignored and text.strip():
            self.parts.append(text.strip())


def _trusted_row(trusted, article_id):
    if "articles" in trusted:
        return {row["id"]: row for row in trusted["articles"]}.get(article_id, {})
    return trusted.get(article_id, {})


def source_body(row, source_dir, trusted, recovery_dir=None):
    """Return a verified body and source method from a frozen snapshot or exact raw recovery."""
    source_dir = Path(source_dir)
    path = source_dir / (row["id"] + ".txt")
    if path.exists():
        text = path.read_text(encoding="utf-8")
        header = json.loads(text.splitlines()[0])
        start, end = "【完整可见正文开始】\n", "\n【完整可见正文结束】"
        if text.count(start) != 1 or text.count(end) != 1:
            raise ValueError("Invalid snapshot markers")
        body = text.split(start, 1)[1].split(end, 1)[0]
        expected = _trusted_row(trusted, row["id"])
        if header.get("id") != row["id"] or header.get("title") != row["title"]:
            raise ValueError("Snapshot identity mismatch")
        for field in ("publisher", "publisher_token"):
            if header.get(field) != row.get(field):
                raise ValueError("Snapshot publisher mismatch")
        if expected.get("source_sha256") != row["source_sha256"] or expected.get("content_sha256") != row["content_sha256"]:
            raise ValueError("Snapshot source hash mismatch")
        if expected.get("text_sha256") != digest(body) or expected.get("text_chars") != len(body) or header.get("text_chars") != len(body):
            raise ValueError("Snapshot text differs from trusted source manifest (hash/length mismatch)")
        method = "verified_snapshot"
    else:
        if recovery_dir is None:
            raise ValueError("Snapshot missing and no exact raw recovery directory provided")
        raw_path = Path(recovery_dir) / (row["id"] + ".json")
        raw_bytes = raw_path.read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != row["source_sha256"]:
            raise ValueError("Recovered raw source hash mismatch")
        original = json.loads(raw_bytes)
        html = original["content"]
        if digest(html) != row["content_sha256"]:
            raise ValueError("Recovered HTML hash mismatch")
        parser = VisibleStrings()
        parser.feed(html)
        parser.close()
        body = "\n".join(parser.parts)
        method = "exact_raw_snapshot_recovery"
    if len(body) != row["read_chars"]:
        raise ValueError("Visible body length mismatch")
    return body, method
