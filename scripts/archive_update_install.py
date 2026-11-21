"""Install a completed, verified Zhihu archive update from an isolated site."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import sys

from archive_update import OUTPUT_FILES, STATE, output_hashes, verify_site
from summary_workflow import ROOT, digest, file_hash, read_json, require, staged_write, write_json


def snapshot(root):
    state = root / STATE
    hashes = output_hashes(root)
    hashes[str(STATE)] = file_hash(state) if state.exists() else None
    return {"schema_version": 1, "files_sha256": hashes}


def install(root, stage, baseline):
    root, stage = root.resolve(), stage.resolve()
    require(root != stage and not (stage / ".git").exists(), "Install from an isolated site copy")
    require(baseline.get("schema_version") == 1
            and set(baseline.get("files_sha256", {})) >= {*OUTPUT_FILES, str(STATE)},
            "Invalid starting site fingerprint")
    lock = root / ".work/archive-update.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        source_hashes = snapshot(stage)["files_sha256"]
        require(source_hashes[str(STATE)] is not None, "The isolated site has no complete archive update")
        state = read_json(stage / STATE)
        runs = state.get("completed_runs", [])
        require(state.get("schema_version") == 1 and bool(runs)
                and state.get("archive_commit") == runs[-1].get("archive_tip")
                and runs[-1].get("outputs_sha256") == {
                    name: value for name, value in source_hashes.items() if name != str(STATE)},
                "Isolated site files differ from the completed update")
        current = snapshot(root)
        if current["files_sha256"] == source_hashes:
            return {"archive_commit": state["archive_commit"], "changed": False}
        require(current == baseline, "Live site changed since the batch was prepared")
        require(current["files_sha256"].keys() <= source_hashes.keys(),
                "The isolated site omits live data or citation history")
        old_path = root / STATE
        prior = read_json(old_path) if old_path.exists() else None
        require(runs[:-1] == (prior["completed_runs"] if prior else [])
                and (prior is None or prior["archive_commit"] == runs[-1]["archive_base"]),
                "Completed update does not extend the live archive history")
        verify_site(stage)
        outputs = {root / name: (stage / name).read_bytes().decode("utf-8")
                   for name in source_hashes if source_hashes[name] != current["files_sha256"].get(name)}
        require(all(digest(value) == source_hashes[str(path.relative_to(root))] for path, value in outputs.items())
                and current == snapshot(root) and source_hashes == snapshot(stage)["files_sha256"],
                "Archive update files changed during installation")
        staged_write(outputs)
        return {"archive_commit": state["archive_commit"], "changed": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--output", required=True, type=Path)
    publish = commands.add_parser("install")
    publish.add_argument("--stage-root", required=True, type=Path)
    publish.add_argument("--baseline", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            result = snapshot(args.root)
            write_json(args.output, result)
        else:
            result = install(args.root, args.stage_root, read_json(args.baseline))
    except (KeyError, TypeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
