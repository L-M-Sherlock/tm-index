#!/usr/bin/env python3
"""Prepare, validate and apply human/agent-authored summaries. No model calls."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-6-luna"
EFFORT = "max"
DATA_PATTERN = re.compile(r'(<script id="catalog-data" type="application/json">)(.*?)(</script>)', re.S)
CHECKPOINT_ORDER_NOTE = "Checkpoint time records the first observed file bytes only; it does not prove prior blind-reading order."
SOURCE_SPEC = importlib.util.spec_from_file_location("summary_sources", Path(__file__).with_name("summary_sources.py"))
summary_sources = importlib.util.module_from_spec(SOURCE_SPEC)
SOURCE_SPEC.loader.exec_module(summary_sources)


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


def model_config(value):
    fields = ("model", "reasoning_effort")
    if not any(key in value for key in fields):
        return {"model": MODEL, "reasoning_effort": EFFORT}
    config = {key: value.get(key) for key in fields}
    require(all(isinstance(setting, str) and bool(setting.strip()) and setting == setting.strip()
                for setting in config.values()), "Invalid model configuration")
    return config


def validate_model_config(manifest):
    require("model" in manifest and "reasoning_effort" in manifest, "Invalid manifest model")
    config = model_config(manifest)
    for item in manifest["articles"]:
        require(model_config(item) == config, f"{item['id']}: model configuration differs from manifest")
    return config


def assign_articles(items, actor_prefix="long", excluded_ids=()):
    excluded_ids = set(excluded_ids)
    eligible = [item for item in items if item["id"] not in excluded_ids]
    assignments = {}
    for role, count in (("writer", 6), ("reviewer", 2)):
        loads, groups = [0] * count, [[] for _ in range(count)]
        for item in eligible:
            index = min(range(count), key=lambda i: loads[i])
            groups[index].append(item["id"])
            loads[index] += item["read_chars"]
        assignments[role] = {f"{actor_prefix}_{role}_{index}": ids for index, ids in enumerate(groups, 1)}
    return assignments


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def preserve_files(root):
    mutable = {"data/catalog.json", "data/validation.json", "data/summary-reviews.json", "data/summary-revisions.json"}
    files = {str(path.relative_to(root)): file_hash(path) for path in sorted((root / "data").iterdir())
             if path.is_file() and str(path.relative_to(root)) not in mutable}
    export = root / "archive-export.js"
    if export.exists():
        files["archive-export.js"] = file_hash(export)
    return files


def verify_preserve_files(root, manifest):
    expected = manifest.get("preserve_files", {})
    require(isinstance(expected, dict), "Invalid preserve_files")
    current = preserve_files(root)
    require(set(current) == set(expected), "Preserved file inventory changed")
    for relative, expected_hash in expected.items():
        require(current[relative] == expected_hash, f"Preserved file changed: {relative}")


def _format_projection(value, ignored_fields=()):
    ignored_fields = set(ignored_fields)
    if isinstance(value, dict):
        return {key: ("<format-span>" if key == "source_spans" else _format_projection(child, ignored_fields))
                for key, child in value.items() if key not in ignored_fields}
    if isinstance(value, list):
        return [_format_projection(child, ignored_fields) for child in value]
    return value


def _normalize_initial_reason_aliases(value):
    """Treat the historical per-point `reason` name as `old_coverage_reason` only in initials."""
    normalized = copy.deepcopy(value)
    points = normalized.get("major_points") if isinstance(normalized, dict) else None
    if isinstance(points, list):
        for point in points:
            if not isinstance(point, dict):
                continue
            if "reason" in point and "old_coverage_reason" in point:
                require(point["reason"] == point["old_coverage_reason"],
                        "Format correction has conflicting reason/old_coverage_reason text")
                point.pop("reason")
            elif "reason" in point:
                point["old_coverage_reason"] = point.pop("reason")
    return normalized


def _validate_format_corrections(work, manifest, owners):
    directory = Path(work) / "format-corrections"
    if not directory.exists():
        require(not manifest.get("initial_point_repairs"), "Approved initial point repairs have no format-correction logs")
        return [], {}
    items = {item["id"]: item for item in manifest["articles"]}
    valid_actors = (set(owners["writer"].values()) | set(owners["reviewer"].values())
                    | set(manifest.get("assignments", {}).get("reviewer", {})) | {"Codex主代理"})
    approved_point_repairs = {}
    for repair in manifest.get("initial_point_repairs", []):
        require(isinstance(repair, dict), "Invalid initial_point_repairs entry")
        article_id, actor, key = repair.get("id"), repair.get("actor"), repair.get("key")
        require(article_id in items and isinstance(key, str) and bool(key), "Invalid initial point repair identity")
        require(actor in {owners["writer"].get(article_id), owners["reviewer"].get(article_id)},
                f"{article_id}: initial point repair is not for an assigned reader")
        for field in ("before_point_sha256", "after_point_sha256"):
            require(isinstance(repair.get(field), str) and re.fullmatch(r"[0-9a-f]{64}", repair[field]),
                    f"{article_id}: invalid initial point repair {field}")
        nonempty(repair.get("reason"), f"{article_id}: initial point repair reason missing")
        repair_key = (article_id, actor, key)
        require(repair_key not in approved_point_repairs, f"{article_id}: duplicate initial point repair")
        outline_path = Path(work) / "outlines" / actor / (article_id + ".json")
        require(outline_path.exists(), f"{article_id}: point repair has no current blind outline")
        outline = read_json(outline_path)
        outline_points = {point.get("key"): point for point in outline.get("major_points", [])}
        require(key in outline_points, f"{article_id}: point repair key is absent from the blind outline")
        require(digest(outline_points[key].get("point", "")) == repair["after_point_sha256"],
                f"{article_id}: point repair after text differs from the blind outline")
        approved_point_repairs[repair_key] = repair
    used_point_repairs = set()
    allowed_roles = {"outlines": None, "reviewer-outlines": "reviewer", "writers": "writer",
                     "reviewers": "reviewer", "initial": "writer", "reviewer-initial": "reviewer",
                     "requests": None, "adjudications": None}
    logs, chains, aliases, backup_paths = [], {}, {}, set()
    for log_path in sorted(directory.glob("*.json")):
        log = read_json(log_path)
        actor = log.get("actor")
        require(actor == log_path.stem and actor in valid_actors, f"Invalid format-correction actor: {log_path.name}")
        nonempty(log.get("reason"), f"{log_path.name}: missing format-correction reason")
        files = log.get("files")
        require(isinstance(files, list), f"{log_path.name}: invalid format-correction files")
        public_files = []
        for record in files:
            require(isinstance(record, dict), f"{log_path.name}: invalid corrected-file record")
            article_id, relative, backup_relative = record.get("id"), record.get("path"), record.get("backup_path")
            require(article_id in items, f"{log_path.name}: correction targets an article outside the run")
            require(isinstance(relative, str) and isinstance(backup_relative, str), f"{log_path.name}: correction paths must be relative strings")
            rel_path, backup_path = Path(relative), Path(backup_relative)
            require(not rel_path.is_absolute() and not backup_path.is_absolute(), f"{log_path.name}: absolute correction path is forbidden")
            require(".." not in rel_path.parts and ".." not in backup_path.parts, f"{log_path.name}: correction path escapes work directory")
            target = confined(work, relative)
            backup = confined(work, backup_relative)
            require(target.exists() and backup.exists(), f"{log_path.name}: corrected file or backup is missing")
            before_hash, after_hash = record.get("before_sha256"), record.get("after_sha256")
            require(isinstance(before_hash, str) and re.fullmatch(r"[0-9a-f]{64}", before_hash), f"{log_path.name}: invalid before hash")
            require(isinstance(after_hash, str) and re.fullmatch(r"[0-9a-f]{64}", after_hash), f"{log_path.name}: invalid after hash")
            require(file_hash(backup) == before_hash, f"{log_path.name}: backup hash mismatch")
            require(backup_relative not in backup_paths, f"Duplicate format-correction backup: {backup_relative}")
            backup_paths.add(backup_relative)
            changes = record.get("changes")
            require(isinstance(changes, list), f"{log_path.name}: changes must be a list")
            parts = rel_path.parts
            require(len(parts) in (2, 3) and parts[0] in allowed_roles and parts[-1] == article_id + ".json",
                    f"{log_path.name}: unexpected corrected-file path {relative}")
            role = allowed_roles[parts[0]]
            if parts[0] == "outlines":
                role = "writer" if actor in owners["writer"].values() else "reviewer" if actor in owners["reviewer"].values() else None
                require(parts[1] == actor and role is not None, f"{log_path.name}: actor does not own {relative}")
            if parts[0] == "adjudications":
                require(actor == "Codex主代理", f"{log_path.name}: only the primary agent may correct adjudications")
            if parts[0] == "requests":
                require(actor in {owners["writer"].get(article_id), owners["reviewer"].get(article_id)},
                        f"{log_path.name}: request correction is not from an assigned reader")
            if role is not None:
                transfer = manifest.get("final_reviewer_transfers", {}).get(article_id, {})
                owns_final_review = (parts[0] == "reviewers" and role == "reviewer"
                                     and transfer.get("from") == owners["reviewer"].get(article_id)
                                     and transfer.get("to") == actor)
                require(owners[role].get(article_id) == actor or owns_final_review,
                        f"{log_path.name}: actor does not own {relative}")
            elif actor != "Codex主代理" and parts[0] not in {"outlines", "requests"}:
                require(actor in valid_actors, f"{log_path.name}: invalid actor for {relative}")
            frozen_path = parts[0] in {"outlines", "reviewer-outlines", "initial", "reviewer-initial"}
            aliases.setdefault(relative, set()).update((before_hash, after_hash))
            chains.setdefault(relative, []).append({"id": article_id, "path": relative, "backup_path": backup_relative,
                "before_sha256": before_hash, "after_sha256": after_hash, "target": target, "backup": backup,
                "frozen": frozen_path, "section": parts[0], "actor": actor, "log": log_path.name,
                "order": len(chains.get(relative, []))})
            public_files.append({key: record[key] for key in ("id", "path", "backup_path", "before_sha256", "after_sha256", "changes")})
        logs.append({"path": f"format-corrections/{log_path.name}",
                     "log": {"actor": actor, "reason": log["reason"], "files": public_files}})

    snapshot_index = {}
    for snapshot_root in (Path(work) / "format-before", Path(work) / "draft-before", Path(work) / "draft-revisions" / "before"):
        if snapshot_root.exists():
            for snapshot in snapshot_root.rglob("*.json"):
                snapshot_index.setdefault(file_hash(snapshot), snapshot)

    for relative, records in chains.items():
        # Frozen blind/initial artifacts must be one continuous chain. Mutable
        # drafts and reviews may have separate components when a substantive
        # draft revision occurred between two format-only corrections.
        frozen = all(record["frozen"] for record in records)
        unused = list(records)
        components = []
        while unused:
            starts = [record for record in unused
                      if not any(other is not record and other["after_sha256"] == record["before_sha256"] for other in unused)]
            require(bool(starts), f"Cyclic format-correction hash chain for {relative}")
            if frozen:
                require(len(starts) == 1, f"Ambiguous or disconnected format-correction chain for {relative}")
            for start in sorted(starts, key=lambda record: record["order"]):
                if start not in unused:
                    continue
                component = [start]
                unused.remove(start)
                current = start
                while True:
                    following = [record for record in unused if record["before_sha256"] == current["after_sha256"]]
                    if not following:
                        break
                    require(len(following) == 1, f"Branched format-correction chain for {relative}")
                    current = following[0]
                    component.append(current)
                    unused.remove(current)
                components.append(component)
        components.sort(key=lambda component: component[0]["order"])
        if frozen:
            require(len(components) == 1, f"Frozen artifact has disconnected format-correction components: {relative}")
            final = components[0][-1]
            require(file_hash(final["target"]) == final["after_sha256"],
                    f"{final['log']}: frozen corrected file no longer matches its final after hash")
        for component in components:
            for index, record in enumerate(component):
                before_obj = read_json(record["backup"])
                after_obj = None
                if index + 1 < len(component):
                    following = component[index + 1]
                    require(record["after_sha256"] == following["before_sha256"],
                            f"Broken format-correction hash chain for {relative}")
                    after_obj = read_json(following["backup"])
                else:
                    current_hash = file_hash(record["target"])
                    aliases[relative].add(current_hash)
                    if frozen:
                        after_obj = read_json(record["target"])
                    elif current_hash == record["after_sha256"]:
                        after_obj = read_json(record["target"])
                    elif record["after_sha256"] in snapshot_index:
                        after_obj = read_json(snapshot_index[record["after_sha256"]])
                if after_obj is not None:
                    ignored_fields = set()
                    if record["section"] in {"initial", "reviewer-initial"}:
                        before_points = {point.get("key"): point for point in before_obj.get("major_points", [])}
                        after_points = {point.get("key"): point for point in after_obj.get("major_points", [])}
                        for repair_key, repair in approved_point_repairs.items():
                            repair_id, repair_actor, point_key = repair_key
                            if repair_id != record["id"] or repair_actor != record["actor"]:
                                continue
                            before_point = before_points.get(point_key, {}).get("point")
                            after_point = after_points.get(point_key, {}).get("point")
                            if before_point == after_point:
                                continue
                            require(digest(before_point or "") == repair["before_point_sha256"]
                                    and digest(after_point or "") == repair["after_point_sha256"],
                                    f"{record['id']}: initial point repair text hash mismatch for {point_key}")
                            require(repair_key not in used_point_repairs,
                                    f"{record['id']}: initial point repair was applied more than once for {point_key}")
                            before_obj["major_points"] = [
                                ({**point, "point": after_point} if point.get("key") == point_key else point)
                                for point in before_obj.get("major_points", [])]
                            used_point_repairs.add(repair_key)
                        ignored_fields.update(("outline_sha256", "old_evidence"))
                        before_obj = _normalize_initial_reason_aliases(before_obj)
                        after_obj = _normalize_initial_reason_aliases(after_obj)
                        before_distortions = before_obj.get("distortions", [])
                        after_distortions = after_obj.get("distortions", [])
                        if (isinstance(before_distortions, list) and isinstance(after_distortions, list)
                                and len(before_distortions) == len(after_distortions)):
                            for before_distortion, after_distortion in zip(before_distortions, after_distortions):
                                if not isinstance(before_distortion, dict) or not isinstance(after_distortion, dict):
                                    continue
                                before_claim = before_distortion.get("claim")
                                after_claim = after_distortion.get("claim")
                                if before_claim == after_claim:
                                    continue
                                old_summary = items[record["id"]].get("summary")
                                require(isinstance(before_claim, str) and isinstance(after_claim, str)
                                        and isinstance(old_summary, str)
                                        and 0 < sum(char.isalnum() for char in before_claim) < 8
                                        and sum(char.isalnum() for char in after_claim) >= 8
                                        and before_claim in after_claim
                                        and before_claim in old_summary and after_claim in old_summary,
                                        f"Invalid short distortion claim expansion in {relative}")
                                before_distortion["claim"] = after_claim
                    if record["section"] in {"writers", "reviewers"}:
                        ignored_fields.update(("blind_outline_sha256", "initial_assessment_sha256", "summary_evidence"))
                    if record["section"] == "writers":
                        # Only paragraph breaks after sentence punctuation are
                        # cosmetic; arbitrary whitespace can change a claim.
                        before_summary = before_obj.get("summary")
                        after_summary = after_obj.get("summary")
                        if isinstance(before_summary, str) and isinstance(after_summary, str):
                            def paragraphs(text):
                                return re.sub(r"(?<=[。！？!?；;])\n\n", "", text)
                            if paragraphs(before_summary) == paragraphs(after_summary):
                                before_obj["summary"] = paragraphs(before_summary)
                                after_obj["summary"] = paragraphs(after_summary)
                    if record["section"] == "reviewers":
                        ignored_fields.add("blind_key")
                    if record["section"] in {"requests", "adjudications"}:
                        ignored_fields.update(("writer_initial_sha256", "reviewer_initial_sha256"))
                    item = items[record["id"]]
                    if record["section"] in {"outlines", "writers"}:
                        before_source = before_obj.get("source_sha256")
                        after_source = after_obj.get("source_sha256")
                        if before_source != after_source:
                            before_is_valid_sha = isinstance(before_source, str) and re.fullmatch(r"[0-9a-fA-F]{64}", before_source)
                            require(not before_is_valid_sha and after_source == item["source_sha256"]
                                    and before_obj.get("body_sha256") == item["body_sha256"]
                                    and after_obj.get("body_sha256") == item["body_sha256"],
                                    f"Invalid source_sha256 format correction for {relative}")
                            ignored_fields.add("source_sha256")
                        before_chunks, after_chunks = before_obj.get("read_chunks"), after_obj.get("read_chunks")
                        if before_chunks != after_chunks:
                            expected_chunks = list(range(len(item["chunks"])))
                            valid_before_list = (isinstance(before_chunks, list)
                                                 and all(type(chunk) is int for chunk in before_chunks)
                                                 and len(before_chunks) == len(set(before_chunks))
                                                 and set(expected_chunks) <= set(before_chunks)
                                                 and all(chunk < 0 or chunk >= len(expected_chunks)
                                                         for chunk in set(before_chunks) - set(expected_chunks)))
                            valid_before_count = (type(before_chunks) is int and before_chunks == len(expected_chunks)
                                                  and before_obj.get("body_sha256") == item["body_sha256"]
                                                  and after_obj.get("body_sha256") == item["body_sha256"])
                            valid_before = valid_before_list or valid_before_count
                            require(valid_before and after_chunks == expected_chunks,
                                    f"Invalid read_chunks format correction for {relative}")
                            ignored_fields.add("read_chunks")
                    require(_format_projection(before_obj, ignored_fields) == _format_projection(after_obj, ignored_fields),
                            f"Format correction changed non-location judgment content in {relative}")
    require(used_point_repairs == set(approved_point_repairs),
            "initial_point_repairs entries do not match logged initial point changes")
    return logs, aliases


def _validate_input_exceptions(work, manifest, owners):
    exceptions = manifest.get("input_exceptions", [])
    require(isinstance(exceptions, list), "Invalid input_exceptions")
    items = {item["id"]: item for item in manifest["articles"]}
    seen = set()
    actor_prefix = manifest.get("actor_prefix", "mid")
    writer_agents = {f"{actor_prefix}_writer_{index}" for index in range(1, 7)}
    for record in exceptions:
        require(isinstance(record, dict), "Invalid input exception record")
        article_id, actor = record.get("id"), record.get("actor")
        require(article_id in items and article_id not in seen, "Input exception has a missing/duplicate article ID")
        seen.add(article_id)
        require(actor in writer_agents, f"{article_id}: exception actor is not a writer")
        require(owners["writer"][article_id] != actor, f"{article_id}: exception was not reassigned to a new writer")
        nonempty(record.get("resolution"), f"{article_id}: input exception resolution missing")
        kind = record.get("kind")
        if kind == "reported_old_summary_seen_before_blind_outline_saved":
            require(record.get("old_initial_created") is False and record.get("draft_created") is False,
                    f"{article_id}: input exception claims a prior initial/draft")
            archived_record = record.get("archived_record")
            require(isinstance(archived_record, str) and not Path(archived_record).is_absolute()
                    and ".." not in Path(archived_record).parts, f"{article_id}: invalid archived input path")
            archived_path = confined(work, archived_record)
            require(archived_path.exists() and file_hash(archived_path) == record.get("archived_sha256"),
                    f"{article_id}: archived exception record hash mismatch")
        elif kind in {"cross_stage_point_mismatch_reassigned", "writer_provenance_reassigned"}:
            archived_records = record.get("archived_records")
            require(isinstance(archived_records, list) and bool(archived_records),
                    f"{article_id}: cross-stage exception must archive original records")
            roles, paths = set(), set()
            for archived in archived_records:
                require(isinstance(archived, dict), f"{article_id}: invalid archived record")
                role, archived_record = archived.get("role"), archived.get("path")
                require(role in {"outline", "initial", "writer"}, f"{article_id}: invalid archived role")
                require(role not in roles, f"{article_id}: duplicate archived role")
                roles.add(role)
                require(isinstance(archived_record, str) and not Path(archived_record).is_absolute()
                        and ".." not in Path(archived_record).parts, f"{article_id}: invalid archived record path")
                require(archived_record not in paths, f"{article_id}: duplicate archived record path")
                paths.add(archived_record)
                archived_path = confined(work, archived_record)
                require(archived_path.exists() and file_hash(archived_path) == archived.get("sha256"),
                        f"{article_id}: archived record file/hash mismatch")
                archived_value = read_json(archived_path)
                require(archived_value == archived.get("record"), f"{article_id}: archived record copy mismatch")
                archived_actor = archived_value.get("writer") if role == "writer" else archived_value.get("actor")
                require(archived_value.get("id") == article_id and archived_actor == actor,
                        f"{article_id}: archived record identity mismatch")
            require({"outline", "initial", "writer"} <= roles,
                    f"{article_id}: cross-stage exception must retain outline, initial, and writer originals")
        else:
            require(False, f"{article_id}: unknown input exception kind")
        require(not (Path(work) / "outlines" / actor / (article_id + ".json")).exists(),
                f"{article_id}: archived untrusted outline was restored as blind evidence")
    return exceptions


def _validate_reviewer_reassignments(work, manifest, owners):
    records = manifest.get("reviewer_reassignments", [])
    require(isinstance(records, list), "Invalid reviewer reassignment records")
    items = {item["id"]: item for item in manifest["articles"]}
    seen = set()
    for record in records:
        require(isinstance(record, dict), "Invalid reviewer reassignment record")
        article_id = record.get("id")
        item = items.get(article_id)
        require(item is not None and article_id not in seen and item.get("review_origin") == "sampling_reuse_problem",
                f"{article_id}: invalid reviewer reassignment scope")
        seen.add(article_id)
        old, new = record.get("from"), record.get("to")
        require(old != new and new == owners["reviewer"].get(article_id)
                and old in set(manifest["assignments"]["reviewer"])
                and record.get("confirmed_unread") is True,
                f"{article_id}: invalid reassigned reviewer identity")
        nonempty(record.get("reason"), f"{article_id}: reviewer reassignment reason missing")
        for path_key, hash_key in (("archived_outline_path", "archived_outline_sha256"),
                                   ("instance_outline_path", "instance_outline_sha256"),
                                   ("instance_initial_path", "instance_initial_sha256")):
            path = record.get(path_key)
            require(isinstance(path, str) and not Path(path).is_absolute() and ".." not in Path(path).parts,
                    f"{article_id}: invalid reviewer reassignment path")
            file = confined(work, path)
            require(file.exists() and file_hash(file) == record.get(hash_key),
                    f"{article_id}: reviewer reassignment record changed")
        archived = read_json(confined(work, record["archived_outline_path"]))
        require(archived.get("id") == article_id and archived.get("actor") == old
                and archived.get("body_sha256") == item["body_sha256"]
                and archived == record.get("archived_outline_record"),
                f"{article_id}: archived reviewer outline identity mismatch")
        instance_outline = read_json(confined(work, record["instance_outline_path"]))
        instance_initial = read_json(confined(work, record["instance_initial_path"]))
        for instance in (instance_outline, instance_initial):
            require(instance.get("id") == article_id and instance.get("body_sha256") == item["body_sha256"]
                    and all(instance.get(key) == value for key, value in model_config(manifest).items()),
                    f"{article_id}: reassigned reviewer instance input mismatch")
            validate_reading(instance, item)
        require(instance_initial.get("old_summary_sha256") == item["old_summary_sha256"]
                and instance_initial.get("outline_sha256") == record["instance_outline_sha256"]
                and instance_initial.get("actor") == instance_outline.get("actor") == new
                and instance_initial.get("verdict") in {"adequate", "undercovered", "distorted"},
                f"{article_id}: reassigned reviewer initial mismatch")
        prior_verdict = item.get("reused_audit", {}).get("final_verdict")
        if instance_initial["verdict"] != prior_verdict:
            adjudication_path = Path(work) / "adjudications" / (article_id + ".json")
            require(adjudication_path.exists()
                    and file_hash(adjudication_path) == record.get("adjudication_sha256")
                    and read_json(adjudication_path).get("decider") == "Codex主代理",
                    f"{article_id}: reassigned reviewer disagreement needs primary adjudication")
        require(not (Path(work) / "outlines" / old / (article_id + ".json")).exists(),
                f"{article_id}: archived reviewer outline still assigned")
    return records


def _validate_checkpoint_indices(work, manifest, owners, format_hash_aliases):
    root = Path(work)
    index_root = root / "checkpoints/index"
    if not index_root.exists():
        return
    archived = {}
    for exception in manifest.get("input_exceptions", []):
        actor, article_id = exception.get("actor"), exception.get("id")
        if exception.get("kind") == "reported_old_summary_seen_before_blind_outline_saved":
            archived[(actor, "outline", article_id)] = {"path": exception["archived_record"],
                                                          "sha256": exception["archived_sha256"]}
        elif exception.get("kind") in {"cross_stage_point_mismatch_reassigned", "writer_provenance_reassigned"}:
            for record in exception.get("archived_records", []):
                archived[(actor, record.get("role"), article_id)] = {"path": record.get("path"),
                                                                      "sha256": record.get("sha256")}
    for record in manifest.get("reviewer_reassignments", []):
        archived[(record["from"], "outline", record["id"])] = {
            "path": record["archived_outline_path"], "sha256": record["archived_outline_sha256"]}

    def expected_path(actor, role, article_id):
        if role == "outline":
            if actor not in {owners["writer"].get(article_id), owners["reviewer"].get(article_id)}:
                require((actor, role, article_id) in archived, f"{article_id}: checkpoint outline actor is not assigned")
            return f"outlines/{actor}/{article_id}.json"
        if role == "initial":
            require(actor == owners["writer"].get(article_id) or (actor, role, article_id) in archived,
                    f"{article_id}: checkpoint initial actor is not assigned")
            return f"initial/{article_id}.json"
        if role == "reviewer-initial":
            require(actor == owners["reviewer"].get(article_id), f"{article_id}: checkpoint reviewer-initial actor is not assigned")
            return f"reviewer-initial/{article_id}.json"
        if role in {"transfer-outline", "transfer-initial"}:
            transfer = manifest.get("final_reviewer_transfers", {}).get(article_id, {})
            require(transfer.get("to") == actor, f"{article_id}: transferred checkpoint actor is not assigned")
            suffix = "outline.json" if role == "transfer-outline" else "initial.json"
            return f"final-review-transfers/{article_id}/{actor}/{suffix}"
        if role == "writer":
            require(actor == owners["writer"].get(article_id) or (actor, role, article_id) in archived,
                    f"{article_id}: checkpoint writer actor is not assigned")
            return f"writers/{article_id}.json"
        if role == "reviewer":
            transfer = manifest.get("final_reviewer_transfers", {}).get(article_id, {})
            require(actor == owners["reviewer"].get(article_id)
                    or (transfer.get("from") == owners["reviewer"].get(article_id) and actor == transfer.get("to")),
                    f"{article_id}: checkpoint reviewer actor is not assigned")
            return f"reviewers/{article_id}.json"
        if role == "adjudication":
            require(actor == "Codex主代理", f"{article_id}: checkpoint adjudication actor is invalid")
            return f"adjudications/{article_id}.json"
        if role == "format-corrections":
            require(actor in set(owners["writer"].values()) | set(owners["reviewer"].values()) | {"Codex主代理"},
                    "Checkpoint format-correction actor is invalid")
            return f"format-corrections/{actor}.json"
        if role == "draft-revisions":
            require(actor in set(owners["writer"].values()) | set(owners["reviewer"].values()),
                    "Checkpoint draft-revision actor is invalid")
            return f"draft-revisions/{actor}.json"
        if role == "primary-acceptance":
            require(actor == "Codex主代理" and article_id == "run", "Checkpoint primary-acceptance identity is invalid")
            return "primary-acceptance.json"
        require(False, f"Unknown checkpoint role: {role}")

    for index_path in sorted(index_root.rglob("*.json")):
        parts = index_path.relative_to(index_root).parts
        require(len(parts) == 3 and parts[2].endswith(".json"), f"Invalid checkpoint index path: {index_path}")
        actor, role, filename = parts
        article_id = filename[:-5]
        index = read_json(index_path)
        require(index.get("schema_version") == 1 and index.get("actor") == actor
                and index.get("role") == role and index.get("id") == article_id,
                f"Checkpoint index identity mismatch: {index_path.relative_to(root)}")
        record_path = index.get("record_path")
        require(isinstance(record_path, str) and not Path(record_path).is_absolute()
                and ".." not in Path(record_path).parts, f"Invalid checkpoint record path: {index_path}")
        require(record_path == expected_path(actor, role, article_id),
                f"Checkpoint record path mismatch: {index_path.relative_to(root)}")
        nonempty(index.get("first_observed_at"), f"Checkpoint first timestamp missing: {index_path.relative_to(root)}")
        require(index.get("ordering_note") == CHECKPOINT_ORDER_NOTE,
                f"Checkpoint ordering caveat missing: {index_path.relative_to(root)}")
        versions = index.get("versions")
        require(isinstance(versions, list) and bool(versions), f"Checkpoint versions missing: {index_path.relative_to(root)}")
        require(index.get("first_sha256") == versions[0].get("sha256")
                and index.get("first_observed_at") == versions[0].get("observed_at"),
                f"Checkpoint first version changed: {index_path.relative_to(root)}")
        seen_hashes = set()
        for version in versions:
            sha256, snapshot_path = version.get("sha256"), version.get("snapshot_path")
            require(isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256),
                    f"Invalid checkpoint hash: {index_path.relative_to(root)}")
            nonempty(version.get("observed_at"), f"Checkpoint version timestamp missing: {index_path.relative_to(root)}")
            expected_snapshot = f"checkpoints/objects/{sha256}.json"
            require(snapshot_path == expected_snapshot, f"Checkpoint object path mismatch: {index_path.relative_to(root)}")
            snapshot = confined(root, snapshot_path)
            require(snapshot.exists() and file_hash(snapshot) == sha256,
                    f"Checkpoint object missing/changed: {index_path.relative_to(root)}")
            seen_hashes.add(sha256)
        exception = archived.get((actor, role, article_id))
        if exception is not None:
            archive_path = confined(root, exception["path"])
            require(archive_path.exists() and file_hash(archive_path) == exception["sha256"]
                    and exception["sha256"] in seen_hashes,
                    f"Archived exception does not match checkpoint: {article_id}/{role}")
            continue
        target = confined(root, record_path)
        if not target.exists():
            require(role not in {"outline", "initial", "reviewer-initial", "transfer-outline", "transfer-initial"},
                    f"Frozen checkpoint record was deleted: {record_path}")
            continue
        current_hash = file_hash(target)
        if role in {"outline", "initial", "reviewer-initial", "transfer-outline", "transfer-initial"} and current_hash != index["first_sha256"]:
            aliases = format_hash_aliases.get(record_path, set())
            require(index["first_sha256"] in aliases and current_hash in aliases,
                    f"Frozen checkpoint changed without a valid format chain: {record_path}")


def _allowed_file_hashes(work, path, aliases):
    relative = Path(path).resolve().relative_to(Path(work).resolve()).as_posix()
    return set(aliases.get(relative, ())) | {file_hash(path)}


def _audit_file(root, value):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _audit_label(root, path):
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _read_reuse_audit(root, value):
    if value is None:
        return None
    path = _audit_file(root, value)
    raw = path.read_bytes()
    report = json.loads(raw)
    require(isinstance(report, dict) and isinstance(report.get("entries"), list), "Reuse audit must contain an entries list")
    require(isinstance(report.get("run_id"), str) and bool(report["run_id"]), "Reuse audit must contain a run_id")
    entries = {}
    for entry in report["entries"]:
        require(isinstance(entry, dict) and isinstance(entry.get("id"), str), "Reuse audit contains an invalid entry")
        require(entry["id"] not in entries, f"Reuse audit contains duplicate entry {entry['id']}")
        entries[entry["id"]] = entry
    return {"path": path, "file": _audit_label(root, path), "sha256": hashlib.sha256(raw).hexdigest(),
            "run_id": report["run_id"], "entries": entries}


def _prior_entry_fingerprint_errors(entry, row, body_sha256):
    if not isinstance(entry, dict):
        return ["prior entry is not an object"]
    source = entry.get("source")
    if not isinstance(source, dict):
        return ["prior entry has no source fingerprints"]
    required_entry = {"id": row["id"], "title": row["title"], "url": row["url"],
                      "summary": row["summary"], "summary_sha256": digest(row["summary"])}
    required_source = {"source_sha256": row["source_sha256"], "content_sha256": row["content_sha256"],
                       "body_sha256": body_sha256}
    errors = [key for key, value in required_entry.items() if key not in entry or entry.get(key) != value]
    errors.extend(key for key, value in required_source.items() if key not in source or source.get(key) != value)
    # Identity fields that older public reports did not export are checked when present.
    for key in ("archive_url",):
        if key in entry and entry.get(key) != row.get(key):
            errors.append(key)
    for key in ("source_path", "read_chars"):
        if key in source and source.get(key) != row.get(key):
            errors.append(key)
    if "source_method" in source and source.get("source_method") not in {"verified_snapshot", "exact_raw_snapshot_recovery"}:
        errors.append("source_method")
    if entry.get("final_verdict") not in {"adequate", "undercovered", "distorted"}:
        errors.append("final_verdict")
    return errors


def _prepare_legacy(root, source_dir, source_manifest, work, run_id, revision_date, minimum, chunk_chars=6000, *,
                    model=MODEL, reasoning_effort=EFFORT):
    config = model_config({"model": model, "reasoning_effort": reasoning_effort})
    work = Path(work)
    require(0 < chunk_chars <= 6000, "chunk_chars must be between 1 and 6000")
    require(minimum > 0, "min_body_chars must be positive")
    require(bool(run_id.strip()), "run_id must not be empty")
    date.fromisoformat(revision_date)
    require(not work.exists() or (work.is_dir() and not any(work.iterdir())), "Fresh empty work directory required")
    catalog = read_json(root / "data/catalog.json")
    source_data = read_json(source_manifest)
    baseline = {row["id"]: row for row in source_data["articles"]} if "articles" in source_data else source_data
    selected = sorted((row for row in catalog["articles"] if row["read_chars"] >= minimum), key=lambda row: (-row["read_chars"], row["id"]))
    require(selected, "No articles match the requested scope")
    manifest = {"schema_version": 1, "run_id": run_id, "revision_date": revision_date,
                "min_body_chars": minimum, "chunk_chars": chunk_chars, **config, "articles": []}
    prepared = []
    for row in selected:
        try:
            body, _ = summary_sources.source_body(row, source_dir, baseline, None)
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise ValueError(f"{row['id']}: {error}") from error
        item = {key: row[key] for key in ("id", "title", "url", "archive_url", "source_path", "source_sha256", "content_sha256", "read_chars", "summary")}
        item.update(old_summary_sha256=digest(row["summary"]), body_sha256=digest(body),
                    initial_provenance=provenance(row), chunks=[], **config)
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


def prepare(root, source_dir, source_manifest, work, run_id, revision_date, minimum, chunk_chars=6000, *,
            max_body_chars=None, recovery_dir=None, reuse_audit=None, actor_prefix="long", blind_review=False,
            model=MODEL, reasoning_effort=EFFORT, require_bound_primary_review=False):
    """Prepare a fresh review run, retaining the original positional legacy interface."""
    config = model_config({"model": model, "reasoning_effort": reasoning_effort})
    require(not require_bound_primary_review or blind_review, "Bound primary review requires blind_review")
    extended = (max_body_chars is not None or recovery_dir is not None or reuse_audit is not None
                or actor_prefix != "long" or blind_review)
    if not extended:
        return _prepare_legacy(root, source_dir, source_manifest, work, run_id, revision_date, minimum, chunk_chars,
                               **config)

    root, work = Path(root), Path(work)
    require(0 < chunk_chars <= 6000, "chunk_chars must be between 1 and 6000")
    require(minimum > 0, "min_body_chars must be positive")
    require(max_body_chars is None or max_body_chars > minimum, "max_body_chars must exceed min_body_chars")
    require(bool(run_id.strip()), "run_id must not be empty")
    require(isinstance(actor_prefix, str) and re.fullmatch(r"[A-Za-z0-9-]+", actor_prefix) is not None, "Invalid actor_prefix")
    date.fromisoformat(revision_date)
    require(not work.exists() or (work.is_dir() and not any(work.iterdir())), "Fresh empty work directory required")
    catalog = read_json(root / "data/catalog.json")
    source_data = read_json(source_manifest)
    baseline = ({row["id"]: row for row in source_data["articles"]}
                if isinstance(source_data, dict) and "articles" in source_data else source_data)
    require(isinstance(baseline, dict), "Invalid source manifest")
    selected = sorted((row for row in catalog["articles"] if row["read_chars"] >= minimum
                       and (max_body_chars is None or row["read_chars"] < max_body_chars)),
                      key=lambda row: (-row["read_chars"], row["id"]))
    require(selected, "No articles match the requested scope")
    reuse = _read_reuse_audit(root, reuse_audit)
    manifest = {"schema_version": 1, "run_id": run_id, "revision_date": revision_date,
                "min_body_chars": minimum, "max_body_chars": max_body_chars, "chunk_chars": chunk_chars,
                **config, "actor_prefix": actor_prefix,
                "blind_review": bool(blind_review), "articles": [], "preserve_files": preserve_files(root)}
    if require_bound_primary_review:
        manifest["require_bound_primary_review"] = True
    if reuse is not None:
        manifest["reused_audit"] = {"file": reuse["file"], "sha256": reuse["sha256"], "run_id": reuse["run_id"]}
        # This path is private run state. Public reports use the root-relative label above.
        manifest["reused_audit_path"] = str(reuse["path"])
    invalidations, prepared = [], []
    for row in selected:
        try:
            body, source_method = summary_sources.source_body(row, source_dir, baseline, recovery_dir)
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise ValueError(f"{row['id']}: source verification failed: {error}") from error
        body_sha256 = digest(body)
        item = {key: row[key] for key in ("id", "title", "url", "archive_url", "source_path", "source_sha256",
                                          "content_sha256", "read_chars", "summary")}
        item.update(old_summary_sha256=digest(row["summary"]), body_sha256=body_sha256,
                    initial_provenance=provenance(row), chunks=[], source_method=source_method, review_origin="fresh", **config)
        prior_entry = reuse["entries"].get(row["id"]) if reuse else None
        if reuse is not None and prior_entry is not None:
            errors = _prior_entry_fingerprint_errors(prior_entry, row, body_sha256)
            if not errors:
                final_verdict = prior_entry["final_verdict"]
                item["review_origin"] = "sampling_reuse_adequate" if final_verdict == "adequate" else "sampling_reuse_problem"
                item["reused_audit"] = {"file": reuse["file"], "report_sha256": reuse["sha256"],
                                        "run_id": reuse["run_id"], "entry_sha256": canonical_hash(prior_entry),
                                        "final_verdict": final_verdict}
                item["prior_review_path"] = f"inputs/{row['id']}/prior-review.json"
            else:
                invalidations.append({"id": row["id"], "prior_run_id": reuse["run_id"],
                                      "audit_file": reuse["file"], "reasons": errors})
        chunks = []
        for index, offset in enumerate(range(0, len(body), chunk_chars)):
            relative = f"inputs/{row['id']}/chunk-{index:02}.txt"
            part = body[offset:offset + chunk_chars]
            item["chunks"].append({"index": index, "start": offset, "end": offset + len(part), "path": relative})
            chunks.append((relative, part))
        manifest["articles"].append(item)
        prepared.append((item, chunks, prior_entry if item.get("reused_audit") else None))
    if invalidations:
        manifest["reuse_invalidations"] = invalidations
    direct_retains = {item["id"] for item in manifest["articles"] if item["review_origin"] == "sampling_reuse_adequate"}
    manifest["assignments"] = assign_articles(manifest["articles"], actor_prefix, direct_retains)
    # Complete all source checks before creating any work files.
    for item, chunks, prior_entry in prepared:
        for relative, part in chunks:
            path = confined(work, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(part, encoding="utf-8")
        write_json(work / "inputs" / item["id"] / "article.json", item)
        source_item = {key: item[key] for key in ("id", "title", "url", "archive_url", "source_path", "source_sha256",
                                                  "content_sha256", "read_chars", "body_sha256", "chunks", "model", "reasoning_effort")}
        write_json(work / "inputs" / item["id"] / "source.json", source_item)
        if prior_entry is not None:
            write_json(work / item["prior_review_path"], prior_entry)
    write_json(work / "manifest.json", manifest)
    for role, groups in manifest["assignments"].items():
        for agent, assignment in groups.items():
            index = agent.rsplit("_", 1)[1]
            write_json(work / f"{role}-{index}-assignment.json", assignment)
    return {"run_id": run_id, "articles": len(selected), "body_characters": sum(row["read_chars"] for row in selected)}


def validate_body(work, item, require_source=False):
    source_only = "summary" not in item and not require_source
    if source_only:
        require(read_json(work / "inputs" / item["id"] / "source.json") == item,
                f"{item['id']}: source input differs from supplied source")
    else:
        require(read_json(work / "inputs" / item["id"] / "article.json") == item, f"{item['id']}: article input differs from manifest")
        require(digest(item["summary"]) == item["old_summary_sha256"], f"{item['id']}: old summary hash mismatch")
    if require_source:
        source_path = work / "inputs" / item["id"] / "source.json"
        source = read_json(source_path)
        expected_source = {key: item[key] for key in ("id", "title", "url", "archive_url", "source_path", "source_sha256",
                                                        "content_sha256", "read_chars", "body_sha256", "chunks")}
        expected_source.update({key: item[key] for key in ("model", "reasoning_effort") if key in item})
        require(source == expected_source, f"{item['id']}: source input differs from manifest")
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


def validate_outline(outline, actor, item, body):
    require(outline.get("id") == item["id"], f"{item['id']}: outline ID mismatch")
    require(outline.get("actor") == actor, f"{item['id']}: unexpected outline actor")
    validate_reading(outline, item)
    nonempty(outline.get("genre"), f"{item['id']}: outline genre missing")
    points = outline.get("major_points")
    require(isinstance(points, list) and bool(points), f"{item['id']}: outline has no major points")
    keys = []
    for point in points:
        nonempty(point.get("key"), f"{item['id']}: outline point key missing")
        nonempty(point.get("point"), f"{item['id']}: outline point missing")
        validate_blind_spans(point, item, body)
        for span in point["source_spans"]:
            require(sum(char.isalnum() for char in body[span["start"]:span["end"]]) >= 8,
                    f"{item['id']}: outline span has no substantive body text")
        keys.append(point["key"])
    require(len(keys) == len(set(keys)), f"{item['id']}: duplicate outline point keys")


def _assessment_verdict(points, distortions):
    coverage = {point["old_coverage"] for point in points}
    if "distorted" in coverage or distortions:
        return "distorted"
    return "undercovered" if coverage & {"partial", "missing"} else "adequate"


def validate_initial(initial, outline, outline_path, actor, item, body, allowed_outline_hashes=()):
    require(item.get("review_origin", "fresh") == "fresh",
            f"{item['id']}: new initial old-summary judgments are only valid for fresh items")
    validate_reading(initial, item)
    require(initial.get("actor") == actor, f"{item['id']}: unexpected initial assessor")
    require(initial.get("old_summary_sha256") == item["old_summary_sha256"], f"{item['id']}: initial used stale summary")
    require(initial.get("outline_sha256") in set(allowed_outline_hashes) | {file_hash(outline_path)},
            f"{item['id']}: initial used a stale outline")
    nonempty(initial.get("rationale"), f"{item['id']}: missing initial rationale")
    points = initial.get("major_points")
    original = outline["major_points"]
    require(isinstance(points, list) and len(points) == len(original), f"{item['id']}: initial points differ from outline")
    for point, blind in zip(points, original):
        require({key: point.get(key) for key in ("key", "point", "source_spans")} == blind,
                f"{item['id']}: initial changed a blind point")
        require(point.get("old_coverage") in {"covered", "partial", "missing", "distorted"},
                f"{item['id']}: invalid initial old_coverage")
        if point["old_coverage"] == "missing":
            require(point.get("old_evidence") is None, f"{item['id']}: missing point must have null old_evidence")
        else:
            validate_evidence(point.get("old_evidence"), item["summary"], item)
    distortions = initial.get("distortions", [])
    require(isinstance(distortions, list), f"{item['id']}: invalid initial distortions")
    for distortion in distortions:
        validate_evidence(distortion.get("claim"), item["summary"], item)
        validate_blind_spans(distortion, item, body)
        nonempty(distortion.get("reason"), f"{item['id']}: unexplained initial distortion")
    expected_verdict = _assessment_verdict(points, distortions)
    require(initial.get("verdict") == expected_verdict, f"{item['id']}: initial verdict conflicts with point evidence")


def _validate_reuse_inputs(work, item, manifest, root):
    reused = item.get("reused_audit")
    require(isinstance(reused, dict), f"{item['id']}: missing reused audit reference")
    global_reuse = manifest.get("reused_audit")
    require(isinstance(global_reuse, dict), "Manifest is missing reused audit reference")
    for key in ("file", "run_id"):
        require(reused.get(key) == global_reuse.get(key), f"{item['id']}: reused audit identity changed")
    require(reused.get("report_sha256") == global_reuse.get("sha256"), f"{item['id']}: reused audit hash changed")
    audit_path = Path(manifest.get("reused_audit_path") or (root / global_reuse["file"]))
    require(file_hash(audit_path) == global_reuse["sha256"], "Pinned reuse audit changed")
    prior_path = confined(work, item["prior_review_path"])
    entry = read_json(prior_path)
    require(canonical_hash(entry) == reused.get("entry_sha256"), f"{item['id']}: prior audit entry changed")
    require(entry.get("id") == item["id"] and entry.get("final_verdict") == reused.get("final_verdict"),
            f"{item['id']}: prior audit decision changed")
    errors = _prior_entry_fingerprint_errors(entry, item, item["body_sha256"])
    require(not errors, f"{item['id']}: prior audit fingerprints mismatch: {', '.join(errors)}")
    return entry


def _validate_request(request, item, writer_initial_path, reviewer_initial_path,
                      writer_initial_hashes=(), reviewer_initial_hashes=()):
    require(request.get("id") == item["id"], f"{item['id']}: request ID mismatch")
    require(request.get("writer_initial_sha256") in set(writer_initial_hashes) | {file_hash(writer_initial_path)}
            and request.get("reviewer_initial_sha256") in set(reviewer_initial_hashes) | {file_hash(reviewer_initial_path)},
            f"{item['id']}: request refers to stale initial assessments")
    nonempty(request.get("issue"), f"{item['id']}: request issue missing")
    nonempty(request.get("rationale"), f"{item['id']}: request rationale missing")
    validate_spans({"source_spans": request.get("source_spans")}, item)


def _validate_fresh_adjudication(adjudication, item, body, writer_initial, reviewer_initial,
                                 writer_initial_path, reviewer_initial_path, writer_outline, reviewer_outline,
                                 writer_initial_hashes=(), reviewer_initial_hashes=()):
    require(adjudication.get("id") == item["id"] and adjudication.get("decider") == "Codex主代理",
            f"{item['id']}: invalid primary adjudication identity")
    require(adjudication.get("writer_initial_sha256") in set(writer_initial_hashes) | {file_hash(writer_initial_path)}
            and adjudication.get("reviewer_initial_sha256") in set(reviewer_initial_hashes) | {file_hash(reviewer_initial_path)},
            f"{item['id']}: adjudication refers to stale initial assessments")
    require(adjudication.get("final_old_verdict") in {"adequate", "undercovered", "distorted"},
            f"{item['id']}: invalid adjudicated old verdict")
    nonempty(adjudication.get("rationale"), f"{item['id']}: adjudication rationale missing")
    validate_blind_spans({"source_spans": adjudication.get("source_spans")}, item, body)
    excluded_by_role = {}
    for field, role, outline in (("excluded_writer_points", "writer", writer_outline),
                                 ("excluded_reviewer_points", "reviewer", reviewer_outline)):
        excluded = adjudication.get(field)
        require(isinstance(excluded, list) and all(isinstance(key, str) for key in excluded),
                f"{item['id']}: invalid {field}")
        keys = {point["key"] for point in outline["major_points"]}
        require(set(excluded) <= keys, f"{item['id']}: {field} references an unknown blind point")
        excluded_by_role[role] = set(excluded)
    corrections = adjudication.get("writer_coverage_corrections", [])
    require(isinstance(corrections, list), f"{item['id']}: invalid writer_coverage_corrections")
    writer_initial_points = {point["key"]: point for point in writer_initial["major_points"]}
    writer_outline_keys = {point["key"] for point in writer_outline["major_points"]}
    corrections_by_key = {}
    for correction in corrections:
        require(isinstance(correction, dict), f"{item['id']}: invalid writer coverage correction")
        key = correction.get("key")
        require(isinstance(key, str) and key in writer_outline_keys, f"{item['id']}: coverage correction key is not in writer blind outline")
        require(key not in excluded_by_role["writer"], f"{item['id']}: excluded writer point cannot receive a coverage correction")
        require(key not in corrections_by_key, f"{item['id']}: duplicate writer coverage correction")
        require(correction.get("old_coverage") in {"covered", "partial", "missing", "distorted"},
                f"{item['id']}: invalid corrected old_coverage")
        require(key not in excluded_by_role["writer"], f"{item['id']}: excluded writer point cannot receive a coverage correction")
        nonempty(correction.get("reason"), f"{item['id']}: writer coverage correction reason missing")
        if correction["old_coverage"] == "missing":
            require("old_summary_evidence" in correction and correction["old_summary_evidence"] is None,
                    f"{item['id']}: missing corrected point must have null old_summary_evidence")
        else:
            validate_evidence(correction.get("old_summary_evidence"), item["summary"], item)
        corrections_by_key[key] = correction
    return adjudication, corrections_by_key


def _validate_reuse_adjudication(adjudication, item, body, prior_entry, require_quote=True):
    require(adjudication.get("id") == item["id"] and adjudication.get("decider") == "Codex主代理",
            f"{item['id']}: invalid primary adjudication identity")
    require(adjudication.get("reused_audit_entry_sha256") == canonical_hash(prior_entry),
            f"{item['id']}: reuse adjudication refers to a stale prior entry")
    require(adjudication.get("final_old_verdict") in {"adequate", "undercovered", "distorted"},
            f"{item['id']}: invalid adjudicated old verdict")
    nonempty(adjudication.get("rationale"), f"{item['id']}: adjudication rationale missing")
    if require_quote:
        validate_blind_spans({"source_spans": adjudication.get("source_spans")}, item, body)
    else:
        validate_spans({"source_spans": adjudication.get("source_spans")}, item)
    return adjudication


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
    require(all(record.get(key) == value for key, value in model_config(item).items()),
            f"{item['id']}: incorrect model provenance")


def validate_spans(point, item):
    spans = point.get("source_spans")
    require(isinstance(spans, list) and bool(spans), f"{item['id']}: missing source spans")
    for span in spans:
        start, end = span.get("start"), span.get("end")
        require(type(start) is int and type(end) is int and 0 <= start < end <= item["read_chars"], f"{item['id']}: source span outside body")
        require(end - start >= 8, f"{item['id']}: source span is too short to support a major point")


def validate_blind_spans(point, item, body):
    """Strict quote-anchored coordinates used only by new blind-review artifacts."""
    validate_spans(point, item)
    for span in point["source_spans"]:
        quote = span.get("quote")
        nonempty(quote, f"{item['id']}: blind source span is missing quote")
        require(8 <= len(quote) <= 160, f"{item['id']}: blind span quote must be 8–160 characters")
        require(sum(char.isalnum() for char in quote) >= 8, f"{item['id']}: blind span quote is not substantive")
        require(quote in body[span["start"]:span["end"]], f"{item['id']}: blind span quote is outside its declared interval")


def validate_evidence(text, summary, item):
    nonempty(text, f"{item['id']}: missing summary evidence")
    require(sum(char.isalnum() for char in text) >= 8, f"{item['id']}: summary evidence is too short to be substantive")
    require(text in summary, f"{item['id']}: evidence is absent from reviewed summary: {text[:70]}")


def validate_writer(writer, item, excluded_keys=()):
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
    excluded_keys = set(excluded_keys)
    require(decision != "retain" or all(p["old_coverage"] == "covered" for p in points if p["key"] not in excluded_keys),
            f"{item['id']}: retained summary has uncovered major points")


def validate_reviewer(review, writer, item, required_major_keys=None):
    validate_reading(review, item)
    nonempty(review.get("reviewer"), f"{item['id']}: missing reviewer identity")
    require(review["reviewer"] != writer["writer"], f"{item['id']}: reviewer is not independent")
    require(review.get("summary_sha256") == digest(writer["summary"]), f"{item['id']}: review is for a stale draft")
    require(review.get("verdict") == "pass", f"{item['id']}: review has not passed")
    checks = review.get("major_point_checks")
    require(isinstance(checks, list), f"{item['id']}: missing major point checks")
    expected = set(required_major_keys) if required_major_keys is not None else {p["key"] for p in writer["major_points"]}
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


def validate_blind_writer(writer, outline, outline_path, item, body, initial=None, initial_path=None, excluded_keys=(), require_no_initial=False,
                          allowed_outline_hashes=(), allowed_initial_hashes=(), coverage_corrections=None):
    validate_reading(writer, item)
    require(writer.get("blind_outline_sha256") in set(allowed_outline_hashes) | {file_hash(outline_path)},
            f"{item['id']}: writer used a stale blind outline")
    excluded = set(excluded_keys)
    outline_points = {point["key"]: point for point in outline["major_points"]}
    writer_points = {point["key"]: point for point in writer["major_points"]}
    for key, original in outline_points.items():
        if key in excluded:
            continue
        require(key in writer_points, f"{item['id']}: writer omitted blind major point {key}")
    for key, point in writer_points.items():
        validate_blind_spans(point, item, body)
        if key in outline_points:
            original = outline_points[key]
            require({field: point.get(field) for field in ("key", "point", "source_spans")} == original,
                    f"{item['id']}: writer changed blind major point {key}")
    if initial is not None:
        require(initial_path is not None, f"{item['id']}: initial assessment path missing")
        require(writer.get("initial_assessment_sha256") in set(allowed_initial_hashes) | {file_hash(initial_path)},
                f"{item['id']}: writer used a stale initial assessment")
        old_points = {point["key"]: point for point in initial["major_points"]}
        coverage_corrections = coverage_corrections or {}
        for key, point in writer_points.items():
            if key not in old_points or key in excluded:
                continue
            correction = coverage_corrections.get(key)
            if correction is None:
                require(point.get("old_coverage") == old_points[key]["old_coverage"],
                        f"{item['id']}: writer changed initial old coverage for {key}")
            else:
                require(point.get("old_coverage") == correction["old_coverage"],
                        f"{item['id']}: writer coverage differs from root correction for {key}")
    else:
        if require_no_initial:
            require("initial_assessment_sha256" not in writer, f"{item['id']}: reused prior judgment must not be fabricated as a new initial assessment")


def validate_blind_reviewer(review, outline, outline_path, item, body, initial=None, initial_path=None, excluded_keys=(), require_no_initial=False,
                            allowed_outline_hashes=(), allowed_initial_hashes=()):
    validate_reading(review, item)
    require(review.get("blind_outline_sha256") in set(allowed_outline_hashes) | {file_hash(outline_path)},
            f"{item['id']}: reviewer used a stale blind outline")
    excluded = set(excluded_keys)
    outline_points = {point["key"]: point for point in outline["major_points"]}
    independent = review.get("independent_major_points")
    require(isinstance(independent, list) and bool(independent), f"{item['id']}: missing independent blind points")
    independent_by_key = {}
    for point in independent:
        blind_key = point.get("blind_key")
        if blind_key is None and point.get("key") in outline_points:
            blind_key = point["key"]
        if blind_key is not None:
            nonempty(blind_key, f"{item['id']}: independent point blind_key invalid")
            require(blind_key in outline_points, f"{item['id']}: independent point maps to an unknown blind_key")
            require(blind_key not in independent_by_key, f"{item['id']}: duplicate independent blind_key")
            independent_by_key[blind_key] = point
        validate_blind_spans(point, item, body)
    for key, original in outline_points.items():
        if key not in excluded:
            exact = any(point.get("point") == original["point"] and point.get("source_spans") == original["source_spans"]
                        for point in independent)
            require(key in independent_by_key or exact, f"{item['id']}: reviewer omitted blind major point {key}")
        if key in independent_by_key:
            point = independent_by_key[key]
            nonempty(point.get("point"), f"{item['id']}: independent blind point is empty")
            require(point.get("source_spans") == outline_points[key]["source_spans"],
                    f"{item['id']}: reviewer blind point no longer uses its own source anchors")
    if initial is not None:
        require(initial_path is not None, f"{item['id']}: reviewer initial assessment path missing")
        require(review.get("initial_assessment_sha256") in set(allowed_initial_hashes) | {file_hash(initial_path)},
                f"{item['id']}: reviewer used a stale initial assessment")
    else:
        if require_no_initial:
            require("initial_assessment_sha256" not in review, f"{item['id']}: reused prior judgment must not be fabricated as a new initial assessment")


def validate_final_reviewer_transfer(work, transfer, owners, item, body, writer, review,
                                     prior_outline, prior_outline_path, prior_initial, prior_initial_path,
                                     excluded_prior=(), expected_old_verdict=None):
    """Validate a genuinely new blind reader while retaining the original old-draft judgment."""
    article_id = item["id"]
    original = owners["reviewer"][article_id]
    replacement = transfer.get("to")
    require(transfer.get("id") == article_id and transfer.get("from") == original,
            f"{article_id}: invalid final reviewer transfer identity")
    reviewer_prefix = original.rsplit("_", 1)[0]
    require(replacement in {f"{reviewer_prefix}_1", f"{reviewer_prefix}_2"} and replacement != original
            and replacement != owners["writer"][article_id] and transfer.get("confirmed_unread") is True,
            f"{article_id}: final reviewer transfer is not independent")
    nonempty(transfer.get("reason"), f"{article_id}: reviewer transfer reason missing")
    previous = transfer.get("previous_review")
    require(isinstance(previous, dict) and previous.get("path") ==
            f"exceptions/{article_id}/previous-review-before-transfer.json",
            f"{article_id}: transferred reviewer history is missing")
    previous_path = confined(work, previous["path"])
    require(isinstance(previous.get("record"), dict) and previous_path.exists()
            and file_hash(previous_path) == previous.get("sha256")
            and read_json(previous_path) == previous.get("record")
            and previous["record"].get("id") == article_id
            and previous["record"].get("reviewer") == original,
            f"{article_id}: transferred reviewer history changed")
    base = f"final-review-transfers/{article_id}/{replacement}"
    require(transfer.get("outline_path") == f"{base}/outline.json"
            and transfer.get("initial_path") == f"{base}/initial.json",
            f"{article_id}: invalid reviewer transfer evidence paths")
    outline_path = confined(work, transfer["outline_path"])
    initial_path = confined(work, transfer["initial_path"])
    require(outline_path.exists() and initial_path.exists()
            and file_hash(outline_path) == transfer.get("outline_sha256")
            and file_hash(initial_path) == transfer.get("initial_sha256"),
            f"{article_id}: reviewer transfer evidence missing or changed")
    for role, expected_hash in (("transfer-outline", file_hash(outline_path)),
                                ("transfer-initial", file_hash(initial_path))):
        index_path = Path(work) / "checkpoints" / "index" / replacement / role / (article_id + ".json")
        require(index_path.exists() and read_json(index_path).get("first_sha256") == expected_hash,
                f"{article_id}: transferred reviewer stage lacks its original checkpoint")
    outline, initial = read_json(outline_path), read_json(initial_path)
    require(digest(json_text(outline)) == file_hash(outline_path)
            and digest(json_text(initial)) == file_hash(initial_path),
            f"{article_id}: reviewer transfer evidence must use canonical JSON")
    validate_outline(outline, replacement, item, body)
    validate_initial(initial, outline, outline_path, replacement, item, body)
    if initial["verdict"] != expected_old_verdict:
        decision = transfer.get("new_initial_adjudication", {})
        require(decision.get("decider") == "Codex主代理"
                and decision.get("initial_sha256") == file_hash(initial_path)
                and decision.get("final_old_verdict") == expected_old_verdict,
                f"{article_id}: transferred reviewer disagreement is not adjudicated")
        nonempty(decision.get("reason"), f"{article_id}: transfer adjudication reason missing")
        validate_blind_spans({"source_spans": decision.get("source_spans")}, item, body)
    require(review.get("reviewer") == replacement and review.get("transfer_provenance") == {
        "from": original, "to": replacement, "reason": transfer["reason"],
        "outline": outline, "initial": initial, "outline_sha256": file_hash(outline_path),
        "initial_sha256": file_hash(initial_path)}, f"{article_id}: final reviewer provenance mismatch")
    validate_reviewer(review, writer, item)
    validate_blind_reviewer(review, outline, outline_path, item, body, initial, initial_path)
    prior_points = review.get("prior_independent_major_points")
    require(isinstance(prior_points, list) and bool(prior_points),
            f"{article_id}: transferred review omitted original independent points")
    prior_review = dict(review, independent_major_points=prior_points,
                        blind_outline_sha256=file_hash(prior_outline_path),
                        initial_assessment_sha256=file_hash(prior_initial_path))
    validate_reviewer(prior_review, writer, item)
    validate_blind_reviewer(prior_review, prior_outline, prior_outline_path, item, body,
                            prior_initial, prior_initial_path, excluded_prior)
    for evidence_points, blind_outline in ((review["independent_major_points"], outline),
                                           (prior_points, prior_outline)):
        original_points = {point["key"]: point for point in blind_outline["major_points"]}
        for point in evidence_points:
            key = point.get("blind_key") or point.get("key")
            if key in original_points:
                require(point.get("point") == original_points[key]["point"],
                        f"{article_id}: transferred review changed a frozen blind point")


def load_ledger(root):
    path = root / "data/summary-revisions.json"
    return read_json(path) if path.exists() else {"schema_version": 1, "entries": []}


def event_id(item, summary, run_id):
    return digest("\n".join((run_id, item["id"], item["source_sha256"], item["body_sha256"], item["old_summary_sha256"], digest(summary))))


def _check_legacy(root, work, allow_incomplete=False):
    manifest = read_json(work / "manifest.json")
    validate_model_config(manifest)
    date.fromisoformat(manifest["revision_date"])
    if "preserve_files" in manifest:
        verify_preserve_files(root, manifest)
    catalog = read_json(root / "data/catalog.json")
    rows = catalog_rows(catalog)
    items = manifest["articles"]
    ids = [item["id"] for item in items]
    require(bool(ids) and len(ids) == len(set(ids)), "Empty/duplicate review scope")
    maximum = manifest.get("max_body_chars")
    require(set(ids) == {row["id"] for row in rows.values() if row["read_chars"] >= manifest["min_body_chars"]
                         and (maximum is None or row["read_chars"] < maximum)},
            "Catalog review scope changed; prepare a new run")
    direct_ids = {item["id"] for item in items if item.get("review_origin") == "sampling_reuse_adequate"}
    assigned_ids = set(ids) - direct_ids
    prefix = manifest.get("actor_prefix", "long")
    assignments = manifest.get("assignments")
    require(isinstance(assignments, dict) and set(assignments) == {"writer", "reviewer"}, "Missing actor assignments")
    owners = {}
    for role in ("writer", "reviewer"):
        groups = assignments[role]
        require(isinstance(groups, dict) and bool(groups), f"Missing {role} assignments")
        allocated = [article_id for assigned in groups.values() for article_id in assigned]
        require(len(allocated) == len(assigned_ids) and set(allocated) == assigned_ids, f"Missing/duplicate {role} assignments")
        owners[role] = {article_id: agent for agent, assigned in groups.items() for article_id in assigned}
        for agent, assigned in groups.items():
            require(agent.startswith(f"{prefix}_{role}_"), f"Invalid {role} agent identity")
            index = agent.rsplit("_", 1)[1]
            require(read_json(work / f"{role}-{index}-assignment.json") == assigned, f"{agent}: assignment file changed")
    global_reuse = manifest.get("reused_audit")
    if global_reuse is not None:
        audit_path = Path(manifest.get("reused_audit_path") or (root / global_reuse["file"]))
        require(file_hash(audit_path) == global_reuse.get("sha256"), "Pinned reuse audit changed")
    ledger = load_ledger(root)
    for event in ledger["entries"]:
        validate_history_review(event)
    existing_events = {event.get("revision_id"): event for event in ledger["entries"] if event.get("revision_id")}
    public_audit_path = root / "data/summary-reviews.json"
    public_audit = read_json(public_audit_path) if public_audit_path.exists() else None
    archived_runs = {run.get("run_id") for run in (public_audit or {}).get("previous_runs", [])}
    archived_run = manifest["run_id"] in archived_runs
    pairs, pending = [], []
    for item in items:
        body = validate_body(work, item, require_source=("max_body_chars" in manifest or global_reuse is not None))
        current = rows[item["id"]]
        for key in ("source_sha256", "content_sha256", "read_chars", "title", "url", "archive_url", "source_path"):
            require(current[key] == item[key], f"{item['id']}: catalog source changed")
        require(provenance(current) == item["initial_provenance"], f"{item['id']}: initial provenance changed")
        prior_entry = None
        if item.get("review_origin", "fresh").startswith("sampling_reuse_"):
            prior_entry = _validate_reuse_inputs(work, item, manifest, root)
        writer_path = work / "writers" / (item["id"] + ".json")
        review_path = work / "reviewers" / (item["id"] + ".json")
        if item.get("review_origin") == "sampling_reuse_adequate":
            require(prior_entry["final_verdict"] == "adequate", f"{item['id']}: direct retain lacks an adequate prior judgment")
            require(digest(current["summary"]) == item["old_summary_sha256"] or archived_run,
                    f"{item['id']}: current summary changed since preparation")
            require(not writer_path.exists() and not review_path.exists(), f"{item['id']}: direct retain has fake writer/reviewer records")
            pairs.append((item, None, None))
            continue
        if not writer_path.exists():
            require(digest(current["summary"]) == item["old_summary_sha256"], f"{item['id']}: old summary changed")
            pending.append(item["id"])
            continue
        writer = read_json(writer_path)
        validate_writer(writer, item)
        require(writer["writer"] == owners["writer"][item["id"]], f"{item['id']}: unexpected writer")
        if item.get("review_origin") == "sampling_reuse_problem":
            require(prior_entry["final_verdict"] in {"undercovered", "distorted"}, f"{item['id']}: problem reuse has no prior problem verdict")
            if writer["decision"] == "retain":
                adjudication_path = work / "adjudications" / (item["id"] + ".json")
                require(adjudication_path.exists(), f"{item['id']}: problem prior must be rewritten or adjudicated")
                adjudication = read_json(adjudication_path)
                _validate_reuse_adjudication(adjudication, item, body, prior_entry, require_quote=False)
                require(adjudication["final_old_verdict"] == "adequate", f"{item['id']}: adjudication does not accept prior summary")
        elif item.get("review_origin", "fresh") == "fresh":
            require(writer["decision"] != "retain" or all(p["old_coverage"] == "covered" for p in writer["major_points"]),
                    f"{item['id']}: retained summary has uncovered major points")
        current_hash = digest(current["summary"])
        already_applied = existing_events.get(event_id(item, writer["summary"], manifest["run_id"]))
        require(current_hash == item["old_summary_sha256"] or (
            archived_run or already_applied is not None and current_hash == digest(writer["summary"])
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
    for directory, expected_ids in (("writers", assigned_ids), ("reviewers", assigned_ids)):
        result_ids = {p.stem for p in (work / directory).glob("*.json") if not p.stem.endswith("-outline")}
        require(result_ids <= expected_ids, f"Unexpected result files in {directory}: {sorted(result_ids - expected_ids)}")
    require(allow_incomplete or not pending, f"Missing writer/reviewer results for {len(pending)} articles: {', '.join(pending)}")
    return manifest, pairs, pending


def _blind_outline_file(work, actor, item_id, role):
    return work / "outlines" / actor / (item_id + ".json")


def _check_blind(root, work, allow_incomplete=False, selected_ids=None, published_entries=None):
    root, work = Path(root), Path(work)
    manifest = read_json(work / "manifest.json")
    require(manifest.get("blind_review") is True, "Invalid blind-review manifest")
    validate_model_config(manifest)
    date.fromisoformat(manifest["revision_date"])
    verify_preserve_files(root, manifest)
    catalog = read_json(root / "data/catalog.json")
    rows = catalog_rows(catalog)
    minimum, maximum = manifest["min_body_chars"], manifest.get("max_body_chars")
    ids = [item["id"] for item in manifest.get("articles", [])]
    require(bool(ids) and len(ids) == len(set(ids)), "Empty/duplicate review scope")
    selected_ids = set(ids) if selected_ids is None else set(selected_ids)
    require(selected_ids <= set(ids), "Selected publication IDs are outside the run scope")
    published_entries = published_entries or {}
    selected = sorted((row for row in rows.values() if row["read_chars"] >= minimum
                       and (maximum is None or row["read_chars"] < maximum)),
                      key=lambda row: (-row["read_chars"], row["id"]))
    require(ids == [row["id"] for row in selected], "Catalog review scope changed; prepare a new run")
    items = {item["id"]: item for item in manifest["articles"]}
    direct_ids = {id for id, item in items.items() if item.get("review_origin") == "sampling_reuse_adequate"}
    eligible_ids = set(ids) - direct_ids
    require(all(item.get("review_origin") in {"fresh", "sampling_reuse_problem", "sampling_reuse_adequate"}
                for item in items.values()), "Invalid review_origin")
    prefix = manifest.get("actor_prefix", "mid")
    assignments = manifest.get("assignments")
    require(isinstance(assignments, dict) and set(assignments) == {"writer", "reviewer"}, "Missing actor assignments")
    owners = {}
    for role, limit in (("writer", 6), ("reviewer", 2)):
        groups = assignments[role]
        expected_agents = {f"{prefix}_{role}_{index}" for index in range(1, limit + 1)}
        require(isinstance(groups, dict) and set(groups) == expected_agents, f"Invalid {role} assignment groups")
        allocated = [article_id for assigned in groups.values() for article_id in assigned]
        require(len(allocated) == len(eligible_ids) and set(allocated) == eligible_ids, f"Missing/duplicate {role} assignment")
        owners[role] = {article_id: agent for agent, assigned in groups.items() for article_id in assigned}
        for agent, assigned in groups.items():
            index = agent.rsplit("_", 1)[1]
            require(read_json(work / f"{role}-{index}-assignment.json") == assigned, f"{agent}: assignment file changed")
    final_transfers = manifest.get("final_reviewer_transfers", {})
    require(isinstance(final_transfers, dict) and set(final_transfers) <= eligible_ids,
            "Invalid final reviewer transfer inventory")
    for article_id, transfer in final_transfers.items():
        require(isinstance(transfer, dict) and transfer.get("id") == article_id
                and items[article_id].get("review_origin") == "fresh"
                and transfer.get("from") == owners["reviewer"][article_id]
                and transfer.get("to") in set(assignments["reviewer"])
                and transfer.get("to") != transfer.get("from")
                and transfer.get("to") != owners["writer"][article_id]
                and transfer.get("confirmed_unread") is True,
                f"{article_id}: invalid final reviewer transfer")
    format_logs, format_hash_aliases = _validate_format_corrections(work, manifest, owners)
    manifest["_format_correction_logs"] = format_logs
    manifest["_format_hash_aliases"] = format_hash_aliases
    manifest["_input_exceptions"] = _validate_input_exceptions(work, manifest, owners)
    manifest["_reviewer_reassignments"] = _validate_reviewer_reassignments(work, manifest, owners)
    _validate_checkpoint_indices(work, manifest, owners, format_hash_aliases)
    global_reuse = manifest.get("reused_audit")
    if global_reuse is not None:
        audit_path = Path(manifest.get("reused_audit_path") or (root / global_reuse["file"]))
        require(file_hash(audit_path) == global_reuse.get("sha256"), "Pinned reuse audit changed")
    else:
        require(not any(item.get("reused_audit") for item in items.values()), "Reused item without a pinned audit")

    ledger = load_ledger(root)
    for event in ledger["entries"]:
        validate_history_review(event)
    existing_events = {event.get("revision_id"): event for event in ledger["entries"] if event.get("revision_id")}
    public_audit_path = root / "data/summary-reviews.json"
    public_audit = read_json(public_audit_path) if public_audit_path.exists() else None
    archived_runs = {run.get("run_id") for run in (public_audit or {}).get("previous_runs", [])}
    archived_run = manifest["run_id"] in archived_runs
    pairs, pending = [], []

    def add_pending(article_id, stage):
        value = f"{article_id}:{stage}"
        if value not in pending:
            pending.append(value)

    for item in manifest["articles"]:
        article_id = item["id"]
        article_pending_start = len(pending)
        body = validate_body(work, item, require_source=True)
        current = rows[article_id]
        for key in ("source_sha256", "content_sha256", "read_chars", "title", "url", "archive_url", "source_path"):
            require(current[key] == item[key], f"{article_id}: catalog source changed")
        require(provenance(current) == item["initial_provenance"], f"{article_id}: initial provenance changed")
        origin = item["review_origin"]
        if origin.startswith("sampling_reuse_"):
            prior_entry = _validate_reuse_inputs(work, item, manifest, root)
        else:
            require(not item.get("reused_audit") and not item.get("prior_review_path"),
                    f"{article_id}: fresh item has stale reuse metadata")
            prior_entry = None

        if article_id not in selected_ids:
            published_entry = published_entries.get(article_id)
            if published_entry is None:
                require(current["summary"] == item["summary"]
                        and digest(current["summary"]) == item["old_summary_sha256"],
                        f"{article_id}: unselected old summary changed")
            else:
                require(published_entry.get("before_summary_sha256") == item["old_summary_sha256"],
                        f"{article_id}: prior publication has a different old-summary version")
                expected_source = {key: item[key] for key in ("source_path", "source_sha256", "content_sha256", "body_sha256", "read_chars")}
                require(published_entry.get("source") == expected_source,
                        f"{article_id}: prior publication has different source fingerprints")
                prior_writer = published_entry.get("writer")
                expected_summary = prior_writer["summary"] if isinstance(prior_writer, dict) else item["summary"]
                require(current["summary"] == expected_summary,
                        f"{article_id}: previously published summary changed")
            continue

        writer_path = work / "writers" / (article_id + ".json")
        review_path = work / "reviewers" / (article_id + ".json")
        initial_path = work / "initial" / (article_id + ".json")
        reviewer_initial_path = work / "reviewer-initial" / (article_id + ".json")
        adjudication_path = work / "adjudications" / (article_id + ".json")
        request_path = work / "requests" / (article_id + ".json")

        if origin == "sampling_reuse_adequate":
            require(prior_entry["final_verdict"] == "adequate", f"{article_id}: direct retain lacks an adequate prior judgment")
            require(digest(current["summary"]) == item["old_summary_sha256"] or archived_run,
                    f"{article_id}: current summary changed since preparation")
            forbidden = [writer_path, review_path, initial_path, reviewer_initial_path, adjudication_path, request_path]
            forbidden.extend(work.glob(f"outlines/*/{article_id}.json"))
            forbidden.extend(work.glob(f"reviewer-outlines/*/{article_id}.json"))
            require(not any(path.exists() for path in forbidden), f"{article_id}: direct retain has fake new review records")
            pairs.append((item, None, None))
            continue

        writer_actor = owners["writer"][article_id]
        reviewer_actor = owners["reviewer"][article_id]
        writer_outline_path = _blind_outline_file(work, writer_actor, article_id, "writer")
        reviewer_outline_path = _blind_outline_file(work, reviewer_actor, article_id, "reviewer")
        writer_outline = reviewer_outline = None
        if writer_outline_path.exists():
            writer_outline = read_json(writer_outline_path)
            validate_outline(writer_outline, writer_actor, item, body)
        else:
            add_pending(article_id, "writer-outline")
        if reviewer_outline_path.exists():
            reviewer_outline = read_json(reviewer_outline_path)
            validate_outline(reviewer_outline, reviewer_actor, item, body)
        else:
            add_pending(article_id, "reviewer-outline")

        writer_initial = reviewer_initial = None
        if origin == "sampling_reuse_problem":
            require(not initial_path.exists() and not reviewer_initial_path.exists(),
                    f"{article_id}: reused prior judgment must not have fabricated new old-summary initials")
            require(prior_entry["final_verdict"] in {"undercovered", "distorted"},
                    f"{article_id}: problem reuse has no prior problem verdict")
        else:
            if initial_path.exists() and writer_outline is not None:
                writer_initial = read_json(initial_path)
                validate_initial(writer_initial, writer_outline, writer_outline_path, writer_actor, item, body,
                                 _allowed_file_hashes(work, writer_outline_path, format_hash_aliases))
            else:
                add_pending(article_id, "writer-initial")
            if reviewer_initial_path.exists() and reviewer_outline is not None:
                reviewer_initial = read_json(reviewer_initial_path)
                validate_initial(reviewer_initial, reviewer_outline, reviewer_outline_path, reviewer_actor, item, body,
                                 _allowed_file_hashes(work, reviewer_outline_path, format_hash_aliases))
            else:
                add_pending(article_id, "reviewer-initial")

        adjudication = None
        excluded_writer, excluded_reviewer = set(), set()
        writer_coverage_corrections = {}
        expected_old_verdict = None
        if origin == "sampling_reuse_problem":
            expected_old_verdict = prior_entry["final_verdict"]
            if adjudication_path.exists():
                adjudication = _validate_reuse_adjudication(read_json(adjudication_path), item, body, prior_entry)
                expected_old_verdict = adjudication["final_old_verdict"]
                require(expected_old_verdict == "adequate", f"{article_id}: new source-based adjudication must not retain a problem prior")
        elif writer_initial is not None and reviewer_initial is not None:
            different = writer_initial["verdict"] != reviewer_initial["verdict"]
            request = read_json(request_path) if request_path.exists() else None
            if request is not None:
                _validate_request(request, item, initial_path, reviewer_initial_path,
                                  _allowed_file_hashes(work, initial_path, format_hash_aliases),
                                  _allowed_file_hashes(work, reviewer_initial_path, format_hash_aliases))
            if different and request is None:
                add_pending(article_id, "request")
            if adjudication_path.exists():
                if request is None:
                    add_pending(article_id, "request")
                elif writer_outline is not None and reviewer_outline is not None:
                    adjudication, writer_coverage_corrections = _validate_fresh_adjudication(read_json(adjudication_path), item, body, writer_initial,
                        reviewer_initial, initial_path, reviewer_initial_path, writer_outline, reviewer_outline,
                        _allowed_file_hashes(work, initial_path, format_hash_aliases),
                        _allowed_file_hashes(work, reviewer_initial_path, format_hash_aliases))
                    excluded_writer = set(adjudication["excluded_writer_points"])
                    excluded_reviewer = set(adjudication["excluded_reviewer_points"])
                    expected_old_verdict = adjudication["final_old_verdict"]
            elif different or request is not None:
                add_pending(article_id, "adjudication")
            else:
                expected_old_verdict = writer_initial["verdict"]

        writer = review = None
        if not writer_path.exists():
            add_pending(article_id, "draft")
        else:
            writer = read_json(writer_path)
            validate_writer(writer, item, excluded_writer)
            require(writer["writer"] == writer_actor, f"{article_id}: unexpected writer")
            if writer_outline is not None:
                writer_outline_hashes = _allowed_file_hashes(work, writer_outline_path, format_hash_aliases)
                writer_initial_hashes = (_allowed_file_hashes(work, initial_path, format_hash_aliases)
                                         if initial_path.exists() else set())
                validate_blind_writer(writer, writer_outline, writer_outline_path, item, body,
                                      writer_initial, initial_path if writer_initial is not None else None,
                                      excluded_writer, require_no_initial=(origin == "sampling_reuse_problem"),
                                      allowed_outline_hashes=writer_outline_hashes,
                                      allowed_initial_hashes=writer_initial_hashes,
                                      coverage_corrections=writer_coverage_corrections)
            else:
                add_pending(article_id, "writer-outline")
            if expected_old_verdict is not None:
                expected_decision = "retain" if expected_old_verdict == "adequate" else "rewrite"
                require(writer["decision"] == expected_decision, f"{article_id}: writer decision conflicts with accepted old verdict")
            elif origin == "fresh":
                add_pending(article_id, "initial-adjudication")
            if origin == "sampling_reuse_problem" and adjudication is None:
                require(writer["decision"] == "rewrite", f"{article_id}: known problem prior must be rewritten")
            current_hash = digest(current["summary"])
            applied_event = existing_events.get(event_id(item, writer["summary"], manifest["run_id"]))
            require(current_hash == item["old_summary_sha256"] or archived_run or (
                applied_event is not None and current_hash == digest(writer["summary"])
                and applied_event["after"]["summary"] == writer["summary"]
            ), f"{article_id}: current summary changed since preparation")
            if not review_path.exists():
                add_pending(article_id, "review")
            else:
                review = read_json(review_path)
                validate_reviewer(review, writer, item)
                transfer = final_transfers.get(article_id)
                if transfer is not None and reviewer_outline is not None and reviewer_initial is not None:
                    validate_final_reviewer_transfer(work, transfer, owners, item, body, writer, review,
                        reviewer_outline, reviewer_outline_path, reviewer_initial, reviewer_initial_path,
                        excluded_reviewer, expected_old_verdict)
                elif transfer is not None:
                    add_pending(article_id, "original-reviewer-evidence")
                elif reviewer_outline is not None:
                    require(review["reviewer"] == reviewer_actor, f"{article_id}: unexpected reviewer")
                    reviewer_outline_hashes = _allowed_file_hashes(work, reviewer_outline_path, format_hash_aliases)
                    reviewer_initial_hashes = (_allowed_file_hashes(work, reviewer_initial_path, format_hash_aliases)
                                               if reviewer_initial_path.exists() else set())
                    validate_blind_reviewer(review, reviewer_outline, reviewer_outline_path, item, body,
                                            reviewer_initial, reviewer_initial_path if reviewer_initial is not None else None,
                                            excluded_reviewer, require_no_initial=(origin == "sampling_reuse_problem"),
                                            allowed_outline_hashes=reviewer_outline_hashes,
                                            allowed_initial_hashes=reviewer_initial_hashes)
                else:
                    require(review["reviewer"] == reviewer_actor, f"{article_id}: unexpected reviewer")
                    add_pending(article_id, "reviewer-outline")
                for point in [*writer["major_points"], *review["independent_major_points"]]:
                    for span in point["source_spans"]:
                        require(sum(char.isalnum() for char in body[span["start"]:span["end"]]) >= 8,
                                f"{article_id}: source span has no substantive text")
        if writer is None and not review_path.exists():
            add_pending(article_id, "review")
        if len(pending) == article_pending_start:
            pairs.append((item, writer, review))

    expected_non_direct = set(ids) - direct_ids
    for directory in ("writers", "reviewers", "initial", "reviewer-initial", "adjudications", "requests"):
        actual = {path.stem for path in (work / directory).glob("*.json")}
        require(actual <= expected_non_direct, f"Unexpected direct-retain or out-of-scope records in {directory}")
    for directory in ("outlines", "reviewer-outlines"):
        for path in (work / directory).glob("*/*.json"):
            actor = path.parent.name
            role = "writer" if actor in owners["writer"].values() else "reviewer" if actor in owners["reviewer"].values() else None
            require(path.stem in expected_non_direct and role is not None and owners[role].get(path.stem) == actor,
                    f"Unexpected actor outline in {directory}: {path}")
    require(allow_incomplete or not pending,
            f"Missing initial/draft/review/adjudication stages: {', '.join(pending)}")
    return manifest, pairs, pending


def check(root, work, allow_incomplete=False):
    manifest = read_json(Path(work) / "manifest.json")
    if manifest.get("blind_review") is True:
        return _check_blind(root, work, allow_incomplete)
    return _check_legacy(root, work, allow_incomplete)


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
            "read_chars": source["read_chars"], "chunks": [{}] * count, **model_config(after)}
    writer = {"writer": after["writer"], "summary": after["summary"], "major_points": event["coverage"]}
    try:
        validate_reviewer(review, writer, item)
        provenance = review.get("transfer_provenance")
        if provenance is not None:
            require(isinstance(provenance, dict) and provenance.get("to") == review["reviewer"]
                    and provenance.get("from") != provenance.get("to"),
                    f"{event['id']}: invalid historical reviewer transfer")
            for kind in ("outline", "initial"):
                record = provenance.get(kind)
                require(isinstance(record, dict) and record.get("actor") == provenance["to"]
                        and digest(json_text(record)) == provenance.get(kind + "_sha256"),
                        f"{event['id']}: invalid historical transferred {kind}")
                validate_reading(record, item)
            prior_points = review.get("prior_independent_major_points")
            require(isinstance(prior_points, list) and bool(prior_points),
                    f"{event['id']}: historical transfer lacks original reviewer points")
            validate_reviewer(dict(review, independent_major_points=prior_points), writer, item)
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{event['id']}: invalid historical review: {error}") from error


def revision_metadata(ledger):
    dates = [event["after"]["revision_date"] for event in ledger["entries"]]
    return {"latest_date": max(dates) if dates else ledger["revision_date"], "articles": len({event["id"] for event in ledger["entries"]}),
            "events": len(ledger["entries"]), "review_file": "data/summary-revisions.json",
            "model_fields": "model 与 reasoning_effort 保留初始标注来源；修订来源见 summary_revision、逐篇审阅与修订历史。"}


def review_metadata(audit):
    metadata = {"run_id": audit["run_id"], "latest_date": audit["revision_date"], **audit["statistics"],
               **model_config(audit), "review_file": "data/summary-reviews.json"}
    publication = audit.get("publication")
    if publication is not None:
        metadata["publication"] = {"status": publication["status"],
                                    "target_scope": publication["target_scope"],
                                    "published": len(publication["published_ids"]),
                                    "pending": len(publication["pending_ids"])}
    return metadata


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
    staged, temporaries, touched = [], [], []
    try:
        for path, text in outputs.items():
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".summary-", delete=False) as stream:
                temporary = Path(stream.name)
                temporaries.append(temporary)
                stream.write(text)
            existed = path.exists() or path.is_symlink()
            backup = None
            if existed:
                original = path.read_bytes()
                with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".summary-backup-", delete=False) as stream:
                    backup = Path(stream.name)
                    temporaries.append(backup)
                    stream.write(original)
            staged.append({"temporary": temporary, "target": path, "existed": existed, "backup": backup})
        for entry in staged:
            # Include the current target in rollback even if replace raises after
            # its filesystem operation completed.
            touched.append(entry)
            entry["temporary"].replace(entry["target"])
    except BaseException as error:
        rollback_errors = []
        for entry in reversed(touched):
            try:
                if entry["existed"]:
                    entry["backup"].replace(entry["target"])
                else:
                    entry["target"].unlink(missing_ok=True)
            except BaseException as rollback_error:
                rollback_errors.append(f"{entry['target']}: {rollback_error}")
        if rollback_errors and hasattr(error, "add_note"):
            error.add_note("Rollback errors: " + "; ".join(rollback_errors))
        raise
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)


def _checkpoint_component(value):
    require(isinstance(value, str) and value not in {"", ".", ".."}
            and "/" not in value and "\\" not in value, "Invalid checkpoint index component")
    return value


def _checkpoint_object(work, raw):
    sha256 = hashlib.sha256(raw).hexdigest()
    relative = f"checkpoints/objects/{sha256}.json"
    target = confined(work, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        require(file_hash(target) == sha256 and target.read_bytes() == raw, f"Checkpoint object collision/corruption: {sha256}")
        return sha256, relative
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, prefix=".checkpoint-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            require(file_hash(target) == sha256 and target.read_bytes() == raw,
                    f"Checkpoint object collision/corruption: {sha256}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return sha256, relative


def _checkpoint_record(work, actor, role, article_id, path):
    actor, role, article_id = (_checkpoint_component(value) for value in (actor, role, article_id))
    path = confined(work, path)
    raw = path.read_bytes()
    sha256, snapshot_path = _checkpoint_object(work, raw)
    relative_record_path = path.relative_to(Path(work).resolve()).as_posix()
    index_relative = f"checkpoints/index/{actor}/{role}/{article_id}.json"
    lock_relative = f"checkpoints/locks/{actor}/{role}/{article_id}.lock"
    index_path, lock_path = confined(work, index_relative), confined(work, lock_relative)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if index_path.exists():
                index = read_json(index_path)
                require(index.get("schema_version") == 1 and index.get("actor") == actor
                        and index.get("role") == role and index.get("id") == article_id,
                        f"Checkpoint index identity mismatch: {index_relative}")
                require(index.get("record_path") == relative_record_path,
                        f"Checkpoint record path changed for {actor}/{role}/{article_id}")
                versions = index.get("versions")
                require(isinstance(versions, list) and bool(versions), f"Checkpoint versions missing: {index_relative}")
                last_sha = versions[-1].get("sha256")
                created = last_sha != sha256
                if created:
                    versions.append({"sha256": sha256, "snapshot_path": snapshot_path, "observed_at": now})
            else:
                created = True
                index = {"schema_version": 1, "actor": actor, "role": role, "id": article_id,
                         "record_path": relative_record_path, "first_sha256": sha256,
                         "first_observed_at": now,
                         "ordering_note": CHECKPOINT_ORDER_NOTE,
                         "versions": [{"sha256": sha256, "snapshot_path": snapshot_path, "observed_at": now}]}
            if created:
                staged_write({index_path: json_text(index)})
            return created
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _checkpoint_targets(work, manifest, actor=None, article_id=None):
    owners = {role: {article_id: actor for actor, assigned in groups.items() for article_id in assigned}
              for role, groups in manifest["assignments"].items()}
    targets = []
    for item in manifest["articles"]:
        if item.get("review_origin") == "sampling_reuse_adequate":
            continue
        current_id = item["id"]
        if article_id is not None and current_id != article_id:
            continue
        for role, outline_actor in (("writer", owners["writer"][current_id]),
                                    ("reviewer", owners["reviewer"][current_id])):
            if actor is not None and outline_actor != actor:
                continue
            path = Path(work) / "outlines" / outline_actor / (current_id + ".json")
            if path.exists():
                record = read_json(path)
                require(record.get("id") == current_id and record.get("actor") == outline_actor,
                        f"{article_id}: cannot checkpoint outline with mismatched identity")
                targets.append((outline_actor, "outline", current_id, path))
        for directory, role, assignment_role, identity_key in (
            ("initial", "initial", "writer", "actor"),
            ("reviewer-initial", "reviewer-initial", "reviewer", "actor"),
            ("writers", "writer", "writer", "writer"),
            ("reviewers", "reviewer", "reviewer", "reviewer"),
        ):
            assigned_actor = owners[assignment_role][current_id]
            transfer = manifest.get("final_reviewer_transfers", {}).get(current_id, {})
            if role == "reviewer" and transfer:
                assigned_actor = transfer["to"]
            if actor is not None and assigned_actor != actor:
                continue
            path = Path(work) / directory / (current_id + ".json")
            if path.exists():
                record = read_json(path)
                require(record.get("id") == current_id and record.get(identity_key) == assigned_actor,
                        f"{current_id}: cannot checkpoint {role} with mismatched identity")
                targets.append((assigned_actor, role, current_id, path))
        transfer = manifest.get("final_reviewer_transfers", {}).get(current_id, {})
        if transfer and (actor is None or actor == transfer["to"]):
            for role, field in (("transfer-outline", "outline_path"), ("transfer-initial", "initial_path")):
                path = confined(work, transfer[field])
                if path.exists():
                    record = read_json(path)
                    require(record.get("id") == current_id and record.get("actor") == transfer["to"],
                            f"{current_id}: cannot checkpoint transferred reviewer with mismatched identity")
                    targets.append((transfer["to"], role, current_id, path))
        for directory, role, identity_key in (("adjudications", "adjudication", "decider"),):
            if actor is not None and actor != "Codex主代理":
                continue
            path = Path(work) / directory / (current_id + ".json")
            if path.exists():
                record = read_json(path)
                decider = record.get(identity_key)
                require(record.get("id") == current_id and isinstance(decider, str) and bool(decider),
                        f"{current_id}: cannot checkpoint {role} with mismatched identity")
                if actor is None or decider == actor:
                    targets.append((decider, role, current_id, path))
    for directory, role in (("format-corrections", "format-corrections"),
                             ("draft-revisions", "draft-revisions")):
        # These ledgers are run-wide. A per-article checkpoint must not read
        # a shared file that can contain other agents' in-progress records.
        if article_id is not None:
            continue
        for path in (Path(work) / directory).glob("*.json"):
            if actor is not None and path.stem != actor:
                continue
            record = read_json(path)
            record_actor = record.get("actor")
            require(record_actor == path.stem, f"Cannot checkpoint {role} with mismatched actor: {path.name}")
            targets.append((record_actor, role, "run", path))
    primary_path = Path(work) / "primary-acceptance.json"
    if article_id is None and (actor is None or actor == "Codex主代理") and primary_path.exists():
        record = read_json(primary_path)
        require(record.get("decider") == "Codex主代理", "Cannot checkpoint primary acceptance with mismatched decider")
        targets.append(("Codex主代理", "primary-acceptance", "run", primary_path))
    return targets


def checkpoint_records(root, work, actor=None, article_id=None):
    """Checkpoint current private review records by actor/role/article key.

    A checkpoint timestamp records when this process first observed bytes. It
    does not prove that the record was written before an agent read a later stage.

    Optional filters let a reviewer checkpoint only their own records or one
    article immediately after saving it. Run-wide correction ledgers are only
    included in bulk/actor-only calls because they can contain multiple IDs.
    """
    root, work = Path(root).resolve(), Path(work).resolve()
    private_root = (root / ".work").resolve()
    require(work != private_root and work.is_relative_to(private_root), "Checkpoints must stay under the private .work directory")
    manifest = read_json(work / "manifest.json")
    require(manifest.get("blind_review") is True, "Checkpoint helper is for new blind-review runs")
    article_ids = {item["id"] for item in manifest.get("articles", [])}
    known_actors = {name for groups in manifest.get("assignments", {}).values() for name in groups}
    known_actors.add("Codex主代理")
    if actor is not None:
        require(isinstance(actor, str) and actor in known_actors,
                f"Unknown checkpoint actor: {actor!r}")
    if article_id is not None:
        require(isinstance(article_id, str) and article_id in article_ids,
                f"Unknown checkpoint article ID: {article_id!r}")
        if actor is not None and actor != "Codex主代理":
            assigned = any(article_id in ids for role, groups in manifest["assignments"].items()
                           if role in ("writer", "reviewer")
                           for name, ids in groups.items() if name == actor)
            transfer = manifest.get("final_reviewer_transfers", {}).get(article_id, {})
            assigned = assigned or transfer.get("to") == actor
            require(assigned, f"Actor {actor!r} is not assigned article {article_id}")
    targets = _checkpoint_targets(work, manifest, actor=actor, article_id=article_id)
    captured = unchanged = 0
    for actor, role, article_id, path in targets:
        if _checkpoint_record(work, actor, role, article_id, path):
            captured += 1
        else:
            unchanged += 1
    return {"records": len(targets), "new_versions": captured, "unchanged": unchanged}


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


def _primary_acceptance_map(acceptance, manifest, allow_subset=False):
    require(isinstance(acceptance, dict) and acceptance.get("decider") == "Codex主代理",
            "Primary acceptance must be authored by Codex主代理")
    entries = acceptance.get("entries")
    require(isinstance(entries, list), "Primary acceptance must contain an entries list")
    expected_ids = [item["id"] for item in manifest["articles"]]
    entry_ids = [entry.get("id") if isinstance(entry, dict) else None for entry in entries]
    require(len(entry_ids) == len(set(entry_ids)), "Primary acceptance contains duplicate IDs")
    if allow_subset:
        require(set(entry_ids) <= set(expected_ids), "Primary acceptance contains an out-of-scope ID")
    else:
        require(len(entries) == len(expected_ids) and set(entry_ids) == set(expected_ids),
                "Primary acceptance must cover the exact run scope")
    return {entry["id"]: entry for entry in entries}


def _prior_publication_state(prior_audit, manifest):
    if prior_audit is None or prior_audit.get("run_id") != manifest["run_id"]:
        return None
    require(model_config(prior_audit) == model_config(manifest), "Previously accepted run model configuration changed")
    publication = prior_audit.get("publication")
    if publication is None:
        return None
    target_ids = [item["id"] for item in manifest["articles"]]
    require(prior_audit.get("scope", {}).get("article_ids") == target_ids,
            "Previously published run scope changed")
    require(publication.get("target_scope") == len(target_ids),
            "Previously published target scope changed")
    published_ids = publication.get("published_ids")
    pending_ids = publication.get("pending_ids")
    require(isinstance(published_ids, list) and len(published_ids) == len(set(published_ids))
            and set(published_ids) <= set(target_ids), "Invalid previously published ID set")
    expected_published = [article_id for article_id in target_ids if article_id in set(published_ids)]
    expected_pending = [article_id for article_id in target_ids if article_id not in set(published_ids)]
    require(published_ids == expected_published and pending_ids == expected_pending,
            "Previously published IDs are not a cumulative manifest-ordered scope")
    require(publication.get("status") == ("complete" if not expected_pending else "in_progress"),
            "Previously published status conflicts with pending IDs")
    history = prior_audit.get("publication_history")
    require(isinstance(history, list) and bool(history), "Previously published phase history is missing")
    previous_phase_ids = set()
    for phase in history:
        phase_ids = phase.get("published_ids") if isinstance(phase, dict) else None
        require(isinstance(phase_ids, list) and len(phase_ids) == len(set(phase_ids))
                and set(phase_ids) <= set(target_ids), "Invalid publication phase ID set")
        expected_phase_ids = [article_id for article_id in target_ids if article_id in set(phase_ids)]
        phase_pending = [article_id for article_id in target_ids if article_id not in set(phase_ids)]
        phase_status = "complete" if not phase_pending else "in_progress"
        phase_payload = {"run_id": manifest["run_id"], "status": phase_status, "published_ids": expected_phase_ids}
        require(phase_ids == expected_phase_ids and phase.get("status") == phase_status
                and phase.get("phase_id") == canonical_hash(phase_payload)
                and previous_phase_ids <= set(phase_ids), "Publication phases are not cumulative")
        require(phase.get("newly_published_ids") == [article_id for article_id in phase_ids
                                                       if article_id not in previous_phase_ids],
                "Publication phase additions do not match the cumulative history")
        previous_phase_ids = set(phase_ids)
    require(history[-1].get("published_ids") == expected_published
            and history[-1].get("status") == publication.get("status"),
            "Latest publication phase does not match the published receipt")
    entries = prior_audit.get("entries")
    require(isinstance(entries, list), "Previously published audit entries are missing")
    entry_ids = [entry.get("id") if isinstance(entry, dict) else None for entry in entries]
    require(entry_ids == expected_published, "Previously published entries do not match their receipt")
    item_by_id = {item["id"]: item for item in manifest["articles"]}
    for entry in entries:
        item = item_by_id[entry["id"]]
        expected_source = {key: item[key] for key in ("source_path", "source_sha256", "content_sha256", "body_sha256", "read_chars")}
        require(entry.get("source") == expected_source
                and entry.get("before_summary_sha256") == item["old_summary_sha256"],
                f"{entry['id']}: prior publication does not match frozen source/old summary")
    acceptance = prior_audit.get("primary_acceptance")
    acceptance_map = _primary_acceptance_map(acceptance, manifest, allow_subset=True)
    require(all(article_id in acceptance_map for article_id in expected_published),
            "Previously published primary acceptance receipts are missing")
    for article_id in expected_published:
        entry = next(record for record in entries if record["id"] == article_id)
        item = item_by_id[article_id]
        writer = entry.get("writer")
        summary = writer["summary"] if isinstance(writer, dict) else item["summary"]
        receipt = acceptance_map[article_id]
        require(receipt.get("accepted") is True and receipt.get("summary_sha256") == digest(summary),
                f"{article_id}: prior primary acceptance does not match its published summary")
    return {"publication": publication, "published_ids": expected_published,
            "entries": {entry["id"]: entry for entry in entries},
            "acceptance_entries": {article_id: acceptance_map[article_id] for article_id in expected_published},
            "history": history}


def _select_publication_ids(work, manifest, acceptance, requested_ids, prior_state=None):
    acceptance_map = _primary_acceptance_map(acceptance, manifest, allow_subset=True)
    previous_ids = set(prior_state["published_ids"]) if prior_state else set()
    if prior_state:
        for article_id, prior_receipt in prior_state["acceptance_entries"].items():
            require(acceptance_map.get(article_id) == prior_receipt,
                    f"{article_id}: previously published primary acceptance receipt changed")
    item_by_id = {item["id"]: item for item in manifest["articles"]}
    valid_ids = set()
    for article_id, entry in acceptance_map.items():
        if entry.get("accepted") is not True:
            continue
        item = item_by_id[article_id]
        if item.get("review_origin") == "sampling_reuse_adequate":
            summary = item["summary"]
        else:
            writer_path = Path(work) / "writers" / (article_id + ".json")
            if not writer_path.exists():
                continue
            try:
                writer = read_json(writer_path)
                summary = writer.get("summary") if isinstance(writer, dict) else None
            except (ValueError, TypeError, OSError):
                continue
        if isinstance(summary, str) and entry.get("summary_sha256") == digest(summary):
            valid_ids.add(article_id)
    target_ids = [item["id"] for item in manifest["articles"]]
    if requested_ids is None:
        selected = [article_id for article_id in target_ids if article_id in valid_ids]
    else:
        require(isinstance(requested_ids, (list, tuple)) and bool(requested_ids),
                "publication_ids must be a non-empty list")
        require(all(isinstance(article_id, str) for article_id in requested_ids),
                "publication_ids must contain string article IDs")
        require(len(requested_ids) == len(set(requested_ids)), "publication_ids contains duplicates")
        require(set(requested_ids) <= set(target_ids), "publication_ids contains an out-of-scope article")
        selected = [article_id for article_id in target_ids if article_id in set(requested_ids)]
        require(set(selected) <= valid_ids,
                "Every selected article must be currently accepted with a matching summary SHA")
    require(bool(selected), "No currently accepted article IDs are available for publication")
    require(previous_ids <= set(selected), "A cumulative publication selection cannot omit already published IDs")
    return selected


def _validate_primary_acceptance(work, manifest, pairs, allow_subset=False, required_ids=None):
    path = Path(work) / "primary-acceptance.json"
    if not path.exists():
        require(not manifest.get("require_bound_primary_review", False),
                "Primary acceptance with bound review evidence is required for this run")
        return None
    acceptance = read_json(path)
    acceptance_map = _primary_acceptance_map(acceptance, manifest, allow_subset=allow_subset)
    expected_ids = [item["id"] for item in manifest["articles"]]
    final_summaries = {item["id"]: writer["summary"] if writer is not None else item["summary"]
                       for item, writer, _ in pairs}
    pair_map = {item["id"]: (item, writer, review) for item, writer, review in pairs}
    require(isinstance(acceptance.get("bind_review_sha", False), bool),
            "Primary acceptance review binding must be boolean")
    require(not manifest.get("require_bound_primary_review", False) or acceptance.get("bind_review_sha") is True,
            "Primary acceptance must bind the review evidence for this run")
    to_validate = set(expected_ids) if required_ids is None else set(required_ids)
    if not allow_subset:
        require(to_validate == set(expected_ids), "Primary acceptance must cover the exact run scope")
    for article_id in to_validate:
        require(article_id in acceptance_map, f"{article_id}: primary acceptance entry is missing")
        entry = acceptance_map[article_id]
        require(entry.get("accepted") is True, f"{article_id}: primary acceptance is not accepted")
        require(entry.get("summary_sha256") == digest(final_summaries[article_id]),
                f"{article_id}: primary acceptance summary SHA is stale")
        if acceptance.get("bind_review_sha", False):
            item, writer, review = pair_map[article_id]
            if writer is None:
                require(entry.get("reused_entry_sha256") == item.get("reused_audit", {}).get("entry_sha256"),
                        f"{article_id}: primary acceptance reused evidence SHA is stale")
            else:
                review_path = Path(work) / "reviewers" / (article_id + ".json")
                require(review is not None and review_path.exists()
                        and entry.get("review_sha256") == file_hash(review_path),
                        f"{article_id}: primary acceptance review SHA is stale")
    return acceptance


def _length_strata(lengths):
    if lengths and all(3000 <= length < 8000 for length in lengths):
        return ((3000, 5000), (5000, 8000))
    return ((8000, 10000), (10000, 15000), (15000, 20000))


def _publication_statistics(entries, target_items=None):
    decisions = Counter(entry["writer"]["decision"] if entry.get("writer") is not None else "retain"
                        for entry in entries)
    population_lengths = ([item["read_chars"] for item in target_items] if target_items is not None
                          else [entry["source"]["read_chars"] for entry in entries])
    stats = {"reviewed": len(entries), "rewritten": decisions["rewrite"],
             "retained": decisions["retain"],
             "body_characters": sum(entry["source"]["read_chars"] for entry in entries),
             "before_summary_characters": sum(entry["before_characters"] for entry in entries),
             "after_summary_characters": sum(entry["after_characters"] for entry in entries)}
    origins = [entry.get("review_origin", "fresh") for entry in entries]
    stats.update(reused_old_judgments=sum(origin.startswith("sampling_reuse_") for origin in origins),
                 reused_direct_retains=sum(origin == "sampling_reuse_adequate" for origin in origins),
                 new_old_judgments=sum(origin == "fresh" for origin in origins),
                 new_draft_validations=decisions["rewrite"],
                 length_strata=[{"lower": lower, "upper": upper,
                                 "population_count": sum(lower <= length < upper for length in population_lengths),
                                 "published_count": sum(lower <= entry["source"]["read_chars"] < upper
                                                        for entry in entries)}
                                for lower, upper in _length_strata(population_lengths)])
    return stats


def apply(root, work, import_catalog=None, accepted_only=False, publication_ids=None):
    root, work = Path(root), Path(work)
    require(publication_ids is None or accepted_only,
            "publication_ids requires accepted_only=True")
    manifest = read_json(work / "manifest.json")
    prior_audit_path = root / "data/summary-reviews.json"
    prior_audit = read_json(prior_audit_path) if prior_audit_path.exists() else None
    archived_runs = {run.get("run_id") for run in (prior_audit or {}).get("previous_runs", [])}
    require(manifest["run_id"] not in archived_runs, "Cannot apply an archived review run")
    prior_publication = _prior_publication_state(prior_audit, manifest)
    if accepted_only:
        require(manifest.get("blind_review") is True, "accepted_only is available only for blind-review runs")
        require(import_catalog is None, "accepted_only cannot be combined with an imported catalog")
        require(not (prior_audit is not None and prior_audit.get("run_id") == manifest["run_id"]
                     and prior_publication is None),
                "Cannot start phased publication over an existing complete same-run audit")
        acceptance_path = work / "primary-acceptance.json"
        require(acceptance_path.exists(), "accepted_only requires primary-acceptance.json")
        primary_acceptance = read_json(acceptance_path)
        selected_ids = _select_publication_ids(work, manifest, primary_acceptance, publication_ids, prior_publication)
        prior_entries = prior_publication["entries"] if prior_publication else {}
        manifest, pairs, pending = _check_blind(root, work, selected_ids=set(selected_ids),
                                                published_entries=prior_entries)
        require(not pending, f"Selected publication is incomplete: {', '.join(pending)}")
        primary_acceptance = _validate_primary_acceptance(work, manifest, pairs, allow_subset=True,
                                                          required_ids=set(selected_ids))
    else:
        manifest, pairs, pending = check(root, work)
        primary_acceptance = _validate_primary_acceptance(work, manifest, pairs)
        selected_ids = [item["id"] for item in manifest["articles"]]
        if prior_publication is not None:
            require(primary_acceptance is not None,
                    "Previously published run requires its primary acceptance receipts")
            acceptance_map = _primary_acceptance_map(primary_acceptance, manifest, allow_subset=True)
            for article_id, receipt in prior_publication["acceptance_entries"].items():
                require(acceptance_map.get(article_id) == receipt,
                        f"{article_id}: previously published primary acceptance receipt changed")
    config = model_config(manifest)
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
    def optional_record(directory, article_id):
        path = Path(work) / directory / (article_id + ".json")
        return read_json(path) if path.exists() else None

    entries = []
    writer_pairs = [(item, writer, review) for item, writer, review in pairs if writer is not None]
    counts = Counter(writer["decision"] for _, writer, _ in writer_pairs)
    direct_retains = sum(writer is None for _, writer, _ in pairs)
    for item, writer, review in pairs:
        blind = manifest.get("blind_review") is True
        writer_initial = optional_record("initial", item["id"]) if blind and item.get("review_origin") == "fresh" else None
        reviewer_initial = optional_record("reviewer-initial", item["id"]) if blind and item.get("review_origin") == "fresh" else None
        adjudication = optional_record("adjudications", item["id"]) if blind else None
        request = optional_record("requests", item["id"]) if blind else None
        prior_review = None
        if item.get("prior_review_path"):
            prior_review = read_json(Path(work) / item["prior_review_path"])
        if writer is None:
            decision, after_summary = "retain", item["summary"]
            require(rows[item["id"]]["summary"] == after_summary, f"{item['id']}: direct-retain summary changed in import")
        else:
            decision, after_summary = writer["decision"], writer["summary"]
        entry = {"id": item["id"], "title": item["title"], "url": item["url"],
                 "source": {key: item[key] for key in ("source_path", "source_sha256", "content_sha256", "body_sha256", "read_chars")},
                 "before_summary_sha256": item["old_summary_sha256"], "before_characters": len(item["summary"]),
                 "after_characters": len(after_summary), "decision": decision,
                 "review_origin": item.get("review_origin", "fresh"), "reused_audit": item.get("reused_audit"),
                 "writer": writer, "review": review,
                 "writer_initial": writer_initial, "reviewer_initial": reviewer_initial,
                 "request": request, "adjudication": adjudication, "prior_review": prior_review}
        if blind:
            entry["final_old_verdict"] = (adjudication["final_old_verdict"] if adjudication is not None
                                          else prior_review["final_verdict"] if prior_review is not None
                                          else writer_initial["verdict"])
        entries.append(entry)
        if writer is None or decision == "retain":
            continue
        key = event_id(item, after_summary, manifest["run_id"])
        if key not in existing:
            ledger["entries"].append({"revision_id": key, "run_id": manifest["run_id"], "id": item["id"], "title": item["title"],
                "url": item["url"], "archive_url": item["archive_url"],
                "source": {"path": item["source_path"], "source_sha256": item["source_sha256"], "content_sha256": item["content_sha256"],
                           "visible_text_sha256": item["body_sha256"], "read_chars": item["read_chars"], "chunk_count": len(item["chunks"])},
                "before": {"summary": item["summary"], "characters": len(item["summary"]), "provenance": item["initial_provenance"]},
                "after": {"summary": after_summary, "characters": len(after_summary), "paragraphs": len(after_summary.split("\n\n")),
                          "writer": writer["writer"], "reviewer": review["reviewer"], **config,
                          "revision_date": manifest["revision_date"]},
                "coverage": writer["major_points"], "previous_summary_issues": writer["issues"],
                "decision_reason": writer["decision_reason"], "length_reason": writer["length_reason"], "review": review,
                "review_origin": item.get("review_origin", "fresh"), "reused_audit": item.get("reused_audit"),
                "prior_review": prior_review, "writer_initial": writer_initial,
                "reviewer_initial": reviewer_initial, "request": request, "adjudication": adjudication})
            existing.add(key)
        rows[item["id"]]["summary"] = after_summary
        event = next(event for event in ledger["entries"] if event.get("revision_id") == key)
        rows[item["id"]]["summary_revision"] = revision_tag(event)
    if prior_publication is not None:
        current_entries = {entry["id"]: entry for entry in entries}
        for article_id, prior_entry in prior_publication["entries"].items():
            require(current_entries.get(article_id) == prior_entry,
                    f"{article_id}: previously published audit entry changed")
        combined = {**prior_publication["entries"], **current_entries}
        entries = [combined[item["id"]] for item in manifest["articles"] if item["id"] in combined]
    # Verify all historical edges, including the two revisions predating this run.
    replayed = replay_revisions(copy.deepcopy(catalog), ledger)
    require(replayed["articles"] == catalog["articles"], "Applied summaries do not match the latest accepted history")
    ledger["revision_date"] = manifest["revision_date"]
    ledger["reviewer"] = f"Codex orchestration; {config['model']} {config['reasoning_effort']} independent review"
    new_old = sum(item.get("review_origin", "fresh") == "fresh" for item, _, _ in pairs)
    reused_old = sum(item.get("review_origin", "fresh").startswith("sampling_reuse_") for item, _, _ in pairs)
    reused_direct = sum(item.get("review_origin") == "sampling_reuse_adequate" for item, _, _ in pairs)
    stats = {"reviewed": len(pairs), "rewritten": counts["rewrite"], "retained": counts["retain"] + direct_retains,
             "body_characters": sum(item["read_chars"] for item, _, _ in pairs),
             "before_summary_characters": sum(len(item["summary"]) for item, _, _ in pairs),
             "after_summary_characters": sum(len(writer["summary"] if writer is not None else item["summary"])
                                             for item, writer, _ in pairs)}
    if manifest.get("blind_review") is True:
        stats.update(reused_old_judgments=reused_old, reused_direct_retains=reused_direct,
                     new_old_judgments=new_old, new_draft_validations=counts["rewrite"],
                     length_strata=[{"lower": lower, "upper": upper,
                                     "population_count": sum(lower <= item["read_chars"] < upper for item, _, _ in pairs)}
                                    for lower, upper in _length_strata([item["read_chars"] for item in manifest["articles"]])])
    if accepted_only or prior_publication is not None:
        stats = _publication_statistics(entries, manifest["articles"])
    scope = {"min_body_chars": manifest["min_body_chars"], "article_ids": [item["id"] for item in manifest["articles"]]}
    if manifest.get("max_body_chars") is not None:
        scope["max_body_chars"] = manifest["max_body_chars"]
    audit = {"schema_version": 1, "run_id": manifest["run_id"], "revision_date": manifest["revision_date"],
             "scope": scope, "assignments": manifest["assignments"],
             "assignment_changes": manifest.get("assignment_changes", []), **config, "statistics": stats,
             "format_corrections": manifest.get("_format_correction_logs", []),
             "initial_point_repairs": manifest.get("initial_point_repairs", []),
             "input_exceptions": manifest.get("_input_exceptions", []),
             "validation_note": "结构、哈希和片段检查不代替语义复核；独立语义结论由实际阅读全文的复核代理记录。",
             "entries": entries}
    if manifest.get("process_exceptions"):
        audit["process_exceptions"] = manifest["process_exceptions"]
    if manifest.get("final_reviewer_transfers"):
        audit["final_reviewer_transfers"] = manifest["final_reviewer_transfers"]
    if manifest.get("_reviewer_reassignments"):
        audit["reviewer_reassignments"] = manifest["_reviewer_reassignments"]
    if primary_acceptance is not None:
        audit["primary_acceptance"] = primary_acceptance
    publication = None
    if accepted_only or prior_publication is not None:
        target_ids = [item["id"] for item in manifest["articles"]]
        published_ids = [article_id for article_id in target_ids if any(entry["id"] == article_id for entry in entries)]
        pending_ids = [article_id for article_id in target_ids if article_id not in set(published_ids)]
        status = "complete" if not pending_ids else "in_progress"
        publication = {"target_scope": len(target_ids), "status": status,
                       "published_ids": published_ids, "pending_ids": pending_ids}
        audit["publication"] = publication
        history = list(prior_publication["history"]) if prior_publication is not None else []
        phase_payload = {"run_id": manifest["run_id"], "status": status, "published_ids": published_ids}
        phase_id = canonical_hash(phase_payload)
        if not any(record.get("phase_id") == phase_id for record in history):
            previous_ids = set(prior_publication["published_ids"]) if prior_publication else set()
            history.append({"phase_id": phase_id, "status": status, "published_ids": published_ids,
                            "newly_published_ids": [article_id for article_id in published_ids
                                                    if article_id not in previous_ids]})
        audit["publication_history"] = history
    audit["previous_runs"] = []
    if prior_audit is not None:
        audit["previous_runs"] = prior_audit.get("previous_runs", [])
        if prior_audit["run_id"] != manifest["run_id"]:
            audit["previous_runs"] = [*audit["previous_runs"], {key: value for key, value in prior_audit.items() if key != "previous_runs"}]
        elif prior_publication is None:
            require(prior_audit == audit, "Accepted results or provenance for this run changed; create a new run for further revisions")
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
    result = {"run_id": manifest["run_id"], **stats}
    if publication is not None:
        result["publication"] = publication
    return result


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
    p.add_argument("--max-body-chars", type=int)
    p.add_argument("--recovery-dir", type=Path)
    p.add_argument("--reuse-audit", type=Path)
    p.add_argument("--actor-prefix", default="long")
    p.add_argument("--blind-review", action="store_true")
    p.add_argument("--model", default=MODEL, help="actual model used for this run; frozen in the review inputs")
    p.add_argument("--reasoning-effort", default=EFFORT, help="actual reasoning effort used for this run")
    p.add_argument("--require-bound-primary-review", action="store_true",
                   help="require primary acceptance receipts bound to the exact review evidence")
    p = sub.add_parser("check")
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--allow-incomplete", action="store_true")
    p = sub.add_parser("apply")
    p.add_argument("--work-dir", type=Path)
    p.add_argument("--import-catalog", type=Path)
    p.add_argument("--accepted-only", action="store_true",
                    help="publish only selected currently accepted articles from a blind-review run")
    p.add_argument("--article-ids-file", type=Path,
                    help="JSON array of cumulative article IDs to publish with --accepted-only")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.root, args.source_dir, args.source_manifest, args.work_dir, args.run_id, args.date,
                             args.min_body_chars, args.chunk_chars, max_body_chars=args.max_body_chars,
                             recovery_dir=args.recovery_dir, reuse_audit=args.reuse_audit,
                             actor_prefix=args.actor_prefix, blind_review=args.blind_review,
                             model=args.model, reasoning_effort=args.reasoning_effort,
                             require_bound_primary_review=args.require_bound_primary_review)
        elif args.command == "check":
            manifest, pairs, pending = check(args.root, args.work_dir, args.allow_incomplete)
            result = {"run_id": manifest["run_id"], "passed": len(pairs), "pending_count": len(pending), "pending": pending}
        else:
            require(args.work_dir is not None or args.import_catalog is not None, "apply needs --work-dir or --import-catalog")
            require(args.article_ids_file is None or args.accepted_only,
                    "--article-ids-file requires --accepted-only")
            require(not args.accepted_only or args.work_dir is not None,
                    "--accepted-only requires --work-dir")
            if args.work_dir:
                publication_ids = read_json(args.article_ids_file) if args.article_ids_file else None
                result = apply(args.root, args.work_dir, args.import_catalog,
                               accepted_only=args.accepted_only, publication_ids=publication_ids)
            else:
                result = sync_import(args.root, args.import_catalog)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"summary_workflow: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
