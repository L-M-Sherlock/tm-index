"""Recheck existing missing-link candidates against a read-only archive checkout."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


def unwrap_url(url):
    for attempt in range(5):
        parsed = urlsplit(url)
        if parsed.hostname != "link.zhihu.com":
            return url
        target = parse_qs(parsed.query).get("target", [])
        if not target or target[0] == url:
            return url
        url = target[0]
    return url


def original_key(url):
    unwrapped = unwrap_url(url)
    parsed = urlsplit(unwrapped)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower().removeprefix("www.")
    port = parsed.port
    if port and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        host += ":" + str(port)
    query = [(key, value) for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
             if not key.lower().startswith("utm_") for value in values]
    pathname = parsed.path.rstrip("/")
    if host in {"supermemo.guru", "wiki.issarice.com"} and pathname.startswith("/wiki/") and not parsed.query:
        if unwrapped.split("#", 1)[0].endswith("?"):
            pathname += "?"
        pathname = re.sub(r"%3[fF]$", "?", pathname)
    return urlunsplit(("https", host, pathname, urlencode(sorted(query)), parsed.fragment))


def zhihu_article_id(url):
    parsed = urlsplit(unwrap_url(url))
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "zhuanlan.zhihu.com":
        return None
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        return None
    match = re.fullmatch(r"/p/([0-9]+)/?", parsed.path)
    return match.group(1) if match else None


class ReferenceLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored.append(tag)
        if self.ignored:
            return
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append({"kind": "anchor", "url": attrs["href"]})
        if tag == "sup" and attrs.get("data-draft-type") == "reference" and attrs.get("data-url"):
            self.links.append({"kind": "native_reference", "url": attrs["data-url"]})

    def handle_endtag(self, tag):
        if self.ignored and self.ignored[-1] == tag:
            self.ignored.pop()


def classify_candidate(reference, links):
    direct = [link for link in links if zhihu_article_id(link["url"]) == reference["target_id"]]
    expected = {original_key(url) for url in reference["original_urls"]} - {None}
    original = [link for link in links if original_key(link["url"]) in expected]
    status = "resolved_direct_link" if direct else "still_missing" if original else "original_link_absent"
    return {"status": status, "direct_link_evidence": direct, "original_link_evidence": original}


def git_value(archive, *args):
    return subprocess.check_output(["git", "-C", str(archive), *args], text=True, encoding="utf-8").strip()


def audit(root, archive):
    candidate_path = root / "data/missing-zhihu-references.json"
    candidate_bytes = candidate_path.read_bytes()
    candidates = json.loads(candidate_bytes)
    head_before = git_value(archive, "rev-parse", "HEAD")
    rows = []
    source_evidence = []
    for source in candidates["sources"]:
        source_id = source["source_id"]
        source_path = archive / "article" / (source_id + ".json")
        error = None
        try:
            raw = source_path.read_bytes()
            article = json.loads(raw)
            if str(article.get("id")) != source_id or not isinstance(article.get("content"), str):
                raise ValueError("Archive article identity/content mismatch")
            parser = ReferenceLinks()
            parser.feed(article["content"])
            parser.close()
            source_evidence.append({"source_id": source_id, "path": "article/" + source_id + ".json",
                                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                                    "content_sha256": hashlib.sha256(article["content"].encode("utf-8")).hexdigest(),
                                    "article_updated": article.get("updated"), "link_count": len(parser.links)})
        except (OSError, ValueError) as exception:
            error = str(exception)
        for reference in source["references"]:
            row = {"source_id": source_id, "source_title": source["source_title"],
                   "target_id": reference["target_id"], "target_title": reference["target_title"],
                   "original_urls": reference["original_urls"]}
            row.update({"status": "unverifiable", "error": error} if error else classify_candidate(reference, parser.links))
            rows.append(row)
    head_after = git_value(archive, "rev-parse", "HEAD")
    if head_after != head_before:
        raise ValueError("Archive HEAD changed during the audit")
    pairs = {(row["source_id"], row["target_id"]) for row in rows}
    if len(pairs) != len(rows) or len(rows) != candidates["metadata"]["candidate_edges"]:
        raise ValueError("Candidate coverage/uniqueness mismatch")
    return {"metadata": {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
                         "scope": "Recheck every existing candidate pair; no new candidate discovery or live Zhihu verification.",
                         "archive_head": head_after, "archive_remote": git_value(archive, "remote", "get-url", "origin"),
                         "archive_tracked_changes": git_value(archive, "status", "--porcelain", "--untracked-files=no"),
                         "candidate_file_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                         "candidate_edges": len(rows), "source_articles": len(candidates["sources"]),
                         "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
                         "still_missing_source_articles": len({row["source_id"] for row in rows if row["status"] == "still_missing"}),
                         "url_matching": "Unwrap Zhihu target redirects; normalize scheme/www/trailing slash and tracking queries; preserve fragments and meaningful queries. On supermemo.guru and wiki.issarice.com only, match a terminal wiki-title question mark to its percent-encoded form.",
                         "limitations": ["Latest local archive is not a fresh live read of Zhihu.",
                                          "An absent original link is not proof that a translation link was added.",
                                          "Historical reference data and accepted summaries are not modified."]},
            "sources": source_evidence, "results": rows}


def markdown_report(result):
    metadata = result["metadata"]
    counts = metadata["status_counts"]
    lines = ["# 待核对译文链接：最新本地备份复核", "",
             "核验时间（UTC）：" + metadata["checked_at"],
             "备份仓库提交：" + metadata["archive_head"], "",
             "范围为现有清单的全部 %s 条候选、%s 篇来源文章。识别普通链接、知乎跳转链接和原生引用注释；逐条保存匹配证据及来源文件哈希。" % (metadata["candidate_edges"], metadata["source_articles"]),
             "", "URL 核对保留片段和有效查询参数；对 supermemo.guru 与 wiki.issarice.com 的词条标题，兼容末尾问号与 %3F 的编码差异，避免把编码变化误判为原文链接消失。",
             "", "|结果|条数|", "|---|---:|"]
    labels = {"resolved_direct_link": "已找到目标译文的知乎链接或引用注释",
              "still_missing": "仍链接原文，未找到目标译文的知乎链接或引用注释",
              "original_link_absent": "未找到原候选原文链接，也未找到目标译文链接", "unverifiable": "无法核验"}
    for status, label in labels.items():
        lines.append("|%s|%s|" % (label, counts.get(status, 0)))
    lines += ["", "仍存在候选的来源文章数：%s。" % metadata["still_missing_source_articles"], "",
              "本报告仅复核既有候选，不声称已发现新增文章或全部新候选，也不等于实时核验知乎公开页面。未找到原文链接不等于已补上译文链接。原有引用图谱、页面计数和总结数据未在本次核验中重建。",
              "", "## 逐对结果", "", "|来源文章|目标译文|结果|", "|---|---|---|"]
    for row in result["results"]:
        source = row["source_title"].replace("|", "\|").replace("\n", " ")
        target = row["target_title"].replace("|", "\|").replace("\n", " ")
        lines.append("|%s (%s)|%s (%s)|%s|" % (source, row["source_id"], target, row["target_id"], labels[row["status"]]))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and json.loads(args.output.read_bytes()).get("application"):
        raise ValueError("Applied audit history must be preserved; use a new output/report path")
    result = audit(args.root, args.archive)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps(result["metadata"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
