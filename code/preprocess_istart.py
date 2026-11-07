#!/usr/bin/env python3
"""Priority-0 iSTART preprocessing; independent of AFNI, FSL and downstream L1."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time

import nibabel as nib
import numpy as np

from harmonized import preprocess as pre
from harmonized.common import ROOT, command, config, image_info, lock, now, read_table, sha, stamp, table, unique, verify, write_json


def normalize(subject):
    sub = "sub-" + subject.removeprefix("sub-")
    if not re.fullmatch(r"sub-[A-Za-z0-9]+", sub):
        raise ValueError("Invalid subject label")
    return sub


def check_subject(c, subject):
    root = Path(c["istart_root"])
    receipt_path = root / f"work/preprocessing/{subject}.json"
    rec = json.loads(receipt_path.read_text())
    if rec["status"] != "command_succeeded":
        raise ValueError("fMRIPrep command success is not recorded")
    verify(rec["bids_inputs"] + [rec["container"]])
    desc = json.loads((root / "derivatives/fmriprep/dataset_description.json").read_text())
    if not any(g.get("Name", "").lower() == "fmriprep" and g.get("Version") == "25.2.5" for g in desc.get("GeneratedBy", [])):
        raise ValueError("Derivative dataset does not record fMRIPrep 25.2.5")
    report = root / f"derivatives/fmriprep/{subject}.html"
    if not report.is_file() or report.stat().st_size == 0:
        raise ValueError("fMRIPrep HTML report missing")
    products = [report, receipt_path]
    results = []
    for task in pre.TASKS:
        folder = root / f"derivatives/fmriprep/{subject}/func"
        bold = unique(folder.glob(f"{subject}_task-{task}_*space-MNI152NLin6Asym*_desc-preproc_bold.nii.gz"), "MNI BOLD")
        img = image_info(bold, tr=1.75)
        mask = bold.with_name(bold.name.replace("desc-preproc_bold", "desc-brain_mask"))
        image_info(mask, mask=True)
        from harmonized.common import grid
        grid(mask, bold)
        confounds = unique(folder.glob(f"{subject}_task-{task}_*desc-confounds_timeseries.tsv"), "confounds")
        fields, records = read_table(confounds)
        if len(records) != img.shape[3] or not all(k in fields for k in ("trans_x", "rot_x", "framewise_displacement", "a_comp_cor_00")):
            raise ValueError("Missing confounds or BOLD/confound row mismatch")
        for row in records:
            for value in row.values():
                if value in ("n/a", "NaN", "nan", "", None):
                    continue
                if not np.isfinite(float(value)):
                    raise ValueError("Nonfinite fMRIPrep confound")
        products += [bold, mask, confounds]
        results.append({"subject": subject, "task": task, "nvol": img.shape[3], "tr": 1.75,
                        "bold": str(bold), "mask": str(mask), "confounds": str(confounds), "status": "structural_checks_passed"})
    return {"subject": subject, "checked": now(), "checks": results,
            "products": [stamp(p) for p in products], "visual_qc": "pending"}


def resource_snapshot():
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    available = None
    if Path("/proc/meminfo").exists():
        available = int(re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())[1]) / 1024
    return {"cpus": cpus, "available_memory_mb": available, "load_average": list(os.getloadavg())}


def resources(jobs, nprocs, omp, mem):
    if not 1 <= jobs <= 32 or nprocs < 1 or omp < 1 or omp > nprocs or (mem != 0 and mem < 1000):
        raise ValueError("Use 1–32 subjects; valid thread counts; memory 0 (no explicit budget) or at least 1000 MB per subject")
    snapshot = resource_snapshot()
    if jobs * nprocs > snapshot["cpus"]:
        raise ValueError(f"Requested {jobs*nprocs} processes but only {snapshot['cpus']} CPUs are available")
    available = snapshot["available_memory_mb"]
    if mem and available is not None and jobs * mem > available * .8:
        raise ValueError(f"Requested {jobs*mem} MB; only {available:.0f} MB currently available. Free resources or lower the explicit budget")
    return snapshot


def active_batches(root):
    found = []
    for path in sorted((Path(root) / "work/preprocessing-runs").glob("*/state.json")):
        state = json.loads(path.read_text())
        if alive(state):
            found.append({"name": path.parent.name, "pid": state["pid"], "log": state.get("log")})
    return found


def capacity(c, nprocs, omp, mem, max_jobs=6, reserve_cpus=16):
    if not 1 <= max_jobs <= 32 or reserve_cpus < 0:
        raise ValueError("Use 1–32 maximum subjects and a nonnegative CPU reserve")
    if nprocs < 1 or omp < 1 or omp > nprocs or (mem != 0 and mem < 1000):
        raise ValueError("Invalid per-subject thread or memory request")
    snapshot = resource_snapshot()
    available = snapshot["available_memory_mb"]
    cpu_jobs = max(0, snapshot["cpus"] - reserve_cpus) // nprocs
    memory_jobs = max_jobs if mem == 0 else int(available * .8 // mem) if available is not None else 0
    active = active_batches(c["istart_root"])
    jobs = 0 if active else min(max_jobs, cpu_jobs, memory_jobs)
    return dict(snapshot, active_batches=active, recommended_jobs=jobs, nprocs=nprocs,
                omp=omp, mem_mb=mem, max_jobs=max_jobs, reserve_cpus=reserve_cpus,
                reason="An existing preprocessing batch is active; do not change its frozen code or start an overlapping cohort" if active else
                       "CPU-affinity recommendation; RAM is informational and no explicit fMRIPrep memory budget is set" if mem == 0 else
                       "Recommendation uses CPU affinity and 80% of current MemAvailable; inspect other workloads and I/O before launch")


def alive(state):
    pid = state.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    proc = Path(f"/proc/{pid}/cmdline")
    if proc.exists():
        return "preprocess_istart.py" in proc.read_bytes().decode(errors="replace")
    return True


def launch(c, cfg_path, name, subjects, jobs, nprocs, omp, mem, acquire=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid batch name")
    root = Path(c["istart_root"])
    batch = root / "work/preprocessing-runs" / name
    batch.mkdir(parents=True, exist_ok=True)
    if name != "pilot":
        check_subject(c, "sub-1002")
    with lock(root / "work/preprocessing-runs/schedule.lock"), lock(batch / "launch.lock"):
        state_file = batch / "state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text())
            if alive(state):
                print(json.dumps(state, indent=2))
                return
        active = active_batches(root)
        if active:
            raise ValueError(f"Another managed preprocessing batch is active: {active}. Let it finish before starting a new batch")
        budget = resources(jobs, nprocs, omp, mem)
        spec = {"name": name, "subjects": [normalize(s) for s in subjects] if subjects else None,
                "jobs": jobs, "nprocs": nprocs, "omp": omp, "mem_mb": mem, "acquire": acquire,
                "config": stamp(cfg_path), "scripts": [stamp(ROOT / "code/preprocess_istart.py"), stamp(ROOT / "code/harmonized/preprocess.py"), stamp(ROOT / "code/harmonized/common.py")]}
        spec_file = batch / "spec.json"
        if spec_file.exists() and json.loads(spec_file.read_text()) != spec:
            raise ValueError("Existing batch specification differs; preserve it and use a new batch name")
        write_json(spec_file, spec)
        log = root / f"logs/preprocessing-{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(ROOT / "code/preprocess_istart.py"), "--config", str(cfg_path.absolute()), "worker", "--batch", str(batch)]
        with log.open("a") as f, open(os.devnull) as null:
            child = subprocess.Popen(argv, stdin=null, stdout=f, stderr=subprocess.STDOUT,
                                     start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))
        state = {"name": name, "pid": child.pid, "launched": now(), "log": str(log),
                 "launch_command": shlex.join(argv), "status": "started", "spec": str(spec_file),
                 "resource_request": {"jobs": jobs, "nprocs": nprocs, "omp": omp, "mem_mb": mem},
                 "resources_at_launch": budget}
        write_json(state_file, state)
        print(json.dumps(state, indent=2))


def worker(c, cfg_path, batch):
    batch = Path(batch)
    spec = json.loads((batch / "spec.json").read_text())
    verify([spec["config"], *spec["scripts"]])
    resources(spec["jobs"], spec["nprocs"], spec["omp"], spec["mem_mb"])
    with lock(batch / "worker.lock"):
        print(f"{now()} Worker PID={os.getpid()} starting {spec['name']}", flush=True)
        if spec["acquire"]:
            with lock(Path(c["istart_root"]) / "work/staging.lock"):
                pre.fetch(c, spec["subjects"])
        inv = pre.inventory(c)
        ready = {r["subject"] for r in inv if r["status"] == "ready"}
        if spec["subjects"]:
            subjects = spec["subjects"]
            if not set(subjects) <= ready:
                raise ValueError("Requested participant has no technically ready raw task input")
        else:
            subjects = sorted(ready)
        if not subjects:
            raise ValueError("No subjects available")
        # Record every roster/task gap separately; no scientific inclusion is assigned.
        table(batch / "input_inventory.tsv", inv)
        write_json(batch / "subjects.json", subjects)
        def run(subject):
            try:
                try:
                    check_subject(c, subject)
                    return {"subject": subject, "status": "verified_existing", "reason": ""}
                except (ValueError, OSError, KeyError):
                    pass
                argv = [sys.executable, str(ROOT / "code/harmonized_l1.py"), "--config", str(cfg_path), "preprocess",
                        "--subjects", subject, "--nprocs", str(spec["nprocs"]), "--omp", str(spec["omp"]),
                        "--mem-mb", str(spec["mem_mb"]), "--execute"]
                command(argv, batch / f"{subject}.launcher.log")
                evidence = check_subject(c, subject)
                write_json(batch / f"{subject}.check.json", evidence)
                return {"subject": subject, "status": "verified", "reason": ""}
            except Exception as exc:
                return {"subject": subject, "status": "failed", "reason": str(exc)}
        results = []
        with ThreadPoolExecutor(max_workers=spec["jobs"]) as pool:
            futures = {pool.submit(run, s): s for s in subjects}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                table(batch / "results.tsv", sorted(results, key=lambda r: r["subject"]))
                print(now(), json.dumps(result), flush=True)
        state = json.loads((batch / "state.json").read_text())
        state.update(status="complete" if all(r["status"] != "failed" for r in results) else "finished_with_failures",
                     finished=now(), subjects=len(subjects), failed=sum(r["status"] == "failed" for r in results))
        write_json(batch / "state.json", state)
        print(json.dumps(state, indent=2), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=ROOT / "config/linux2.json")
    sp = p.add_subparsers(dest="action", required=True)
    f = sp.add_parser("fetch")
    f.add_argument("--subjects", nargs="*")
    v = sp.add_parser("validate")
    v.add_argument("--subjects", nargs="+", default=["1002"])
    cap = sp.add_parser("capacity", help="Read-only CPU/memory report and recommended cohort concurrency")
    cap.add_argument("--max-jobs", type=int, default=6)
    cap.add_argument("--reserve-cpus", type=int, default=16)
    for parser in (v, cap):
        parser.add_argument("--nprocs", type=int, default=16)
        parser.add_argument("--omp", type=int, default=2)
        parser.add_argument("--mem-mb", type=int, default=24000, help="0 omits the memory budget and aggregate RAM gate")
    l = sp.add_parser("launch")
    l.add_argument("--name", required=True)
    l.add_argument("--subjects", nargs="*")
    l.add_argument("--jobs", type=int, default=2)
    l.add_argument("--nprocs", type=int, default=16)
    l.add_argument("--omp", type=int, default=2)
    l.add_argument("--mem-mb", type=int, default=24000, help="0 omits the memory budget and aggregate RAM gate")
    l.add_argument("--acquire", action="store_true", help="Detached worker downloads required subjects before processing")
    for name in ("status", "wait", "check"):
        parser = sp.add_parser(name)
        parser.add_argument("--name", required=True)
    a = sp.add_parser("accept-pilot")
    a.add_argument("--reviewer", required=True)
    a.add_argument("--notes", required=True)
    w = sp.add_parser("worker")
    w.add_argument("--batch", type=Path, required=True)
    args = p.parse_args()
    c = config(args.config)
    root = Path(c["istart_root"])
    try:
        if args.action == "fetch":
            with lock(root / "work/staging.lock"):
                pre.fetch(c, args.subjects)
        elif args.action == "capacity":
            print(json.dumps(capacity(c, args.nprocs, args.omp, args.mem_mb, args.max_jobs, args.reserve_cpus), indent=2))
        elif args.action == "validate":
            resources(1, args.nprocs, args.omp, args.mem_mb)
            pre.preprocess(c, args.subjects, args.nprocs, args.omp, args.mem_mb, False)
        elif args.action == "launch":
            launch(c, args.config, args.name, args.subjects, args.jobs, args.nprocs, args.omp, args.mem_mb, args.acquire)
        elif args.action == "worker":
            worker(c, args.config, args.batch)
        elif args.action == "accept-pilot":
            evidence = check_subject(c, "sub-1002")
            if not args.reviewer.strip() or not args.notes.strip():
                raise ValueError("Pilot reviewer and actual inspection notes are required")
            write_json(root / "work/preprocessing-runs/pilot/review.json",
                       {"reviewer": args.reviewer, "notes": args.notes, "time": now(), "pilot_products": evidence["products"]})
        else:
            batch = root / "work/preprocessing-runs" / args.name
            state = json.loads((batch / "state.json").read_text())
            if args.action == "wait":
                while state.get("status") == "started" and alive(state):
                    print(f"{now()} PID {state['pid']} running; log {state['log']}", flush=True)
                    time.sleep(30)
                    state = json.loads((batch / "state.json").read_text())
            if args.action == "check":
                subjects = json.loads((batch / "subjects.json").read_text())
                for subject in subjects:
                    evidence = check_subject(c, subject)
                    write_json(batch / f"{subject}.check.json", evidence)
                print(f"STRUCTURAL CHECK PASSED: {len(subjects)} subjects, both tasks; visual QC remains separate")
            else:
                print(json.dumps(dict(state, process_alive=alive(state)), indent=2))
                if args.action == "wait" and state.get("status") != "complete":
                    raise ValueError("Batch did not finish successfully; inspect log and resume with the same launch command")
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
