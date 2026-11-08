#!/usr/bin/env python3
"""Detached activation and seed-PPI execution using the existing harmonized runner."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import nibabel as nib
import numpy as np

from harmonized.common import ROOT, config, lock, now, sha, write_json
from harmonized.environment import configure
from harmonized.inputs import prepare, load_plan
from harmonized.ppi import prepare as prepare_ppi
from harmonized.workflow import batch, selected


def alive(state):
    pid = state.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    path = Path(f"/proc/{pid}/cmdline")
    return "run_harmonized_l1.py" in path.read_bytes().decode(errors="replace") if path.exists() else True


def summary(private):
    private = Path(private)
    plan = json.loads((private / "plan.json").read_text())
    rows = []
    for job in selected(plan, "pilot"):
        row = {key: job[key] for key in ("unit", "dataset", "subject", "session", "task", "output")}
        row.update(model=job.get("model", "activation"), seed=job.get("seed"), visual_qc="pending")
        design = private / "designs" / job["unit"] / "preflight.json"
        if design.is_file():
            row["design"] = json.loads(design.read_text())["design"]
        receipt = Path(job["spatial"]) / "receipt.json"
        if receipt.is_file():
            spatial = json.loads(receipt.read_text())
            row["smoothness"] = {key: spatial[key] for key in ("before", "after", "status")}
        if job.get("phys") and Path(job["phys"]).with_suffix(".json").is_file():
            series = json.loads(Path(job["phys"]).with_suffix(".json").read_text())
            row["seed_series"] = {key: series[key] for key in ("seed_voxels", "seed_voxels_in_brain_mask", "coverage_fraction", "partial_coverage_flag", "series_sd")}
        row["success_receipt_present"] = (Path(job["output"]) / "harmonized_success.json").is_file()
        row["statistics"] = []
        for path in sorted((Path(job["output"]) / "stats").glob("cope*.nii.gz")):
            image = nib.load(path)
            values = np.asanyarray(image.dataobj)
            if np.isfinite(values).all():
                row["statistics"].append({"file": path.name, "min": float(values.min()), "max": float(values.max()),
                                          "nonzero_voxels": int(np.count_nonzero(values)), "finite": True})
            else:
                row["statistics"].append({"file": path.name, "finite": False})
        rows.append(row)
    write_json(private / "pilot_summary.json", {"created": now(), "model": plan.get("model", "activation"), "runs": rows,
                                               "visual_qc": "pending", "scientific_inclusion_assigned": False})


def launch(c, cfg, phase, jobs):
    if not 1 <= jobs <= 32:
        raise ValueError("Use 1–32 concurrent L1 runs")
    root = Path(c["istart_root"])
    folder = root / "work/l1-runs" / phase
    folder.mkdir(parents=True, exist_ok=True)
    with lock(root / "work/l1-runs/schedule.lock"), lock(folder / "launch.lock"):
        for path in (root / "work/l1-runs").glob("*/state.json"):
            state = json.loads(path.read_text())
            if alive(state):
                if path.parent == folder:
                    print(json.dumps(state, indent=2))
                    return state
                raise ValueError(f"Another L1 phase is active: {path.parent.name}")
        tools = configure(c)
        statefile = folder / "state.json"
        log = root / f"logs/harmonized-l1-{phase}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        spec = {"phase": phase, "jobs": jobs, "config": str(cfg.absolute()),
                "activation_plan": str(root / "work/harmonized-l1-v1"), "ppi_plan": str(root / "work/harmonized-ppi-v1")}
        specfile = folder / "spec.json"
        if specfile.exists() and json.loads(specfile.read_text()) != spec:
            raise ValueError("Existing phase specification differs; preserve it before choosing another run")
        write_json(specfile, spec)
        argv = [sys.executable, str(ROOT / "code/run_harmonized_l1.py"), "--config", str(cfg.absolute()), "worker", "--phase", phase]
        with log.open("a") as output, open(os.devnull) as null:
            process = subprocess.Popen(argv, stdin=null, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                       env=dict(os.environ, PYTHONUNBUFFERED="1"))
        state = {"pid": process.pid, "phase": phase, "status": "started", "launched": now(), "jobs": jobs,
                 "worker_script": "run_harmonized_l1.py", "log": str(log), "launch_command": shlex.join(argv),
                 "tools": tools, "spec": str(specfile)}
        write_json(statefile, state)
        print(json.dumps(state, indent=2))
        return state


def worker(c, phase):
    root = Path(c["istart_root"])
    folder = root / "work/l1-runs" / phase
    with (folder / "launch.lock").open("a") as barrier:
        fcntl.flock(barrier, fcntl.LOCK_EX)
    with lock(folder / "worker.lock"):
        statefile = folder / "state.json"
        state = json.loads(statefile.read_text())
        spec = json.loads((folder / "spec.json").read_text())
        results = []
        def stage(model, operation, private):
            state.update(operation=f"{model}:{operation}", updated=now())
            write_json(statefile, state)
            passed = batch(private, operation, phase, spec["jobs"])
            results.append({"model": model, "operation": operation, "all_selected_verified": passed})
        try:
            configure(c)
            activation, ppi = Path(spec["activation_plan"]), Path(spec["ppi_plan"])
            if not activation.exists():
                state.update(operation="prepare_activation", updated=now())
                write_json(statefile, state)
                prepare(c, Path(spec["config"]), activation, workers=spec["jobs"])
            else:
                load_plan(activation)
            activation_plan = load_plan(activation)
            pilot_pairs = {(job["dataset"], job["task"]) for job in selected(activation_plan, "pilot")}
            if len(activation_plan["pilot"]) != 4 or pilot_pairs != {(dataset, task) for dataset in ("rf1", "istart") for task in ("doors", "socialdoors")}:
                raise ValueError("The pilot requires a technically ready run for both tasks in both datasets; see run_review.tsv")
            for operation in ("spatial", "run", "check"):
                stage("activation", operation, activation)
            summary(activation)
            if not ppi.exists():
                state.update(operation="prepare_ppi", updated=now())
                write_json(statefile, state)
                prepare_ppi(activation, ppi)
            else:
                load_plan(ppi)
            for operation in ("run", "check"):
                stage("ppi", operation, ppi)
            summary(ppi)
            state.update(status="complete" if all(row["all_selected_verified"] for row in results) else "finished_with_failures",
                         finished=now(), operations=results, activation_plan=str(activation), ppi_plan=str(ppi))
        except Exception as exc:
            state.update(status="failed", finished=now(), error=str(exc), operations=results)
            print(f"{now()} ERROR: {exc}", flush=True)
        write_json(statefile, state)
        print(json.dumps(state, indent=2), flush=True)
        return 0 if state["status"] == "complete" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/linux2.json")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("launch", "status", "worker"):
        option = actions.add_parser(name)
        option.add_argument("--phase", choices=("pilot", "cohort"), default="pilot")
        if name == "launch":
            option.add_argument("--jobs", type=int, default=10)
    args = parser.parse_args()
    c = config(args.config)
    try:
        if args.action == "launch":
            state = launch(c, args.config, args.phase, args.jobs)
            publisher = [sys.executable, str(ROOT / "code/publish_processing_status.py"), "start", "--batch", "l1-" + args.phase,
                         "--run-state", str(Path(c["istart_root"]) / f"work/l1-runs/{args.phase}/state.json")]
            result = subprocess.run(publisher)
            if result.returncode:
                print("Status publisher failed to start; L1 processing remains detached", file=sys.stderr)
            return 0
        if args.action == "worker":
            return worker(c, args.phase)
        state = json.loads((Path(c["istart_root"]) / f"work/l1-runs/{args.phase}/state.json").read_text())
        print(json.dumps(dict(state, process_alive=alive(state)), indent=2))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        # Publish environment/tool diagnostics even when no L1 process could start.
        subprocess.run([sys.executable, str(ROOT / "code/publish_processing_status.py"), "publish"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
