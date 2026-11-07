"""Assemble a local release candidate only; never upload or publish derivatives."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from .common import now, read_table, sha, stamp, table, write_json
from .inputs import load_plan
from .workflow import complete

NAMES = {1: "win", 2: "loss", 3: "decision", 4: "win-minus-loss"}


def export(private, destination, contrasts, kinds, units=None, execute=False):
    private, dest = Path(private).absolute(), Path(destination).absolute()
    plan = load_plan(private)
    allowed = Path(plan["config"]["istart_root"]) / "derivatives/release-candidates"
    if allowed.resolve() not in dest.resolve().parents:
        raise ValueError(f"Release destination must be a fresh child of ignored {allowed}")
    if dest.exists():
        raise ValueError("Release destination exists; preserved unchanged")
    if not contrasts or not set(contrasts) <= set(NAMES) or not kinds or not set(kinds) <= {"cope", "varcope", "zstat"}:
        raise ValueError("Select contrast numbers 1–4 and cope/varcope/zstat")
    jobs = plan["jobs"]
    if units:
        if not set(units) <= {j["unit"] for j in jobs}:
            raise ValueError("Unknown selected export unit")
        jobs = [j for j in jobs if j["unit"] in units]
    if not jobs:
        raise ValueError("No maps selected")
    records = []
    for job in jobs:
        complete(job, plan, sha(private / "plan.json"))
        for contrast in sorted(set(contrasts)):
            for kind in sorted(set(kinds)):
                src = Path(job["output"]) / "stats" / f"{kind}{contrast}.nii.gz"
                rel = Path(job["dataset"]) / (job["stem"] + f"_contrast-{NAMES[contrast]}_stat-{kind}.nii.gz")
                records.append({"unit": job["unit"], "dataset": job["dataset"], "task": job["task"],
                                "contrast_number": contrast, "contrast": NAMES[contrast], "statistic": kind,
                                "source": str(src), "file": str(rel), "sha256": sha(src), "bytes": src.stat().st_size})
    print(json.dumps({"mode": "assemble_local_candidate" if execute else "dry_run", "maps": len(records),
                      "bytes": sum(r["bytes"] for r in records), "destination": str(dest), "records": records}, indent=2))
    if execute:
        if shutil.disk_usage(dest.parent if dest.parent.exists() else Path(plan["config"]["istart_root"])).free < sum(r["bytes"] for r in records) * 1.1:
            raise ValueError("Insufficient free space for release candidate")
        dest.mkdir(parents=True, exist_ok=False)
        for record in records:
            p = dest / record["file"]
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(record["source"], p)
            if sha(p) != record["sha256"]:
                raise ValueError("Export checksum mismatch")
        table(dest / "maps.tsv", records)
        write_json(dest / "release.json", {"created": now(), "plan_sha256": sha(private / "plan.json"),
                   "variant": plan["variant"], "contrasts": sorted(set(contrasts)), "statistics": sorted(set(kinds)),
                   "maps": len(records), "unthresholded": True, "published": False,
                   "scientific_inclusion": "not_assigned_by_utility", "plan_provenance": stamp(private / "plan.json")})
        (dest / "README.md").write_text("# Local unthresholded map release candidate\n\n"
            "Not published. Scientific selection, participant privacy, metadata, and sharing approval remain pending.\n\n"
            "COPE is a contrast parameter estimate; VARCOPE is its variance; ZSTAT is its unthresholded Z statistic. "
            "Task EV order: Win, Loss, Decision, optional missed-decision nuisance. "
            "Contrast order: 1 Win, 2 Loss, 3 Decision, 4 Win minus Loss. "
            "All maps use the RF1 MNI152NLin6Asym reference grid, AFNI total measured 6-mm smoothing and base-only nuisance model. "
            "See release.json and maps.tsv for the exact selected contrasts, statistics, checksums and input plan.\n")


def evaluation_report(c, output):
    evaluation = Path(c["evaluation"])
    _, rows = read_table(evaluation / "run_results.tsv")
    selected = [dict(r, reuse="not_adopted", review="pending", notes="") for r in rows if r.get("status") == "outside_tolerance"]
    if not selected:
        raise ValueError("No outside-tolerance rows found; inspect live schema/status and validation.json")
    output = Path(output)
    if output.exists():
        raise ValueError("Exception review output already exists; preserved")
    output.mkdir(parents=True)
    table(output / "five_run_review.tsv", selected)
    write_json(output / "source.json", {"source": stamp(evaluation / "run_results.tsv"),
               "validation": stamp(evaluation / "validation.json"), "outside_tolerance_runs": len(selected),
               "known_published_snapshot_count": 5, "existing_results_reused": False})
    (output / "README.md").write_text("# Prior smoothing exceptions\n\n"
        f"The live evaluation contains {len(selected)} outside-tolerance runs; the reviewed source summary reported five. "
        "Inspect each run's before/after directional and combined classic FWHM, ACF, mask, receipt and command log. "
        "Record review notes in five_run_review.tsv. This workflow does not import any evaluation images, "
        "does not switch to -bmall, and independently measures/smooths unsmoothed inputs. "
        "Out-of-band runs in the new variant remain blocked for explicit methodological review.\n")
    print(f"Exported {len(selected)} exception rows to {output}; no existing smoothing image reused")
