#!/usr/bin/env python3
"""Commit the explicit status/log allowlist directly to main under status/linux2.

Uses an isolated bare Git repository, never the production checkout's index.
No images, full HTML reports, licenses, environment dumps or credentials are sent.
"""
from __future__ import annotations

import argparse
from collections import deque
import csv
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

REMOTE = "https://github.com/DVS-Lab/istart-ds004920.git"
BRANCH = "main"
PREFIX = "status/linux2/"
ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(text):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(text))
    text = re.sub(r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+)\b", "<redacted-token>", text)
    text = re.sub(r"https?://[^\s<>]+", "<redacted-url>", text)
    text = re.sub(r"/home/[^/\s]+", "/home/<user>", text)
    text = re.sub(r"(?im)^.*\b(?:authorization|password|passwd|access[_-]?token|api[_-]?key|client[_-]?secret)\s*[:=].*$", "<redacted-sensitive-line>", text)
    return text


def clean_values(value):
    if isinstance(value, dict):
        return {key: clean_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_values(item) for item in value]
    return clean(value) if isinstance(value, str) else value


def read_json(path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def alive(state, script):
    pid = state.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    proc = Path(f"/proc/{pid}/cmdline")
    return script in proc.read_bytes().decode(errors="replace") if proc.exists() else True


def collect(root):
    config = read_json(root / "config/linux2.json", {})
    mem = {}
    if Path("/proc/meminfo").exists():
        for name, kb in re.findall(r"^(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M):
            mem[name + "_mb"] = round(int(kb) / 1024, 1)
    disk = shutil.disk_usage(root)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    report = {"schema": 1, "collected_utc": now(), "code_commit": head.stdout.strip() if head.returncode == 0 else None,
              "cpus": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count(),
              "load_average": list(os.getloadavg()), "memory": mem,
              "filesystem_bytes": {"total": disk.total, "free": disk.free}, "preprocessing_batches": [], "l1_plans": []}
    for path in sorted((root / "work/preprocessing-runs").glob("*/state.json")):
        state = read_json(path, {})
        row = {k: state[k] for k in ("name", "pid", "launched", "finished", "status", "subjects", "failed", "log", "launch_command", "resource_request") if k in state}
        row["process_alive"] = alive(state, "preprocess_istart.py")
        spec = read_json(path.parent / "spec.json", {})
        row["resources"] = {k: spec[k] for k in ("jobs", "nprocs", "omp", "mem_mb", "acquire") if k in spec}
        row["pilot_review_recorded"] = (path.parent / "review.json").is_file()
        row["subject_results"] = []
        if (path.parent / "results.tsv").is_file():
            with (path.parent / "results.tsv").open() as stream:
                row["subject_results"] = [{k: r.get(k) for k in ("subject", "status", "reason")} for r in csv.DictReader(stream, delimiter="\t")]
        row["structural_checks"] = []
        for check in sorted(path.parent.glob("sub-*.check.json")):
            data = read_json(check, {})
            row["structural_checks"].append({"subject": data.get("subject"), "checked": data.get("checked"), "visual_qc": data.get("visual_qc"),
                "tasks": [{k: r.get(k) for k in ("task", "nvol", "tr", "status")} for r in data.get("checks", [])]})
        report["preprocessing_batches"].append(row)
    roots = {root, Path(config.get("rf1_analysis", str(root))), Path(config.get("rf1_root", str(root)))}
    for analysis in sorted(roots):
        for planfile in sorted((analysis / "work").glob("*/plan.json")):
            plan = read_json(planfile, {})
            row = {"path": str(planfile), "variant": plan.get("variant"), "jobs": len(plan.get("jobs", [])),
                   "pilot_runs": plan.get("pilot", []), "pilot_review_recorded": (planfile.parent / "pilot_review.json").is_file(), "stages": []}
            for validation in sorted(planfile.parent.glob("*_validation.json")):
                data = read_json(validation, {})
                row["stages"].append({"file": validation.name, **{k: data[k] for k in ("time", "selected", "verified", "all_selected_verified", "visual_qc") if k in data}})
            report["l1_plans"].append(row)
    tails = []
    logs = set((root / "logs").glob("preprocessing-*.log"))
    logs.update((root / "logs/fmriprep").glob("sub-*.log"))
    logs.update((root / "work/preprocessing-runs").glob("*/sub-*.launcher.log"))
    for log in sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True)[:16]:
        # Do not follow links outside the explicit log locations.
        if log.is_symlink() or not log.is_file():
            continue
        with log.open(errors="replace") as stream:
            tail = "".join(deque(stream, maxlen=50))[-16000:]
        tails.append(f"\n--- {log.relative_to(root)} ---\n{clean(tail)}")
    return {"status.json": json.dumps(clean_values(report), indent=2) + "\n", "log-tails.txt": "".join(tails),
            "README.txt": "Linux2 processing handoff\n\nstatus.json: structured CPU, RAM, disk, fMRIPrep and L1 state.\nlog-tails.txt: sanitized tails of explicit preprocessing log locations only.\n\nPublished automatically from Linux2 using an isolated Git object store.\nThis handoff contains no imaging derivatives, raw behavioral data, license files or environment dumps.\nStatus timestamps are UTC; absence of an active PID does not by itself establish success.\n"}


def git(repo, arguments, stdin=None, index_file=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Linux2 processing status",
               GIT_AUTHOR_EMAIL="linux2-status@users.noreply.github.com", GIT_COMMITTER_NAME="Linux2 processing status",
               GIT_COMMITTER_EMAIL="linux2-status@users.noreply.github.com")
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    result = subprocess.run(["git", "--git-dir", str(repo), *arguments], input=stdin, capture_output=True, text=True, env=env)
    if result.returncode:
        raise RuntimeError(clean(result.stderr.strip()) or "Git command failed")
    return result.stdout.strip()


def publish(root, payload, remote=REMOTE):
    folder = root / "work/github-status"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "publish.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        repo = folder / "objects.git"
        if not repo.exists():
            git(repo, ["init", "--bare", str(repo)])
            git(repo, ["remote", "add", "origin", remote])
        if git(repo, ["remote", "get-url", "origin"]) != remote:
            raise RuntimeError("Status repository remote differs; existing object store preserved")
        blobs = []
        for filename, content in sorted(payload.items()):
            if filename not in ("README.txt", "status.json", "log-tails.txt"):
                raise ValueError("File outside the status publication allowlist")
            blob = git(repo, ["hash-object", "-w", "--stdin"], content)
            blobs.append((filename, blob))
        index = folder / "status.index"
        for attempt in range(3):
            git(repo, ["fetch", "--depth=1", "origin", "refs/heads/main"])
            parent = git(repo, ["rev-parse", "FETCH_HEAD"])
            # Start with the complete current main tree, then replace only the
            # allowlisted status paths in an index isolated from production.
            git(repo, ["read-tree", parent], index_file=index)
            for filename, blob in blobs:
                git(repo, ["update-index", "--add", "--cacheinfo", "100644", blob, PREFIX + filename], index_file=index)
            tree = git(repo, ["write-tree"], index_file=index)
            commit = git(repo, ["commit-tree", tree, "-p", parent], "Update Linux2 processing status " + now() + "\n")
            try:
                git(repo, ["push", "origin", commit + ":refs/heads/main"])
            except RuntimeError:
                if attempt == 2:
                    raise
                continue
            print(f"{now()} Published {commit} to main:{PREFIX}", flush=True)
            return commit


def save_state(root, state):
    target = root / "work/github-status/state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n")
    temp.replace(target)


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "publish", "start", "watch", "status"))
    parser.add_argument("--batch", default="cohort-expanded")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--root", type=Path, default=ROOT, help="Production checkout when running an isolated publisher copy")
    args = parser.parse_args()
    ROOT = args.root.resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.batch) or args.interval < 30:
        parser.error("Use a valid batch name and interval of at least 30 seconds")
    statefile = ROOT / "work/github-status/state.json"
    try:
        if args.action == "collect":
            print(collect(ROOT)["status.json"])
        elif args.action == "publish":
            publish(ROOT, collect(ROOT))
        elif args.action == "status":
            state = read_json(statefile, {})
            print(json.dumps(dict(state, process_alive=alive(state, "publish_processing_status.py")), indent=2))
        elif args.action == "start":
            folder = statefile.parent
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "start.lock").open("a") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = read_json(statefile, {})
                if alive(state, "publish_processing_status.py"):
                    if state.get("batch") != args.batch:
                        raise ValueError("A publisher is watching a different batch; let it finish")
                    print(json.dumps(state, indent=2))
                    return 0
                if not (ROOT / "work/preprocessing-runs" / args.batch / "state.json").is_file():
                    raise ValueError("Launch the named preprocessing batch before starting its publisher")
                log = ROOT / "logs/github-status-publisher.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                argv = [sys.executable, str(Path(__file__).resolve()), "watch", "--batch", args.batch, "--interval", str(args.interval), "--root", str(ROOT)]
                with log.open("a") as output, open(os.devnull) as null:
                    child = subprocess.Popen(argv, stdin=null, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                             env=dict(os.environ, PYTHONUNBUFFERED="1"))
                state = {"pid": child.pid, "batch": args.batch, "status": "started", "started": now(), "log": str(log), "launch_command": argv, "target_branch": "main"}
                save_state(ROOT, state)
                print(json.dumps(state, indent=2))
        else:
            # Keep the start lock until the parent has saved our initial state.
            with (statefile.parent / "start.lock").open("a") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
            state = read_json(statefile, {})
            failures = 0
            while True:
                try:
                    commit = publish(ROOT, collect(ROOT))
                    failures = 0
                    state.update(last_commit=commit, last_published=now(), last_error=None)
                except Exception as exc:
                    failures += 1
                    state.update(last_error=clean(str(exc)), consecutive_publish_failures=failures)
                    print(f"{now()} Publish failed: {clean(exc)}", flush=True)
                batch = read_json(ROOT / "work/preprocessing-runs" / args.batch / "state.json", {})
                finished = batch.get("status") in ("complete", "finished_with_failures") or not alive(batch, "preprocess_istart.py")
                state.update(status="finished" if finished and not failures else "publish_failed" if failures else "watching", checked=now())
                save_state(ROOT, state)
                if finished and not failures:
                    break
                # A failed publisher cannot affect preprocessing. Avoid endless auth retries.
                if failures >= 3:
                    return 1
                time.sleep(args.interval)
    except (ValueError, OSError, RuntimeError) as exc:
        print("ERROR: " + clean(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
