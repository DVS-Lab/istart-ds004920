#!/usr/bin/env python3
"""Commit the explicit status/log allowlist directly to main under status/linux2.

Uses an isolated bare Git repository, never the production checkout's index.
No images, full HTML reports, licenses, environment dumps or credentials are sent.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
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


def rolling_model_progress(root, plan):
    """Count unique PPI model statuses, including pilots and resumed attempts."""
    units = {job["unit"]: job.get("dataset", "unknown") for job in plan.get("jobs", []) if job.get("unit")}
    latest = {}
    for name in ("harmonized-l1-pilot.log", "harmonized-l1-cohort.log", "harmonized-l1-sub10657-review.log"):
        path = root / "logs" / name
        if not path.is_file() or path.is_symlink():
            continue
        with path.open(errors="replace") as stream:
            for line in stream:
                fields = line.strip().split(maxsplit=2)
                if len(fields) >= 2 and fields[0] in units and fields[1] in ("verified", "review_or_failure"):
                    latest[fields[0]] = {"status": fields[1], "reason": fields[2] if len(fields) > 2 else ""}
    datasets = {}
    for dataset, planned in Counter(units.values()).items():
        verified = sum(row["status"] == "verified" for unit, row in latest.items() if units[unit] == dataset)
        failed = sum(row["status"] == "review_or_failure" for unit, row in latest.items() if units[unit] == dataset)
        datasets[dataset] = {"planned": planned, "verified": verified, "failed_or_held": failed,
                             "remaining_or_running": planned - verified - failed}
    verified = sum(row["status"] == "verified" for row in latest.values())
    issues = [{"unit": unit, "dataset": units[unit], "reason": row["reason"]} for unit, row in latest.items() if row["status"] != "verified"]
    return {"planned": len(units), "verified": verified, "failed_or_held": len(issues),
            "remaining_or_running": len(units) - len(latest), "percent_verified": round(100 * verified / len(units), 2) if units else 0,
            "by_dataset": datasets, "issues": issues[:100], "issues_truncated": len(issues) > 100,
            "source": "Latest per-model controller log status, deduplicated across pilot, cohort and resumes; final cohort check remains separate"}


def held_spatial_details(plan):
    """Publish existing smoothness QC and bounded command logs, never images."""
    rows = []
    for job in plan.get("jobs", []):
        if job.get("model") == "ppi" or not job.get("spatial"):
            continue
        folder = Path(job["spatial"])
        if folder.is_symlink():
            continue
        receipt = read_json(folder / "receipt.json", {})
        review = read_json(folder / "review_required.json", {})
        if receipt.get("status") != "outside_tolerance" and not review:
            continue
        row = {"unit": job["unit"], "spatial": str(folder),
               "receipt": {k: receipt[k] for k in ("method", "status", "target_mm", "bmall", "before", "after") if k in receipt},
               "review": review}
        row["measurement_tables"] = {}
        for name in ("before.tsv", "after.tsv"):
            path = folder / name
            if path.is_file() and not path.is_symlink():
                row["measurement_tables"][name] = path.read_text()[:4000]
        path = folder / "commands.log"
        if path.is_file() and not path.is_symlink():
            with path.open(errors="replace") as stream:
                row["command_log_tail"] = "".join(deque(stream, maxlen=120))[-20000:]
        rows.append(row)
        if len(rows) >= 20:
            break
    return rows


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
    review = read_json(root / "work/postpreproc-review.json")
    if review is not None:
        report["preprocessing_review"] = review
    l1_tools = read_json(root / "work/l1-tools.json")
    if l1_tools is not None:
        report["l1_tools"] = l1_tools
    report["l1_runs"] = []
    for path in sorted((root / "work/l1-runs").glob("*/state.json")):
        state = read_json(path, {})
        record = {key: state[key] for key in ("pid", "phase", "status", "launched", "finished", "operation", "jobs", "error", "log", "operations", "reviewed_activation", "reviewed_ppi", "all_cohort_checks_passed") if key in state}
        record["process_alive"] = alive(state, state.get("worker_script", "run_harmonized_l1.py"))
        report["l1_runs"].append(record)
    failed_subjects = set()
    l1_failed_logs = []
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
            failed_subjects.update(r["subject"] for r in row["subject_results"] if r["status"] == "failed")
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
            if plan.get("model") == "ppi":
                row["rolling_progress"] = rolling_model_progress(root, plan)
            elif plan.get("jobs"):
                row["held_spatial_details"] = held_spatial_details(plan)
            for validation in sorted(planfile.parent.glob("*_validation.json")):
                data = read_json(validation, {})
                stage = {"file": validation.name, **{k: data[k] for k in ("time", "selected", "verified", "all_selected_verified", "visual_qc", "smoothness_qc_review") if k in data}}
                result_file = validation.with_name(validation.name.removesuffix("_validation.json") + ".tsv")
                if result_file.is_file():
                    with result_file.open() as stream:
                        issues = [dict(r) for r in csv.DictReader(stream, delimiter="\t") if r.get("status") != "verified"]
                    stage.update(issues=issues[:100], issues_truncated=len(issues) > 100)
                    for issue in issues[:8]:
                        unit = issue.get("unit", "")
                        if unit and Path(unit).name == unit:
                            l1_failed_logs += [planfile.parent / "logs" / (unit + ".log"), planfile.parent / "designs" / unit / "feat_model.log"]
                row["stages"].append(stage)
            pilot = read_json(planfile.parent / "pilot_summary.json")
            if pilot is not None:
                row["pilot_summary"] = pilot
            review_file = planfile.parent / "run_review.tsv"
            if review_file.is_file():
                with review_file.open() as stream:
                    review_rows = list(csv.DictReader(stream, delimiter="\t"))
                counts = {}
                unavailable = []
                for record in review_rows:
                    status = record.get("status", "prepared")
                    counts[status] = counts.get(status, 0) + 1
                    if status not in ("ready", "candidate", "prepared"):
                        unavailable.append({key: record.get(key) for key in ("unit", "dataset", "subject", "session", "task", "status", "reason")})
                row.update(readiness_counts=counts, unavailable_runs=unavailable[:200], unavailable_runs_truncated=len(unavailable) > 200)
            report["l1_plans"].append(row)
    tails = []
    logs = set((root / "logs").glob("preprocessing-*.log"))
    logs.update((root / "logs/fmriprep").glob("sub-*.log"))
    logs.update((root / "work/preprocessing-runs").glob("*/sub-*.launcher.log"))
    logs.update((root / "logs").glob("harmonized-l1-*.log"))
    recent = sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True)[:16]
    failed_logs = [p for p in logs if any(p.name in (f"{subject}.log", f"{subject}.launcher.log") for subject in failed_subjects)]
    l1_failed_logs = [path for path in l1_failed_logs if path.is_file() and not path.is_symlink()]
    for log in list(dict.fromkeys([*l1_failed_logs[:8], *failed_logs, *recent])):
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
    parser.add_argument("--run-state", type=Path, help="L1 worker state under the production work directory")
    args = parser.parse_args()
    ROOT = args.root.resolve()
    watched_state = args.run_state.resolve() if args.run_state else ROOT / "work/preprocessing-runs" / args.batch / "state.json"
    if args.run_state:
        try:
            watched_state.relative_to(ROOT / "work")
        except ValueError:
            parser.error("Run state must be under the production work directory")
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
                if not watched_state.is_file():
                    raise ValueError("Launch the named processing worker before starting its publisher")
                log = ROOT / "logs/github-status-publisher.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                argv = [sys.executable, str(Path(__file__).resolve()), "watch", "--batch", args.batch, "--interval", str(args.interval), "--root", str(ROOT)]
                if args.run_state:
                    argv += ["--run-state", str(watched_state)]
                with log.open("a") as output, open(os.devnull) as null:
                    child = subprocess.Popen(argv, stdin=null, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                             env=dict(os.environ, PYTHONUNBUFFERED="1"))
                state = {"pid": child.pid, "batch": args.batch, "status": "started", "started": now(), "log": str(log), "launch_command": argv, "target_branch": "main", "watched_state": str(watched_state)}
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
                batch = read_json(watched_state, {})
                worker_script = "run_harmonized_l1.py" if args.run_state else "preprocess_istart.py"
                finished = batch.get("status") in ("complete", "finished_with_failures", "failed") or not alive(batch, worker_script)
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
