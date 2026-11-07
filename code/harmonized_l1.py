#!/usr/bin/env python3
"""Matched Doors/Social Doors L1 with isolated, resumable processing records."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import subprocess

from harmonized.common import ROOT, config


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=ROOT / "config/linux2.json")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("fetch", help="Download pinned OpenNeuro task subset and stage BIDS")
    sub.add_parser("inventory-preproc")
    sub.add_parser("check-preproc")
    pp = sub.add_parser("preprocess")
    pp.add_argument("--subjects", nargs="*")
    pp.add_argument("--nprocs", type=int, default=8)
    pp.add_argument("--omp", type=int, default=2)
    pp.add_argument("--mem-mb", type=int, default=24000)
    pp.add_argument("--execute", action="store_true", help="Default prints commands; explicit execution opt-in")
    prep = sub.add_parser("prepare")
    prep.add_argument("--plan", type=Path, required=True)
    for name in ("spatial", "render", "run", "check"):
        sp = sub.add_parser(name)
        sp.add_argument("--plan", type=Path, required=True)
        sp.add_argument("--stage", choices=("pilot", "cohort"), default="pilot")
        sp.add_argument("--jobs", type=int, default=1)
    ac = sub.add_parser("accept-pilot")
    ac.add_argument("--plan", type=Path, required=True)
    ac.add_argument("--reviewer", required=True)
    ac.add_argument("--notes", required=True)
    ev = sub.add_parser("evaluation-report")
    ev.add_argument("--output", type=Path, required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--plan", type=Path, required=True)
    exp.add_argument("--destination", type=Path, required=True)
    exp.add_argument("--contrasts", type=int, nargs="+", default=[1, 2, 3, 4])
    exp.add_argument("--kinds", nargs="+", default=["cope", "varcope", "zstat"])
    exp.add_argument("--units", nargs="+")
    exp.add_argument("--execute", action="store_true")
    ms = sub.add_parser("motion-select")
    ms.add_argument("--plan", type=Path, required=True)
    ms.add_argument("--output", type=Path, required=True)
    ms.add_argument("--count", type=int, default=8)
    mc = sub.add_parser("motion-compare")
    mc.add_argument("--mapping", type=Path, required=True)
    mc.add_argument("--output", type=Path, required=True)
    mc.add_argument("--fd-thresholds", type=float, nargs="+", required=True,
                    help="Existing FD thresholds from your QC policy; no defaults are invented")
    a = p.parse_args()
    c = config(a.config)
    try:
        if a.action in ("fetch", "inventory-preproc", "check-preproc", "preprocess"):
            from harmonized import preprocess as pre
            if a.action == "fetch":
                with pre.lock(Path(c["istart_root"]) / "work/staging.lock"):
                    pre.fetch(c)
            elif a.action == "inventory-preproc":
                pre.inventory(c)
            elif a.action == "check-preproc":
                return 0 if pre.check_preproc(c) else 1
            else:
                if a.nprocs < 1 or a.omp < 1 or a.mem_mb < 0 or a.omp > a.nprocs:
                    raise ValueError("Invalid preprocessing resource limits")
                pre.preprocess(c, a.subjects, a.nprocs, a.omp, a.mem_mb, a.execute)
        elif a.action == "prepare":
            from harmonized.inputs import prepare
            prepare(c, a.config, a.plan)
        elif a.action in ("spatial", "render", "run", "check"):
            from harmonized.workflow import batch
            if not 1 <= a.jobs <= 32:
                raise ValueError("Use 1–32 workers; start small and measure resources")
            return 0 if batch(a.plan, a.action, a.stage, a.jobs) else 1
        elif a.action == "accept-pilot":
            from harmonized.workflow import accept_pilot
            accept_pilot(a.plan, a.reviewer, a.notes)
        elif a.action == "evaluation-report":
            from harmonized.export import evaluation_report
            evaluation_report(c, a.output)
        elif a.action == "export":
            from harmonized.export import export
            export(a.plan, a.destination, a.contrasts, a.kinds, a.units, a.execute)
        elif a.action == "motion-select":
            from harmonized.inputs import load_plan
            from harmonized.motion import select
            select(load_plan(a.plan), a.output, a.count)
        elif a.action == "motion-compare":
            from harmonized.motion import compare
            return 0 if compare(a.mapping, a.output, a.fd_thresholds) else 1
    except (ValueError, OSError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
