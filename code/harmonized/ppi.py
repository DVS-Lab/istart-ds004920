"""Seed PPI using the authoritative rf1-sra-socdoors FEAT interaction model."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import nibabel as nib
import numpy as np

from .common import grid, now, sha, stamp, table, verify, write_json
from .inputs import load_plan


def prepare(activation_private, private, seeds=None):
    activation_private, private = Path(activation_private).absolute(), Path(private).absolute()
    parent = load_plan(activation_private)
    if private.exists():
        plan = load_plan(private)
        if plan.get("model") != "ppi" or plan["activation_plan"] != str(activation_private):
            raise ValueError("Existing PPI plan belongs to different inputs")
        return plan
    masks = Path(parent["config"]["istart_root"]) / "masks"
    manifest = json.loads((masks / "seed_manifest.json").read_text())
    available = {row["seed"]: row for row in manifest["seeds"]}
    names = list(seeds) if seeds else list(available)
    if not names or len(set(names)) != len(names) or not set(names) <= set(available):
        raise ValueError("Unknown or duplicate PPI seed names")
    executable = shutil.which("fslmeants")
    if not executable:
        raise ValueError("fslmeants unavailable in the configured FSL environment")
    seed_stamps = {}
    for name in names:
        path = masks / available[name]["file"]
        if sha(path) != available[name]["sha256"]:
            raise ValueError(f"Seed differs from Chloe's recorded reslice: {name}")
        grid(path, parent["config"]["reference"])
        seed_stamps[name] = stamp(path, full=True)
    private.mkdir(parents=True, mode=0o700)
    jobs = []
    for original in parent["jobs"]:
        for name in names:
            unit = original["unit"] + "_seed-" + name
            job = dict(original, model="ppi", unit=unit, activation_unit=original["unit"],
                       activation_output=original["output"], seed=name,
                       seed_priority="primary" if name in ("mPFC_left", "mPFC_right") else "exploratory",
                       seed_mask=seed_stamps[name]["path"],
                       output=original["output"].removesuffix("_type-act.feat") + f"_type-ppi_seed-{name}.feat",
                       phys=str(private / "phys" / (unit + ".txt")),
                       prepared=[*original["prepared"], seed_stamps[name]])
            jobs.append(job)
    pilots = [job["unit"] for job in jobs if job["activation_unit"] in parent["pilot"]]
    sources = [*parent["sources"], stamp(activation_private / "plan.json"), stamp(activation_private / "plan.sha256"),
               stamp(masks / "seed_manifest.json"), stamp(executable, full=True), *seed_stamps.values()]
    plan = dict(parent, created=now(), model="ppi", activation_plan=str(activation_private),
                activation_plan_sha256=sha(activation_private / "plan.json"), sources=sources, jobs=jobs, pilot=pilots,
                seeds=names, seed_timeseries_recipe="fslmeants mean of the unchanged provided seed on unsmoothed preprocessed data on the common grid, before statistical-model smoothing as in legacy L1stats.sh; no extra regression or deconvolution")
    write_json(private / "plan.json", plan, exclusive=True)
    (private / "plan.sha256").write_text(sha(private / "plan.json") + "\n")
    table(private / "pilot.tsv", [{"unit": unit} for unit in pilots])
    table(private / "run_review.tsv", [{k: job[k] for k in ("unit", "dataset", "subject", "session", "task", "run", "seed", "seed_priority", "activation_output", "output")} for job in jobs])
    print(f"Prepared {len(jobs)} PPI models; {len(pilots)} pilot models; {len(names)} seeds", flush=True)
    return plan


def series_values(path, nvol):
    values = np.loadtxt(path, ndmin=1)
    if values.shape != (nvol,) or not np.isfinite(values).all() or np.std(values) <= 0:
        raise ValueError("Physiological EV must be one finite, nonconstant value per BOLD volume")
    return values


def verify_series(job, plan):
    from .workflow import verified_spatial
    rec = json.loads(Path(job["phys"]).with_suffix(".json").read_text())
    spatial = verified_spatial(job, plan)
    if rec["unit"] != job["unit"] or rec["source_products"] != spatial["products"] or rec["seed"] != stamp(job["seed_mask"], full=True):
        raise ValueError("PPI seed-series receipt differs from the frozen spatial input or mask")
    if rec["seed_input"] != spatial["seed_input"]:
        raise ValueError("PPI unsmoothed seed input differs from the spatial receipt")
    verify(rec["products"] + [rec["activation_success"], rec["seed_input"]])
    series_values(job["phys"], job["nvol"])
    return rec


def seed_series(job, plan):
    from .workflow import command, complete, spatial_paths, verified_spatial
    path = Path(job["phys"])
    if path.with_suffix(".json").exists():
        return verify_series(job, plan)
    if path.exists():
        raise ValueError(f"Partial seed series preserved for inspection: {path}")
    parent_private = Path(plan["activation_plan"])
    parent = load_plan(parent_private)
    if sha(parent_private / "plan.json") != plan["activation_plan_sha256"]:
        raise ValueError("Activation plan changed")
    original = next(row for row in parent["jobs"] if row["unit"] == job["activation_unit"])
    complete(original, parent, plan["activation_plan_sha256"])
    spatial = verified_spatial(job, plan)
    _, bold, mask = spatial_paths(job)
    seed_input = Path(spatial["seed_input"]["path"])
    verify([spatial["seed_input"]])
    grid(seed_input, mask)
    grid(job["seed_mask"], mask)
    seed = np.asanyarray(nib.load(job["seed_mask"]).dataobj)
    brain = np.asanyarray(nib.load(mask).dataobj)
    if seed.ndim != 3 or not np.isin(seed, [0, 1]).all() or not np.any(seed):
        raise ValueError("Seed must be a nonempty binary 3D mask")
    voxels = int(np.count_nonzero(seed))
    overlap = int(np.count_nonzero((seed > 0) & (brain > 0)))
    if overlap == 0:
        raise ValueError("Seed has no overlap with the run brain mask")
    path.parent.mkdir(parents=True, exist_ok=True)
    command(["fslmeants", "-i", seed_input, "-o", path, "-m", job["seed_mask"]], path.with_suffix(".log"))
    values = series_values(path, job["nvol"])
    rec = {"unit": job["unit"], "created": now(), "seed": stamp(job["seed_mask"], full=True),
           "source_products": spatial["products"], "seed_input": spatial["seed_input"], "activation_success": stamp(Path(original["output"]) / "harmonized_success.json"),
           "seed_voxels": voxels, "seed_voxels_in_brain_mask": overlap, "coverage_fraction": overlap / voxels,
           "partial_coverage_flag": overlap < voxels, "seed_mask_modified": False, "nvol": len(values),
           "series_mean": float(values.mean()), "series_sd": float(values.std()),
           "products": [stamp(path, full=True)], "scientific_inclusion": "not_assigned"}
    write_json(path.with_suffix(".json"), rec, exclusive=True)
    return verify_series(job, plan)
