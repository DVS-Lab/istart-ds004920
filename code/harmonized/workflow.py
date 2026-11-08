from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import json
import os
from pathlib import Path
import re
import shutil

import nibabel as nib
import numpy as np

from .common import (CONTRASTS, PPI_CONTRASTS, ROOT, VARIANT, command, grid, image_info, lock, now,
                     read_table, sha, stamp, table, verify, write_json)
from .inputs import base_confounds, load_plan


def measure_values(path):
    _, rows = read_table(path)
    if len(rows) != 1:
        raise ValueError("Expected exactly one AFNI smoothness measurement")
    names = ("classic_fwhm_x", "classic_fwhm_y", "classic_fwhm_z", "classic_fwhm_combined", "acf_a", "acf_b", "acf_c", "acf_effective_fwhm")
    values = {name: float(rows[0][name]) for name in names}
    if not np.isfinite(list(values.values())).all() or min(values.values()) <= 0:
        raise ValueError("Invalid AFNI measurement")
    return values


def spatial_paths(job):
    d = Path(job["spatial"])
    return d, d / (job["stem"] + "_desc-smoothToFWHM6_bold.nii.gz"), d / "mask.nii.gz"


def verify_job(job):
    verify(job["inputs"] + job["prepared"])
    expected, columns = base_confounds(job["source_confounds"], job["nvol"])
    actual = np.loadtxt(job["confounds"], ndmin=2)
    if columns != job["columns"] or actual.shape != expected.shape or not np.allclose(actual, expected, rtol=1e-12, atol=1e-14):
        raise ValueError("Prepared base-only matrix differs from frozen source")


def verified_spatial(job, plan):
    d, bold, mask = spatial_paths(job)
    rec = json.loads((d / "receipt.json").read_text())
    if rec["unit"] != job.get("activation_unit", job["unit"]) or rec["source_inputs"] != job["inputs"] or rec["method"] != "default-blurmaster-classic6-v1":
        raise ValueError("Spatial receipt does not match this run/method")
    verify(rec["products"])
    grid(bold, plan["config"]["reference"])
    grid(mask, plan["config"]["reference"])
    values = measure_values(d / "after.tsv")
    if not 5.4 <= values["classic_fwhm_combined"] <= 6.6:
        raise ValueError("Smoothing outside 5.4–6.6 mm: review required; no automatic retry")
    return rec


def spatial(job, plan):
    verify_job(job)
    if job.get("model") == "ppi":
        return verified_spatial(job, plan)
    d, bold, mask = spatial_paths(job)
    if d.exists():
        return verified_spatial(job, plan)
    d.mkdir(parents=True)
    log = d / "commands.log"
    env = {"REFERENCE_GRID": plan["config"]["reference"]}
    source = Path(job["bold"])
    if job["dataset"] == "istart":
        source = d / "rf1grid_unsmoothed_bold.nii.gz"
        for kind, inp, out in (("bold", job["bold"], source), ("mask", job["mask"], mask)):
            command(["bash", ROOT / "vendor/grid/resample_to_rf1_grid.sh", "--input", inp,
                     "--kind", kind, "--output", out], log, env)
    else:
        # RF1 has already been preprocessed on the authoritative grid.
        grid(source, plan["config"]["reference"])
        grid(job["mask"], plan["config"]["reference"])
        shutil.copyfile(job["mask"], mask)
    geometry = {"bold": grid(source, plan["config"]["reference"]), "mask": grid(mask, plan["config"]["reference"])}
    img = image_info(source, tr=job["tr"])
    image_info(mask, mask=True)
    if img.shape[3] != job["nvol"]:
        raise ValueError("Resampling changed volume count")
    command(["bash", ROOT / "vendor/afni/measure_smoothness.sh", "--input", source,
             "--mask", mask, "--output-tsv", d / "before.tsv"], log)
    before = measure_values(d / "before.tsv")
    if before["classic_fwhm_combined"] >= 6 or max(before[k] for k in ("classic_fwhm_x", "classic_fwhm_y", "classic_fwhm_z")) >= 6:
        write_json(d / "review_required.json", {"status": "at_or_above_target", "before": before,
                   "reason": "Smoothing cannot undo existing blur; no image smoothed"})
        raise ValueError("Baseline classic FWHM at/above target; inspect review_required.json")
    command(["bash", ROOT / "vendor/afni/smooth_to_target.sh", "--input", source, "--mask", mask,
             "--output", bold, "--target", "6", "--qc-tsv", d / "after.tsv"], log)
    after = measure_values(d / "after.tsv")
    image_info(bold, tr=job["tr"])
    grid(bold, plan["config"]["reference"])
    if nib.load(bold).shape[3] != job["nvol"]:
        raise ValueError("Smoothing changed volume count")
    rec = {"unit": job["unit"], "created": now(), "source_inputs": job["inputs"],
           "method": "default-blurmaster-classic6-v1", "target_mm": 6, "bmall": False,
           "before": before, "after": after, "geometry": geometry, "tools": plan["tools"],
           "status": "within_tolerance" if 5.4 <= after["classic_fwhm_combined"] <= 6.6 else "outside_tolerance",
           "products": [stamp(p, full=True) for p in (bold, mask, d / "before.tsv", d / "after.tsv")],
           "seed_input": stamp(source, full=job["dataset"] == "istart")}
    write_json(d / "receipt.json", rec, exclusive=True)
    return verified_spatial(job, plan)


def matrix(path):
    text = Path(path).read_text()
    if "/Matrix" not in text:
        raise ValueError("Missing FSL matrix")
    values = np.loadtxt(io.StringIO(text.split("/Matrix", 1)[1]), ndmin=2)
    if not values.size or not np.isfinite(values).all():
        raise ValueError("Empty/nonfinite FSL matrix")
    return values


def validate_design(mat, con, job):
    x, c = matrix(mat), matrix(con)
    expected = PPI_CONTRASTS if job.get("model") == "ppi" else CONTRASTS
    width = expected.shape[1]
    if x.shape[0] != job["nvol"] or c.shape != (len(expected), x.shape[1]) or x.shape[1] < width:
        raise ValueError("Unexpected design/contrast dimensions")
    if not np.allclose(c[:, :width], expected, rtol=0, atol=1e-8) or np.any(c[:, width:] != 0):
        raise ValueError("Contrasts differ from the authoritative activation/PPI model and ordering")
    # Optional absent miss EV and constant confounds may be rank deficient.
    # Scientific contrasts must nevertheless be estimable with residual degrees of freedom.
    rank = np.linalg.matrix_rank(np.column_stack((np.ones(len(x)), x)))
    if rank >= len(x) or not np.allclose(c @ np.linalg.pinv(x) @ x, c, atol=1e-6, rtol=1e-6):
        raise ValueError("Unestimable scientific contrast or no residual degrees of freedom")
    return {"columns": int(x.shape[1]), "rank_with_mean": int(rank), "residual_dof_unwhitened": int(len(x) - rank)}


def fsf_values(text):
    return dict(re.findall(r'^set\s+(\S+)\s+(.+?)\s*$', text, re.M))


def validate_fsf(path, job):
    fields = fsf_values(Path(path).read_text())
    expected = {"fmri(smooth)": "0", "fmri(tr)": str(job["tr"]), "fmri(temphp_yn)": "0",
                "fmri(templp_yn)": "0", "fmri(prewhiten_yn)": "1", "fmri(ndelete)": "0"}
    for k, v in expected.items():
        if fields.get(k, "").strip('"') != v:
            raise ValueError(f"Unexpected FSF setting {k}")
    for k, v in {"feat_files(1)": str(spatial_paths(job)[1]), "confoundev_files(1)": job["confounds"],
                 "fmri(outputdir)": job["output"]}.items():
        if fields.get(k, "").strip('"') != v:
            raise ValueError(f"Unexpected FSF linkage {k}")
    if job.get("model") == "ppi":
        if fields.get("fmri(custom5)", "").strip('"') != job["phys"]:
            raise ValueError("PPI physiological EV does not match its extracted seed series")
        if fields.get("fmri(evs_orig)") != "9" or fields.get("fmri(ncon_orig)") != "5":
            raise ValueError("Unexpected seed PPI model dimensions")


def render(job, plan, private):
    verify_job(job)
    verified_spatial(job, plan)
    if job.get("model") == "ppi":
        from .ppi import seed_series
        seed_series(job, plan)
    d = Path(private) / "designs" / job["unit"]
    d.mkdir(parents=True, exist_ok=True)
    fsf = d / "design.fsf"
    model = "ppi" if job.get("model") == "ppi" else "act"
    text = (ROOT / f"templates/L1_task-{job['task']}_model-1_type-{model}.fsf").read_text()
    # Tcl quotes must not permit variable or command substitution in paths.
    replacements = {"OUTPUT": job["output"], "DATA": str(spatial_paths(job)[1]),
                    "CONFOUNDEVS": job["confounds"], "NVOLUMES": str(job["nvol"]),
                    "TR_SECONDS": str(job["tr"]), "SHAPE_MISSED_TRIAL": "3" if job["missed"] else "10",
                    "FSL_STANDARD": str(Path(os.environ["FSLDIR"]) / "data/standard/MNI152_T1_2mm_brain")}
    if job.get("model") == "ppi":
        replacements["PHYS"] = job["phys"]
    for value in replacements.values():
        if any(c in value for c in '\\"$[]\n\r'):
            raise ValueError("Unsafe Tcl path characters")
    for name in ("win", "loss", "decision", "decision-missed"):
        text = text.replace("EVDIR_" + name + ".txt", job["evs"][name])
    for name, value in replacements.items():
        text = text.replace(name, value)
    if re.search(r"EVDIR|NVOLUMES|TR_SECONDS|CONFOUNDEVS|SHAPE_MISSED_TRIAL|FSL_STANDARD|PHYS", text):
        raise ValueError("Unresolved template token")
    if fsf.exists() and fsf.read_text() != text:
        raise ValueError("Rendered FSF changed; preserved")
    if not fsf.exists():
        fsf.write_text(text)
    validate_fsf(fsf, job)
    command(["feat_model", fsf.with_suffix(""), job["confounds"]], d / "feat_model.log")
    result = validate_design(fsf.with_suffix(".mat"), fsf.with_suffix(".con"), job)
    write_json(d / "preflight.json", {"unit": job["unit"], "fsf": stamp(fsf), "design": result,
                                     "products": [stamp(p) for p in (fsf.with_suffix(".mat"), fsf.with_suffix(".con"))]})
    return fsf


def output_products(job, plan):
    output = Path(job["output"])
    validate_fsf(output / "design.fsf", job)
    validate_design(output / "design.mat", output / "design.con", job)
    products = [output / x for x in ("design.fsf", "design.mat", "design.con", "mask.nii.gz", "mean_func.nii.gz")]
    image_info(output / "mask.nii.gz", mask=True)
    for kind in ("cope", "varcope", "zstat"):
        for i in range(1, 6 if job.get("model") == "ppi" else 5):
            p = output / "stats" / f"{kind}{i}.nii.gz"
            img = nib.load(p)
            if len(img.shape) != 3:
                raise ValueError(f"Statistical map is not 3D: {p}")
            values = np.asanyarray(img.dataobj)
            if not np.isfinite(values).all() or (kind == "varcope" and (values < 0).any()):
                raise ValueError(f"Invalid statistical map: {p}")
            grid(p, plan["config"]["reference"])
            products.append(p)
    grid(output / "mask.nii.gz", plan["config"]["reference"])
    dof = float((output / "stats/dof").read_text().strip())
    if not np.isfinite(dof) or dof <= 0:
        raise ValueError("FEAT residual degrees of freedom invalid")
    products.append(output / "stats/dof")
    return products


def complete(job, plan, plan_hash):
    verify_job(job)
    verified_spatial(job, plan)
    if job.get("model") == "ppi":
        from .ppi import verify_series
        verify_series(job, plan)
    rec = json.loads((Path(job["output"]) / "harmonized_success.json").read_text())
    if rec["plan_sha256"] != plan_hash or rec["unit"] != job["unit"]:
        raise ValueError("Existing FEAT output belongs to another plan")
    verify(rec["products"] + rec["provenance"])
    output_products(job, plan)
    return rec


def run_l1(job, plan, private, plan_hash):
    out = Path(job["output"])
    if out.exists():
        return complete(job, plan, plan_hash)
    fsf = render(job, plan, private)
    out.parent.mkdir(parents=True, exist_ok=True)
    command(["feat", fsf], Path(private) / "logs" / (job["unit"] + ".log"))
    products = output_products(job, plan)
    rec = {"plan_sha256": plan_hash, "unit": job["unit"], "finished": now(),
           "products": [stamp(p, full=True) for p in products],
           "provenance": job["inputs"] + job["prepared"] + [stamp(fsf), stamp(Path(job["spatial"]) / "receipt.json")],
           "versions": plan["tools"], "visual_qc": "pending", "scientific_inclusion": "not_assigned"}
    if job.get("model") == "ppi":
        rec["provenance"] += [stamp(job["phys"]), stamp(Path(job["phys"]).with_suffix(".json"))]
        rec.update(model="ppi", seed=job["seed"], seed_priority=job["seed_priority"])
    write_json(out / "harmonized_success.json", rec, exclusive=True)
    return complete(job, plan, plan_hash)


def selected(plan, stage):
    if stage == "pilot":
        return [j for j in plan["jobs"] if j["unit"] in plan["pilot"]]
    return plan["jobs"]


def pilot_gate(private, plan):
    path = Path(private) / "pilot_review.json"
    plan_hash = sha(Path(private) / "plan.json")
    expected = 4 * len(plan["seeds"]) if plan.get("model") == "ppi" else 4
    if len(plan["pilot"]) != expected:
        raise ValueError("Both datasets and both tasks require verified pilot models")
    if path.exists():
        review = json.loads(path.read_text())
        if review["plan_sha256"] != plan_hash or not review["reviewer"] or not review["notes"]:
            raise ValueError("Existing pilot-review record differs from this plan")
    for job in selected(plan, "pilot"):
        complete(job, plan, plan_hash)


def batch(private, operation, stage="pilot", workers=1):
    private = Path(private).absolute()
    plan = load_plan(private)
    if stage == "cohort":
        pilot_gate(private, plan)
    jobs = selected(plan, stage)
    if not jobs:
        raise ValueError("No selected runs")
    if operation in ("spatial", "run"):
        # Conservative working-space estimate, checked per destination filesystem.
        required = {}
        for job in jobs:
            dest = Path(job["spatial"] if operation == "spatial" else job["output"])
            if dest.exists():
                continue
            parent = dest.parent
            while not parent.exists():
                parent = parent.parent
            device = parent.stat().st_dev
            factor = (3 if job["dataset"] == "istart" else 2) if operation == "spatial" else 8
            estimated = factor * 4 * job["spatial_voxels"] * job["nvol"]
            old = required.get(device, (parent, 0))
            required[device] = (parent, old[1] + estimated)
        for parent, estimated in required.values():
            if shutil.disk_usage(parent).free < estimated * 1.1:
                raise ValueError(f"Insufficient estimated working space on {parent}: need approximately {estimated / 1e9:.1f} GB")
    results = []
    with lock(private / "batch.lock"):
        # Variant-root locks also serialize independent plans targeting these outputs.
        from contextlib import ExitStack
        with ExitStack() as stack:
            roots = sorted({str(Path(j["output"]).parents[2 if j["session"] else 1]) for j in plan["jobs"]})
            for r in roots:
                stack.enter_context(lock(Path(r) / ".harmonized.lock"))
            plan_hash = sha(private / "plan.json")
            def work(job):
                try:
                    if operation == "spatial":
                        spatial(job, plan)
                    elif operation == "render":
                        render(job, plan, private)
                    elif operation == "run":
                        run_l1(job, plan, private, plan_hash)
                    else:
                        complete(job, plan, plan_hash)
                    return {"unit": job["unit"], "dataset": job["dataset"], "task": job["task"], "status": "verified", "reason": ""}
                except Exception as exc:
                    return {"unit": job["unit"], "dataset": job["dataset"], "task": job["task"], "status": "review_or_failure", "reason": str(exc)}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(work, j): j for j in jobs}
                for f in as_completed(futures):
                    r = f.result()
                    results.append(r)
                    print(r["unit"], r["status"], r["reason"], flush=True)
    results.sort(key=lambda r: r["unit"])
    table(private / f"{operation}_{stage}.tsv", results)
    aggregate = [{"dataset": d, "task": t, "status": s, "runs": n}
                 for (d, t, s), n in sorted(Counter((r["dataset"], r["task"], r["status"]) for r in results).items())]
    table(private / f"{operation}_{stage}_summary.tsv", aggregate)
    write_json(private / f"{operation}_{stage}_validation.json", {"time": now(), "plan_sha256": plan_hash,
               "selected": len(jobs), "verified": sum(r["status"] == "verified" for r in results),
               "all_selected_verified": all(r["status"] == "verified" for r in results), "visual_qc": "not_assigned"})
    return all(r["status"] == "verified" for r in results)


def accept_pilot(private, reviewer, notes):
    plan = load_plan(private)
    plan_hash = sha(Path(private) / "plan.json")
    expected = 4 * len(plan["seeds"]) if plan.get("model") == "ppi" else 4
    if len(plan["pilot"]) != expected or not reviewer.strip() or not notes.strip():
        raise ValueError("Both dataset/task combinations and explicit reviewer/notes required")
    for job in selected(plan, "pilot"):
        complete(job, plan, plan_hash)
    write_json(Path(private) / "pilot_review.json", {"plan_sha256": plan_hash, "reviewer": reviewer,
               "notes": notes, "time": now(), "scope": "technical pilot acceptance; scientific inclusion unchanged"}, exclusive=True)
