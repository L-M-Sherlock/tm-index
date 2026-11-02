"""Apply evidenced citation-marker corrections without changing summaries."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re

from audit_missing_zhihu_references import git_value, zhihu_article_id


DATA_PATTERN = re.compile(r'(<script id="catalog-data" type="application/json">)(.*?)(</script>)', re.S)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def csv_text(rows, fields):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def article_lists(candidates, audit):
    sources = sorted(candidates["sources"], key=lambda source: (-source["missing_zhihu_reference_count"], source["source_id"]))
    resolved_count = sum(row["status"] == "resolved_direct_link" for row in audit["results"])
    lines = ["# 仍需核对译文链接的文章列表", "", "共 %s 篇来源文章、%s 条候选。按剩余候选数从多到少排列；候选不等于引用错误，是否补链仍须编辑判断。" %
             (len(sources), candidates["metadata"]["candidate_edges"]), "",
             "本次已删除 %s 条过时标记；依据为本地 ZhiHuArchive 提交 %s，不代表实时核验知乎。" % (resolved_count, audit["metadata"]["archive_head"]), "",
             "|序号|来源文章|待核对数|对应目标译文|", "|---:|---|---:|---|"]
    csv_rows = []
    escape = lambda value: value.replace("|", "\\|").replace("\n", " ")
    for index, source in enumerate(sources, 1):
        targets = "；".join("[%s](%s)" % (escape(reference["target_title"]), reference["recommended_zhihu_url"]) for reference in source["references"])
        lines.append("|%s|[%s](%s)|%s|%s|" % (index, escape(source["source_title"]), source["source_zhihu_url"], source["missing_zhihu_reference_count"], targets))
        csv_rows.append({"source_id": source["source_id"], "source_title": source["source_title"], "source_zhihu_url": source["source_zhihu_url"],
                         "missing_zhihu_reference_count": source["missing_zhihu_reference_count"],
                         "target_titles": "；".join(reference["target_title"] for reference in source["references"]),
                         "target_zhihu_urls": "；".join(reference["recommended_zhihu_url"] for reference in source["references"])})
    lines += ["", "## 已移除过时标记的文章", "", "下列为已找到知乎链接或原生引用注释的 %s 对关系；同一来源文章可能仍有其他待核对项。" % resolved_count, "",
              "|来源文章|已解决的目标译文|该来源剩余候选数|", "|---|---|---:|"]
    remaining = {source["source_id"]: source["missing_zhihu_reference_count"] for source in sources}
    for row in audit["results"]:
        if row["status"] == "resolved_direct_link":
            lines.append("|[%s](https://zhuanlan.zhihu.com/p/%s)|[%s](https://zhuanlan.zhihu.com/p/%s)|%s|" %
                         (escape(row["source_title"]), row["source_id"], escape(row["target_title"]), row["target_id"], remaining.get(row["source_id"], 0)))
    return "\n".join(lines) + "\n", "\ufeff" + csv_text(csv_rows, list(csv_rows[0]) if csv_rows else ["source_id", "source_title", "source_zhihu_url", "missing_zhihu_reference_count", "target_titles", "target_zhihu_urls"])


def prepare_updates(root, archive, audit_relative="data/missing-zhihu-reference-recheck.json", report_relative="docs/missing-zhihu-reference-recheck.md"):
    audit_path = root / audit_relative
    audit = json.loads(audit_path.read_bytes())
    candidate_path = root / "data/missing-zhihu-references.json"
    candidate_raw = candidate_path.read_bytes()
    application = audit.get("application", {})
    if digest(candidate_raw) == application.get("candidate_file_sha256_after"):
        # Older applications could mark an audit complete while retaining
        # stale top-level edges.  Do not report that state as a clean no-op.
        candidate = json.loads(candidate_raw)
        source_pairs = [(source["source_id"], reference["target_id"])
                        for source in candidate["sources"] for reference in source["references"]]
        edge_pairs = [(edge["source_id"], edge["target_id"]) for edge in candidate.get("edges", [])]
        if (len(source_pairs) != len(set(source_pairs)) or len(edge_pairs) != len(set(edge_pairs))
                or set(source_pairs) != set(edge_pairs)):
            raise ValueError("Previously applied candidate file has inconsistent top-level edges")
        return {}
    if digest(candidate_raw) != audit["metadata"]["candidate_file_sha256"]:
        raise ValueError("Candidate file changed since audit; no updates applied")
    if git_value(archive, "rev-parse", "HEAD") != audit["metadata"]["archive_head"]:
        raise ValueError("Archive HEAD differs from audit")
    if git_value(archive, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Archive has tracked modifications")
    for source in audit["sources"]:
        if digest((archive / source["path"]).read_bytes()) != source["source_sha256"]:
            raise ValueError("Archive source hash differs from audit")
    if any(row["status"] not in {"still_missing", "resolved_direct_link"} for row in audit["results"]):
        raise ValueError("Unresolved audit statuses require manual handling")
    candidates = json.loads(candidate_raw)
    old_pairs = {(source["source_id"], reference["target_id"]) for source in candidates["sources"] for reference in source["references"]}
    candidate_edges = candidates.get("edges")
    if not isinstance(candidate_edges, list):
        raise ValueError("Candidate JSON is missing its top-level edges")
    edge_pairs = [(edge["source_id"], edge["target_id"]) for edge in candidate_edges]
    if len(edge_pairs) != len(set(edge_pairs)) or not old_pairs <= set(edge_pairs):
        raise ValueError("Candidate top-level edges are duplicated or omit a source candidate")
    audit_pairs = {(row["source_id"], row["target_id"]) for row in audit["results"]}
    if old_pairs != audit_pairs or len(audit_pairs) != len(audit["results"]):
        raise ValueError("Audit does not cover exactly the candidate pairs")
    resolved = {(row["source_id"], row["target_id"]): row for row in audit["results"] if row["status"] == "resolved_direct_link"}
    removed = []
    sources = []
    for source in candidates["sources"]:
        kept = []
        for reference in source["references"]:
            if (source["source_id"], reference["target_id"]) in resolved:
                removed.append({"source_id": source["source_id"], "source_title": source["source_title"], "reference": copy.deepcopy(reference)})
            else:
                kept.append(reference)
        if kept:
            source["references"] = kept
            source["missing_zhihu_reference_count"] = len(kept)
            sources.append(source)
    candidates["sources"] = sources
    # `sources` is the candidate set used by the audit.  Older applications
    # accidentally retained removed entries in `edges`; keep the original
    # order and payload for surviving pairs while discarding those stale rows.
    kept_pairs = {(source["source_id"], reference["target_id"])
                  for source in sources for reference in source["references"]}
    candidates["edges"] = [edge for edge in candidate_edges
                           if (edge["source_id"], edge["target_id"]) in kept_pairs]
    if len(candidates["edges"]) != len(kept_pairs):
        raise ValueError("Candidate top-level edges do not match remaining sources")
    all_references = [reference for source in sources for reference in source["references"]]
    target_counts = Counter(reference["target_id"] for reference in all_references)
    target_titles = {reference["target_id"]: reference["target_title"] for reference in all_references}
    host_counts = Counter(host for reference in all_references for host in set(reference["original_hosts"]))
    metadata = candidates["metadata"]
    old_top_order = {entry["id"]: index for index, entry in enumerate(metadata["top_targets"])}
    metadata.update({"candidate_edges": len(all_references), "source_articles": len(sources), "target_articles": len(target_counts),
                     "original_host_counts": dict(host_counts.most_common()),
                     "top_targets": [{"id": target, "title": target_titles[target], "missing_incoming_zhihu_links": target_counts[target]}
                                     for target in sorted(target_counts, key=lambda target: (-target_counts[target], old_top_order.get(target, 1000000), target))[:50]],
                     "confidence_counts": dict(Counter(reference["confidence"] for reference in all_references)),
                     "category_counts": dict(Counter(reference["category"] for reference in all_references))})
    reviewed_removed = sum(row["reference"]["confidence"] != "unreviewed" for row in removed)
    metadata["prior_review_rows_no_longer_candidates"] += reviewed_removed
    metadata["unreviewed_candidate_edges"] = metadata["confidence_counts"].get("unreviewed", 0)
    metadata["verified_candidate_edges"] = sum(len(source["references"]) for source in sources if source["reference_live_status"] == "verified")
    metadata["unverified_candidate_edges"] = len(all_references) - metadata["verified_candidate_edges"]
    correction = {"checked_at": audit["metadata"]["checked_at"], "archive_head": audit["metadata"]["archive_head"],
                  "audit_file": Path(audit_relative).name, "removed_resolved_pairs": len(resolved),
                  "remaining_candidate_edges": len(all_references), "remaining_source_articles": len(sources),
                  "scope": "Only existing candidate pairs were rechecked against the local archive; historical live-check dates are unchanged."}
    metadata["candidate_recheck"] = correction
    catalog = json.loads((root / "data/catalog.json").read_bytes())
    old_catalog = copy.deepcopy(catalog)
    references = json.loads((root / "data/references.json").read_bytes())
    edges = {(edge["source_id"], edge["target_id"]): edge for edge in references["edges"]}
    added_occurrences = 0
    for pair, result in resolved.items():
        evidence = result["direct_link_evidence"]
        if not evidence or any(zhihu_article_id(link["url"]) != pair[1] for link in evidence):
            raise ValueError("Missing or mismatched direct-link evidence")
        edge = edges[pair]
        if "zhihu" in edge["via"]:
            raise ValueError("Candidate already has Zhihu edge evidence")
        edge["via"] = sorted(set(edge["via"]) | {"zhihu"})
        edge["match_quality"] = sorted(set(edge["match_quality"]) | {"direct"})
        edge["source_urls"] = sorted(set(edge["source_urls"]) | {link["url"] for link in evidence})
        edge["occurrences"] += len(evidence)
        edge["candidate_recheck"] = {"checked_at": correction["checked_at"], "archive_head": correction["archive_head"], "direct_link_evidence": evidence}
        added_occurrences += len(evidence)
    reference_metadata = references["metadata"]
    # After editorial review, the public pending queue is a subset of the
    # mechanical original-link graph.  Keep both counts in their own domain.
    editorial = metadata.get("editorial_review")
    if editorial is not None:
        if not isinstance(editorial, dict):
            raise ValueError("Editorial review metadata is malformed")
        mechanical = [edge for edge in references["edges"]
                      if "original" in edge["via"] and "zhihu" not in edge["via"]]
        mechanical_pairs = {(edge["source_id"], edge["target_id"]) for edge in mechanical}
        if not kept_pairs <= mechanical_pairs:
            raise ValueError("Editorial queue contains a pair absent from the mechanical graph")
        mechanical_count = len(mechanical)
        mechanical_sources = len({edge["source_id"] for edge in mechanical})
        editorial.update({"mechanical_candidates": mechanical_count,
                          "editorially_excluded": mechanical_count - len(kept_pairs),
                          "pending_candidates": len(kept_pairs)})
        reference_metadata.update({"editorial_pending_candidates": len(kept_pairs),
                                   "editorial_pending_source_articles": len(sources),
                                   "editorial_review": copy.deepcopy(editorial)})
    else:
        mechanical_count, mechanical_sources = len(all_references), len(sources)
    reference_metadata.update({"missing_zhihu_reference_candidates": mechanical_count, "articles_with_missing_zhihu_references": mechanical_sources,
                               "method_edge_counts": dict(sorted(Counter(method for edge in references["edges"] for method in edge["via"]).items())),
                               "link_occurrences": sum(edge["occurrences"] for edge in references["edges"]), "candidate_recheck": correction})
    reference_metadata["method_occurrence_counts"]["zhihu"] += added_occurrences
    source_counts = {source["source_id"]: source["missing_zhihu_reference_count"] for source in sources}
    pending_reasons = {(source["source_id"], reference["target_id"]): reference.get("review_reason", "")
                       for source in sources for reference in source["references"]}
    for row in catalog["articles"]:
        row["missing_zhihu_reference_count"] = source_counts.get(row["id"], 0)
        for reference in row["references"]:
            pair = (row["id"], reference["id"])
            reference["via"] = edges[pair]["via"]
            if editorial is not None:
                if pair in kept_pairs:
                    reference["pending_review"] = True
                    reference["pending_review_reason"] = pending_reasons[pair]
                else:
                    reference.pop("pending_review", None)
                    reference.pop("pending_review_reason", None)
        for reference in row["referenced_by"]:
            reference["via"] = edges[(reference["id"], row["id"])]["via"]
    for before, after in zip(old_catalog["articles"], catalog["articles"]):
        ignored = {"missing_zhihu_reference_count", "references", "referenced_by"}
        if {key: value for key, value in before.items() if key not in ignored} != {key: value for key, value in after.items() if key not in ignored}:
            raise ValueError("Unexpected non-reference article changes")
    catalog["metadata"]["references"] = reference_metadata
    validation = json.loads((root / "data/validation.json").read_bytes())
    validation["references"] = reference_metadata
    method = json.loads((root / "data/missing-zhihu-reference-review-method.json").read_bytes())
    method["input"]["candidate_edges"] = len(all_references)
    method["counts"] = {"confidence": metadata["confidence_counts"], "category": metadata["category_counts"]}
    method["validation"].update({"record_count": len(all_references), "reused_review_rows": len(all_references) - metadata["unreviewed_candidate_edges"],
                                 "unreviewed_candidates": metadata["unreviewed_candidate_edges"], "old_reviews_no_longer_candidates": metadata["prior_review_rows_no_longer_candidates"]})
    method["candidate_recheck"] = correction
    if editorial is not None:
        method["editorial_review"] = copy.deepcopy(editorial)
    html = (root / "index.html").read_text(encoding="utf-8")
    matches = list(DATA_PATTERN.finditer(html))
    if len(matches) != 1:
        raise ValueError("Expected exactly one embedded catalog")
    match = matches[0]
    embedded = json.loads(match.group(2))
    if embedded["records"] != old_catalog["articles"] or embedded["report"] != old_catalog["metadata"]:
        raise ValueError("Embedded catalog differs from canonical catalog")
    embedded["records"] = catalog["articles"]
    embedded["report"] = catalog["metadata"]
    embedded_text = json.dumps(embedded, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    html = html[:match.start(2)] + embedded_text + html[match.end(2):]
    candidate_text = json_text(candidates)
    audit["application"] = {"applied_at": datetime.now(timezone.utc).isoformat(), "removed_candidate_edges": len(resolved),
                            "candidate_file_sha256_after": digest(candidate_text.encode("utf-8")), "remaining_candidate_edges": len(all_references),
                            "remaining_source_articles": len(sources), "removed_candidates": removed}
    reader = csv.DictReader(io.StringIO((root / "data/missing-zhihu-references.csv").read_text(encoding="utf-8-sig")))
    fields = reader.fieldnames
    existing_csv_rows = list(reader)
    if {(row["source_id"], row["target_id"]) for row in existing_csv_rows} != old_pairs or len(existing_csv_rows) != len(old_pairs):
        raise ValueError("CSV candidate pairs differ from JSON candidate pairs")
    csv_rows = [row for row in existing_csv_rows if (row["source_id"], row["target_id"]) not in resolved]
    article_markdown, article_csv = article_lists(candidates, audit)
    outputs = {"data/missing-zhihu-references.json": candidate_text, "data/missing-zhihu-references.csv": csv_text(csv_rows, fields),
               "data/missing-zhihu-reference-review-method.json": json_text(method), "data/references.json": json_text(references),
               "data/catalog.json": json_text(catalog), "data/validation.json": json_text(validation), "index.html": html,
               audit_relative: json_text(audit),
               "docs/missing-zhihu-reference-articles.md": article_markdown, "data/missing-zhihu-reference-articles.csv": article_csv}
    report_path = root / report_relative
    report = report_path.read_text(encoding="utf-8")
    report = report.replace("原有引用图谱、页面计数和总结数据未在本次核验中重建。", "历史核验结果保留；过时标记的应用情况见文末，正文和总结不在更新范围。")
    outputs[report_relative] = report + "\n## 过时标记修正\n\n已依据以上证据移除 %s 条候选，同步 JSON/CSV 清单、目录、页面内嵌数据和引用图谱的对应链接类型。现剩 %s 条候选、%s 篇来源文章；总结和正文未修改。文章列表见 [仍需核对译文链接的文章列表](missing-zhihu-reference-articles.md)。\n" % (len(resolved), len(all_references), len(sources))
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--audit", default="data/missing-zhihu-reference-recheck.json")
    parser.add_argument("--report", default="docs/missing-zhihu-reference-recheck.md")
    args = parser.parse_args()
    updates = prepare_updates(args.root, args.archive, args.audit, args.report)
    for relative, text in updates.items():
        (args.root / relative).write_text(text, encoding="utf-8", newline="")
    print("Updated %s files; repeated application is a no-op." % len(updates))


if __name__ == "__main__":
    main()
