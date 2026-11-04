#!/usr/bin/env python3
"""Reproducible stratified diagnostics. Never writes summaries or site data."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import random
import sys
from collections import Counter
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("summary_audit_base", Path(__file__).with_name("summary_workflow.py"))
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)
source_body = base.summary_sources.source_body
ROOT = Path(__file__).resolve().parents[1]
BOUNDS = (5000, 8000, 10000, 15000, 20000)
VERDICTS = {"adequate", "undercovered", "distorted"}
REPORT_PATH = "data/summary-sampling-audit.json"
INCLUDED_DECISIONS = ("translation", "translated_excerpt", "translated_adaptation")


def validate_config(bounds, model, effort):
    base.require(isinstance(bounds, (list, tuple)) and len(bounds) >= 2
                 and all(type(value) is int and value >= 0 for value in bounds)
                 and all(lower < upper for lower, upper in zip(bounds, bounds[1:])), "Invalid sampling bounds")
    base.nonempty(model, "Missing audit model")
    base.nonempty(effort, "Missing reasoning effort (use unknown if unavailable)")


def report_target(root, relative):
    base.nonempty(relative, "Missing report path")
    path = base.confined(root, relative)
    base.require(path.suffix == ".json" and path != (root / "data/catalog.json").resolve(), "Invalid report path")
    if path.exists():
        base.require(base.read_json(path).get("kind") == "sampling_audit", "Report path is not an audit report")
    return path


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sampled_population(rows, seed, per_stratum, bounds=BOUNDS, included_decisions=None):
    validate_config(bounds, base.MODEL, base.EFFORT)
    if included_decisions is not None:
        base.require(list(included_decisions) == list(INCLUDED_DECISIONS), "Invalid included decisions")
        rows = [row for row in rows if row.get("decision") in included_decisions]
    base.require(type(per_stratum) is int and per_stratum > 0, "Invalid per-stratum sample size")
    rng = random.Random(seed)
    population, strata, selected = [], [], []
    for index, (lower, upper) in enumerate(zip(bounds, bounds[1:])):
        group = sorted((r for r in rows if lower <= r["read_chars"] < upper), key=lambda r: int(r["id"]))
        base.require(len(group) >= per_stratum, f"Stratum {index}: sample exceeds population")
        sample = sorted(rng.sample(group, per_stratum), key=lambda r: int(r["id"]))
        strata.append({"index": index, "lower": lower, "upper": upper, "population_size": len(group),
                       "sample_size": per_stratum, "sample_ids": [r["id"] for r in sample]})
        population.extend({"id": r["id"], "stratum": index, "read_chars": r["read_chars"], "source_sha256": r["source_sha256"],
                           "content_sha256": r["content_sha256"], "summary_sha256": base.digest(r["summary"])} for r in group)
        selected.extend((index, r) for r in sample)
    return population, strata, selected


def assign(items):
    result = {}
    for role, count in (("assessor", 6), ("reviewer", 2)):
        loads = [0] * count
        groups = {f"sample_{role}_{i + 1}": [] for i in range(count)}
        for item in sorted(items, key=lambda a: (-a["read_chars"], int(a["id"]))):
            index = min(range(count), key=lambda i: loads[i])
            groups[f"sample_{role}_{index + 1}"].append(item["id"])
            loads[index] += item["read_chars"]
        result[role] = groups
    return result


def protected_files(root, report_path=REPORT_PATH):
    result = {p.relative_to(root).as_posix(): file_hash(p) for p in sorted((root / "data").iterdir())
              if p.is_file() and p.resolve() != (root / report_path).resolve()}
    for name in ("index.html", "archive-export.js"):
        result[name] = file_hash(root / name)
    return result


def prepare(root, source_dir, source_manifest, recovery_dir, work, run_id, audit_date, seed=20261101, per_stratum=15,
            bounds=BOUNDS, model=base.MODEL, reasoning_effort=base.EFFORT, report_path=REPORT_PATH):
    validate_config(bounds, model, reasoning_effort)
    report_path = report_target(root, report_path).relative_to(root.resolve()).as_posix()
    base.require(not (work / "manifest.json").exists(), "Work directory already contains a frozen run")
    base.require(not work.exists() or not any(work.iterdir()), "Prepare requires a fresh empty work directory; old judgments must not be reused")
    base.date.fromisoformat(audit_date)
    catalog = base.read_json(root / "data/catalog.json")
    population, strata, selected = sampled_population(catalog["articles"], seed, per_stratum, bounds, INCLUDED_DECISIONS)
    trusted_data = base.read_json(source_manifest)
    trusted = {r["id"]: r for r in trusted_data["articles"]} if "articles" in trusted_data else trusted_data
    manifest = {"schema_version": 1, "kind": "sampling_audit", "run_id": run_id, "date": audit_date,
                "seed": seed, "per_stratum": per_stratum, "model": model, "reasoning_effort": reasoning_effort,
                "bounds": list(bounds), "included_decisions": list(INCLUDED_DECISIONS), "report_path": report_path,
                "chunk_chars": 6000, "strata": strata, "population": population, "articles": [], "source_errors": []}
    for index, row in selected:
        item = {key: row[key] for key in ("id", "title", "url", "archive_url", "source_path", "source_sha256", "content_sha256", "read_chars")}
        item.update(stratum=index, summary_sha256=base.digest(row["summary"]), summary_path=f"summaries/{row['id']}.json", chunks=[])
        try:
            body, method = source_body(row, source_dir, trusted, recovery_dir)
            item.update(body_sha256=base.digest(body), source_method=method, source_status="available")
            for number, start in enumerate(range(0, len(body), 6000)):
                relative = f"inputs/{row['id']}/chunk-{number:02}.txt"
                part = body[start:start + 6000]
                item["chunks"].append({"index": number, "start": start, "end": start + len(part), "path": relative})
                target = base.confined(work, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(part, encoding="utf-8")
        except (ValueError, KeyError, TypeError, OSError) as error:
            # Keep the original sampled ID. Never draw a substitute.
            item.update(body_sha256=None, source_method=None, source_status="unavailable", source_error=str(error).replace(str(source_dir), "SNAPSHOTS").replace(str(recovery_dir), "RECOVERY"))
            manifest["source_errors"].append({"id": row["id"], "reason": item["source_error"]})
        base.write_json(work / "inputs" / row["id"] / "article.json", item)
        base.write_json(work / item["summary_path"], {"id": row["id"], "summary": row["summary"], "summary_sha256": item["summary_sha256"]})
        manifest["articles"].append(item)
    manifest["assignments"] = assign(manifest["articles"])
    manifest["protected_files"] = protected_files(root, report_path)
    base.write_json(work / "manifest.json", manifest)
    for role, groups in manifest["assignments"].items():
        for actor, ids in groups.items():
            base.write_json(work / (actor + "-assignment.json"), ids)
    return {"population": len(population), "sampled": len(selected), "source_gaps": len(manifest["source_errors"])}


def validate_manifest(root, work):
    manifest = base.read_json(work / "manifest.json")
    base.require(manifest.get("kind") == "sampling_audit", "Not an audit manifest")
    validate_config(manifest.get("bounds", BOUNDS), manifest.get("model"), manifest.get("reasoning_effort"))
    target = report_target(root, manifest.get("report_path", REPORT_PATH))
    base.require(all(base.confined(root, relative) != target for relative in manifest["protected_files"]), "Report overlaps protected file")
    base.date.fromisoformat(manifest["date"])
    for relative, expected in manifest["protected_files"].items():
        base.require(file_hash(base.confined(root, relative)) == expected, f"Protected file changed: {relative}")
    catalog = base.read_json(root / "data/catalog.json")
    population, strata, sample = sampled_population(catalog["articles"], manifest["seed"], manifest["per_stratum"],
                                                   manifest.get("bounds", BOUNDS), manifest.get("included_decisions"))
    base.require(manifest["population"] == population and manifest["strata"] == strata, "Population or random sample changed")
    base.require([a["id"] for a in manifest["articles"]] == [r["id"] for _, r in sample], "Sample IDs reordered, replaced or duplicated")
    owners, items = {}, {a["id"]: a for a in manifest["articles"]}
    base.require(len(items) == len(sample), "Duplicate sampled ID")
    expected_strata = {row["id"]: index for index, row in sample}
    base.require(all(item["stratum"] == expected_strata[id] for id, item in items.items()), "Article stratum differs from the frozen sampling design")
    for role, limit in (("assessor", 6), ("reviewer", 2)):
        groups = manifest["assignments"][role]
        base.require(0 < len(groups) <= limit, "Too many or missing audit agents")
        allocated = [id for ids in groups.values() for id in ids]
        base.require(len(allocated) == len(items) and set(allocated) == set(items), f"Missing/duplicate {role} assignment")
        owners[role] = {}
        for actor, ids in groups.items():
            base.require(actor.startswith(f"sample_{role}_"), "Invalid actor identity")
            base.require(base.read_json(work / (actor + "-assignment.json")) == ids, "Assignment file changed")
            owners[role].update({id: actor for id in ids})
    rows = base.catalog_rows(catalog)
    bodies, summaries = {}, {}
    for item in items.values():
        row = rows[item["id"]]
        for key in ("id", "title", "url", "archive_url", "source_path", "source_sha256", "content_sha256", "read_chars"):
            base.require(item[key] == row[key], f"{item['id']}: catalog source changed")
        base.require(base.read_json(work / "inputs" / item["id"] / "article.json") == item, "Blind input changed")
        summary = base.read_json(base.confined(work, item["summary_path"]))
        base.require(summary["id"] == item["id"] and summary["summary"] == row["summary"] and summary["summary_sha256"] == item["summary_sha256"] == base.digest(row["summary"]), "Summary changed since sampling")
        summaries[item["id"]] = summary["summary"]
        if item["source_status"] == "unavailable":
            base.require(bool(item.get("source_error")), "Unexplained source gap")
            continue
        base.require(item["source_status"] == "available", "Unknown source status")
        offset, parts = 0, []
        for index, chunk in enumerate(item["chunks"]):
            part = base.confined(work, chunk["path"]).read_text(encoding="utf-8")
            base.require(chunk["index"] == index and chunk["start"] == offset and chunk["end"] == offset + len(part) and 0 < len(part) <= 6000, "Invalid source chunks")
            parts.append(part)
            offset = chunk["end"]
        body = "".join(parts)
        base.require(offset == item["read_chars"] and base.digest(body) == item["body_sha256"], "Body changed since sampling")
        bodies[item["id"]] = body
    return manifest, owners, bodies, summaries


def validate_reading(record, item, config):
    expected = list(range(len(item["chunks"])))
    chunks = record.get("read_chunks")
    base.require(isinstance(chunks, list) and all(type(index) is int for index in chunks), f"{item['id']}: invalid read_chunks")
    base.require(sorted(chunks) == expected, f"{item['id']}: incomplete/duplicate read_chunks")
    for key in ("id", "source_sha256", "body_sha256"):
        base.require(record.get(key) == item[key], f"{item['id']}: {key} mismatch")
    base.require(record.get("model") == config["model"] and record.get("reasoning_effort") == config["reasoning_effort"],
                 f"{item['id']}: incorrect model provenance")


def validate_outline(outline, actor, item, body, config):
    base.require(outline.get("actor") == actor, f"{item['id']}: unexpected outline actor")
    validate_reading(outline, item, config)
    base.nonempty(outline.get("genre"), "Missing genre")
    points = outline.get("major_points")
    base.require(isinstance(points, list) and points, "Missing blind major-point outline")
    keys = []
    for point in points:
        base.nonempty(point.get("key"), "Missing point key")
        base.nonempty(point.get("point"), "Missing substantive major point")
        keys.append(point["key"])
        base.validate_spans(point, item)
        for span in point["source_spans"]:
            base.require(sum(c.isalnum() for c in body[span["start"]:span["end"]]) >= 8, "Source span has no substantive body text")
    base.require(len(keys) == len(set(keys)), "Duplicate point keys")


def derived_verdict(assessment):
    coverage = {p["coverage"] for p in assessment["major_points"]}
    if "distorted" in coverage or assessment["distortions"]:
        return "distorted"
    return "undercovered" if coverage & {"partial", "missing"} else "adequate"


def validate_assessment(assessment, outline, outline_path, actor, item, body, summary, config):
    validate_outline(outline, actor, item, body, config)
    validate_reading(assessment, item, config)
    base.require(assessment.get("actor") == actor, f"{item['id']}: unexpected assessment actor")
    base.require(assessment.get("summary_sha256") == item["summary_sha256"], "Assessment used an outdated summary")
    base.require(assessment.get("outline_sha256") == file_hash(outline_path), "Blind outline changed after assessment")
    base.require(assessment.get("genre") == outline["genre"], "Genre changed after reading summary")
    base.nonempty(assessment.get("rationale"), "Missing verdict rationale")
    points = assessment.get("major_points")
    base.require(isinstance(points, list) and len(points) == len(outline["major_points"]), "Blind points omitted or added after reading summary")
    for point, original in zip(points, outline["major_points"]):
        base.require({k: point.get(k) for k in ("key", "point", "source_spans")} == original, "Blind major point was changed")
        base.require(point.get("coverage") in {"covered", "partial", "missing", "distorted"}, "Invalid point coverage")
        base.nonempty(point.get("reason"), "Missing point coverage reason")
        if point["coverage"] == "missing":
            base.require(point.get("summary_evidence") is None, "Missing point must not fabricate summary evidence")
        else:
            base.validate_evidence(point.get("summary_evidence"), summary, item)
    base.require(isinstance(assessment.get("minor_omissions"), list) and isinstance(assessment.get("distortions"), list), "Invalid issue lists")
    for distortion in assessment["distortions"]:
        base.validate_evidence(distortion.get("claim"), summary, item)
        base.validate_spans(distortion, item)
        base.nonempty(distortion.get("reason"), "Unexplained distortion")
    base.require(assessment.get("verdict") == derived_verdict(assessment), "Verdict conflicts with major-point evidence")


def check(root, work, allow_pending=False, allow_unavailable=False):
    manifest, owners, bodies, summaries = validate_manifest(root, work)
    records, pending, gaps = [], [], []
    for item in manifest["articles"]:
        id = item["id"]
        if item["source_status"] != "available":
            gaps.append(id)
            records.append({"id": id, "title": item["title"], "stratum": item["stratum"], "source": item, "summary": summaries[id], "final_verdict": "unassessable", "source_error": item["source_error"]})
            continue
        first_path, second_path, comparison_path = work / "assessments" / (id + ".json"), work / "independent" / (id + ".json"), work / "comparisons" / (id + ".json")
        found = {}
        for role, path in (("assessor", first_path), ("reviewer", second_path)):
            if not path.exists():
                continue
            actor = owners[role][id]
            outline_path = work / "outlines" / actor / (id + ".json")
            found[role] = base.read_json(path)
            validate_assessment(found[role], base.read_json(outline_path), outline_path, actor, item, bodies[id], summaries[id], manifest)
        if len(found) != 2 or not comparison_path.exists():
            pending.append(id)
            continue
        first, second, comparison = found["assessor"], found["reviewer"], base.read_json(comparison_path)
        base.require(owners["assessor"][id] != owners["reviewer"][id], "Assessment is not independent")
        base.require(comparison.get("id") == id and comparison.get("reviewer") == owners["reviewer"][id], "Unexpected comparison actor")
        for key, path in (("first_assessment_sha256", first_path), ("independent_assessment_sha256", second_path)):
            base.require(comparison.get(key) == file_hash(path), "Comparison used a stale initial judgment")
        base.require(type(comparison.get("needs_adjudication")) is bool and isinstance(comparison.get("conflicts"), list) and bool(comparison.get("notes")), "Incomplete comparison")
        different = first["verdict"] != second["verdict"]
        base.require(not (different or comparison["conflicts"]) or comparison["needs_adjudication"], "Unresolved disagreement was hidden")
        base.require(not comparison["needs_adjudication"] or bool(comparison["conflicts"]), "Adjudication requires a concrete conflict record")
        for conflict in comparison["conflicts"]:
            base.nonempty(conflict.get("issue"), "Empty conflict")
            base.nonempty(conflict.get("reason"), "Unexplained conflict")
            base.validate_spans(conflict, item)
        adjudication = None
        path = work / "adjudications" / (id + ".json")
        if comparison["needs_adjudication"] or path.exists():
            if not path.exists():
                pending.append(id)
                continue
            adjudication = base.read_json(path)
            base.require(adjudication.get("id") == id and adjudication.get("final_verdict") in VERDICTS, "Invalid final adjudication")
            base.require(adjudication.get("first_assessment_sha256") == file_hash(first_path) and adjudication.get("independent_assessment_sha256") == file_hash(second_path), "Adjudication used stale initial judgments")
            base.require(adjudication.get("decider") == "Codex主代理", "Adjudication must be made by the primary agent")
            base.nonempty(adjudication.get("rationale"), "Missing adjudication rationale")
            base.validate_spans(adjudication, item)
        records.append({"id": id, "title": item["title"], "url": item["url"], "stratum": item["stratum"],
                        "source": {k: item[k] for k in ("source_path", "source_sha256", "content_sha256", "body_sha256", "source_method", "read_chars")},
                        "summary": summaries[id], "summary_sha256": item["summary_sha256"], "first_assessment": first,
                        "independent_assessment": second, "comparison": comparison, "adjudication": adjudication,
                        "final_verdict": adjudication["final_verdict"] if adjudication else first["verdict"]})
    base.require(allow_pending or not pending, f"Pending assessments/comparisons/adjudications: {', '.join(pending)}")
    base.require(allow_unavailable or not gaps, f"Unassessable sampled sources (not replaced): {', '.join(gaps)}")
    expected = {a["id"] for a in manifest["articles"]}
    for directory in ("assessments", "independent", "comparisons", "adjudications"):
        actual = {p.stem for p in (work / directory).glob("*.json")}
        base.require(actual <= expected, f"Unexpected sample result in {directory}")
    return manifest, records, pending, gaps


def wilson_interval(k, n):
    base.require(type(k) is int and type(n) is int and 0 <= k <= n and n > 0, "Invalid binomial counts")
    z = 1.959963984540054
    p, z2 = k / n, z * z
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return [max(0.0, center - half), min(1.0, center + half)]


def statistics(manifest, records):
    total = len(manifest["population"])
    layers, weighted_low, weighted_high = [], 0.0, 0.0
    for stratum in manifest["strata"]:
        group = [r for r in records if r["stratum"] == stratum["index"]]
        n = stratum["sample_size"]
        base.require(len(group) == n, "Incomplete stratum cannot be reported")
        counts = Counter(r["final_verdict"] for r in group)
        problems, unknown = counts["undercovered"] + counts["distorted"], counts["unassessable"]
        weight = stratum["population_size"] / total
        weighted_low += weight * problems / n
        weighted_high += weight * (problems + unknown) / n
        layers.append({**stratum, "weight": weight, "counts": {key: counts[key] for key in ("adequate", "undercovered", "distorted", "unassessable")},
                       "class_proportions": {key: counts[key] / n for key in ("adequate", "undercovered", "distorted", "unassessable")},
                       "problem_rate": problems / n if not unknown else None,
                       "problem_rate_bounds": [problems / n, (problems + unknown) / n],
                       "wilson_95_interval": wilson_interval(problems, n) if not unknown else None})
    unknown = sum(r["final_verdict"] == "unassessable" for r in records)
    initial_pairs = [(r["first_assessment"]["verdict"], r["independent_assessment"]["verdict"]) for r in records if r["final_verdict"] != "unassessable"]
    return {"population_size": total, "sample_size": len(records), "source_gaps": unknown, "strata": layers,
            "weighted_problem_rate_estimate": weighted_low if not unknown else None,
            "weighted_problem_rate_bounds": [weighted_low, weighted_high],
            "initial_label_agreement": sum(a == b for a, b in initial_pairs) / len(initial_pairs) if initial_pairs else None,
            "adjudicated_articles": sum(r.get("adjudication") is not None for r in records),
            "counts": dict(Counter(r["final_verdict"] for r in records)),
            "interval_method": "Wilson 95% per stratum, without finite-population correction; no overall confidence interval is claimed.",
            "interpretation": "总体只给按层总体篇数加权的点估计；不能将样本混合比例作为总体估计，也不能给未抽中的单篇文章贴标签。"}


def report(root, work, allow_unavailable=False):
    manifest, records, _, gaps = check(root, work, allow_unavailable=allow_unavailable)
    result = {"schema_version": 1, "kind": "sampling_audit", "run_id": manifest["run_id"], "date": manifest["date"],
              "sampling": {"seed": manifest["seed"], "per_stratum": manifest["per_stratum"], "bounds_left_closed_right_open": list(manifest.get("bounds", BOUNDS)),
                           "method": "Numeric ID order within each stratum; one Python random.Random instance, sequential sample without replacement.",
                           "population": manifest["population"]},
              "model": manifest["model"], "reasoning_effort": manifest["reasoning_effort"], "assignments": manifest["assignments"],
              "assignment_changes": manifest.get("assignment_changes", []), "complete": not gaps,
              "statistics": statistics(manifest, records), "entries": records,
              "validation_note": "来源与结构检查不能证明阅读或语义质量；结论来自两次独立全文审阅及必要的主代理裁定。现有总结未修改。"}
    if "included_decisions" in manifest:
        result["sampling"]["included_decisions"] = manifest["included_decisions"]
    path = report_target(root, manifest.get("report_path", REPORT_PATH))
    result["previous_runs"] = []
    if path.exists():
        prior = base.read_json(path)
        result["previous_runs"] = prior.get("previous_runs", [])
        if prior["run_id"] == result["run_id"]:
            base.require(prior == result, "Accepted audit changed; create a new run rather than overwriting initial judgments")
        else:
            result["previous_runs"] = [*result["previous_runs"], {k: v for k, v in prior.items() if k != "previous_runs"}]
    text = base.json_text(result)
    base.require(not any(token in text for token in ("/Users/", "file://", "localhost")), "Non-public local path in audit report")
    base.staged_write({path: text})
    # Reports only have one designated output. All original data stays fixed.
    for relative, expected in manifest["protected_files"].items():
        base.require(file_hash(root / relative) == expected, f"Protected file changed during reporting: {relative}")
    return result["statistics"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    for flag in ("source-dir", "source-manifest", "work-dir"):
        p.add_argument("--" + flag, type=Path, required=True)
    p.add_argument("--recovery-dir", type=Path)
    p.add_argument("--run-id", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--seed", type=int, default=20261101)
    p.add_argument("--per-stratum", type=int, default=15)
    p.add_argument("--bounds", type=int, nargs="+", default=BOUNDS)
    p.add_argument("--model", default=base.MODEL)
    p.add_argument("--reasoning-effort", default=base.EFFORT)
    p.add_argument("--report-path", default=REPORT_PATH)
    for command in ("check", "report"):
        p = sub.add_parser(command)
        p.add_argument("--work-dir", type=Path, required=True)
        p.add_argument("--allow-unavailable", action="store_true")
        if command == "check":
            p.add_argument("--allow-pending", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.root, args.source_dir, args.source_manifest, args.recovery_dir, args.work_dir, args.run_id, args.date, args.seed, args.per_stratum,
                             args.bounds, args.model, args.reasoning_effort, args.report_path)
        elif args.command == "check":
            m, records, pending, gaps = check(args.root, args.work_dir, args.allow_pending, args.allow_unavailable)
            result = {"run_id": m["run_id"], "checked": len(records), "pending": len(pending), "source_gaps": len(gaps)}
        else:
            result = report(args.root, args.work_dir, args.allow_unavailable)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"summary_sampling_audit: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
