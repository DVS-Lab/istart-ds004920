"""Read-only diagnostic of the corrected MCFLIRT reconstruction (fMRIPrep #3697)."""
from __future__ import annotations

import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pandas as pd
from scipy import ndimage as ndi
from scipy.spatial.transform import Rotation

from .common import MOTION, now, read_table, stamp, table, verify, write_json

FIX = "3e5b08b163fd9cf25bb62df9fc1b427bdec6c15b"


def from_fsl(matrices, boldref):
    """Equation-identical to upstream FSLMotionParams at FIX; mm and radians."""
    data = boldref.get_fdata()
    if np.linalg.det(boldref.affine) > 0:
        data = data[::-1]
    if not np.isfinite(data).all() or np.sum(data - data.min()) <= 0:
        raise ValueError("Motion reference has no valid intensity-weighted centre of gravity")
    center = np.diag(boldref.header.get_zooms()[:3]) @ ndi.center_of_mass(data - data.min())
    m, b = matrices[:, :3, :3], matrices[:, :3, 3]
    if not np.allclose(m @ m.transpose(0, 2, 1), np.eye(3), atol=1e-4, rtol=0) or not np.allclose(np.linalg.det(m), 1, atol=1e-4, rtol=0):
        raise ValueError("Original motion matrices must be rigid transforms")
    trans = m @ center + b - center
    rot = -Rotation.from_matrix(m).as_euler("XYZ")
    return np.hstack((trans, rot))


def reconstruct(xfm, reference):
    import nitransforms as nt
    boldref = nib.load(reference)
    hmc = nt.linear.load(xfm)
    fsl = nt.io.fsl.FSLLinearTransformArray.from_ras(hmc.matrix, reference=boldref, moving=boldref)
    matrices = np.stack([x["parameters"] for x in fsl.xforms])
    return from_fsl(matrices, boldref)


def fd(values):
    diff = np.abs(np.diff(values, axis=0))
    return np.r_[np.nan, diff[:, :3].sum(axis=1) + 50 * diff[:, 3:].sum(axis=1)]


def select(plan, output, count=8):
    if not 5 <= count <= 10:
        raise ValueError("Diagnostic sample must contain 5–10 runs")
    candidates = []
    # Includes technically ready and held runs with available confounds, to avoid a low-motion-only sample.
    rf1 = Path(plan["config"]["rf1_root"])
    for path in sorted((rf1 / "derivatives/fmriprep").glob("sub-*/ses-*/func/*desc-confounds_timeseries.tsv")):
        from .common import entities
        e = entities(path)
        if e.get("task") not in ("doors", "socialdoors"):
            continue
        f = pd.read_csv(path, sep="\t")
        values = f[MOTION].to_numpy(dtype=float)
        if not len(values) or not np.isfinite(values).all():
            continue
        unit = path.name.replace("_desc-confounds_timeseries.tsv", "")
        candidates.append({"unit": unit, "source_confounds": str(path),
                           "translation_range_mm": float(np.ptp(values[:, :3], axis=0).max()),
                           "rotation_range_rad": float(np.ptp(values[:, 3:], axis=0).max()),
                           "max_exported_fd_mm": float(f["framewise_displacement"].max()),
                           "hmc_transform": "", "hmc_reference": "", "original_mcflirt_par": "",
                           "reference_evidence": "", "status": "supply_original_motion_transform_and_exact_hmc_reference"})
    chosen = []
    ranked = [sorted(candidates, key=lambda r: -r[k]) for k in ("translation_range_mm", "rotation_range_rad", "max_exported_fd_mm")]
    for i in range(len(candidates)):
        for rank in ranked:
            if rank[i] not in chosen:
                chosen.append(rank[i])
            if len(chosen) == count:
                break
        if len(chosen) == count:
            break
    if len(chosen) < 5:
        raise ValueError("Fewer than five suitable RF1 motion series available")
    table(output, chosen)
    print(f"Selected {len(chosen)} high-motion RF1 runs. Fill exact original HMC transform/reference paths and reference_evidence in {output}.")


def compare(mapping, output, thresholds):
    """Thresholds are explicit existing QC thresholds, never guessed or changed."""
    if not thresholds or any(not np.isfinite(t) or t <= 0 for t in thresholds):
        raise ValueError("Supply the existing positive FD thresholds explicitly")
    _, rows = read_table(mapping)
    if not 5 <= len(rows) <= 10 or len({r["unit"] for r in rows}) != len(rows):
        raise ValueError("Use 5–10 unique mapped runs")
    output = Path(output)
    if output.exists():
        raise ValueError("Use a new diagnostic output directory; prior diagnostics are preserved")
    output.mkdir(parents=True)
    summaries, missing = [], []
    provenance = [stamp(mapping)]
    for row in rows:
        try:
            if not row.get("hmc_transform") or not row.get("hmc_reference") or not row.get("reference_evidence"):
                raise ValueError("Original transforms/exact HMC reference not mapped; no substitute reference is allowed")
            inputs = [stamp(row[k], full=True) for k in ("source_confounds", "hmc_transform", "hmc_reference")]
            if any(r.get("absent") for r in inputs):
                raise ValueError("Required original motion input absent")
            exported = pd.read_csv(row["source_confounds"], sep="\t")
            old = exported[MOTION].to_numpy(dtype=float)
            corrected = reconstruct(row["hmc_transform"], row["hmc_reference"])
            if old.shape != corrected.shape or not np.isfinite(old).all() or not np.isfinite(corrected).all():
                raise ValueError("Motion series mismatch/nonfinite values")
            old_fd, new_fd = fd(old), fd(corrected)
            exported_fd = exported["framewise_displacement"].to_numpy(dtype=float)
            if not np.isnan(exported_fd[0]) or not np.isfinite(exported_fd[1:]).all():
                raise ValueError("Exported FD must have undefined first frame and finite later frames")
            delta = corrected - old
            # Include common trends plus nuisance subspace change; FD counts alone cannot quantify GLM effects.
            a = np.column_stack((np.ones(len(old)), old, np.nan_to_num(exported_fd)))
            b = np.column_stack((np.ones(len(old)), corrected, np.nan_to_num(new_fd)))
            qa = np.linalg.svd(a, full_matrices=False)[0][:, :np.linalg.matrix_rank(a)]
            qb = np.linalg.svd(b, full_matrices=False)[0][:, :np.linalg.matrix_rank(b)]
            # Frobenius norm of projector difference, without allocating T x T projectors.
            projector_distance = float(np.sqrt(max(0, qa.shape[1] + qb.shape[1] - 2 * np.linalg.norm(qa.T @ qb, "fro") ** 2)))
            summary = {"unit": row["unit"], "volumes": len(old), "fd_max_abs_delta_mm": float(np.nanmax(np.abs(new_fd - exported_fd))),
                       "fd_mean_abs_delta_mm": float(np.nanmean(np.abs(new_fd - exported_fd))),
                       "existing_fd_reconstruction_max_error": float(np.nanmax(np.abs(old_fd - exported_fd))),
                       "motion_fd_subspace_projector_frobenius": projector_distance,
                       "reference_evidence": row["reference_evidence"], "status": "compared", "materiality": "review_required"}
            for i, name in enumerate(MOTION):
                summary[name + "_max_abs_delta"] = float(np.max(np.abs(delta[:, i])))
                summary[name + "_rms_delta"] = float(np.sqrt(np.mean(delta[:, i] ** 2)))
            for threshold in thresholds:
                label = f"fd_gt_{threshold:g}"
                summary[label + "_old_count"] = int(np.sum(exported_fd > threshold))
                summary[label + "_corrected_count"] = int(np.sum(new_fd > threshold))
                summary[label + "_classification_changes"] = int(np.sum((exported_fd[1:] > threshold) != (new_fd[1:] > threshold)))
            if row.get("original_mcflirt_par"):
                truth = np.loadtxt(row["original_mcflirt_par"], ndmin=2)
                if truth.shape != old.shape:
                    raise ValueError("Original MCFLIRT .par dimensions differ")
                truth = truth[:, [3, 4, 5, 0, 1, 2]]
                if not np.allclose(corrected[:, :3], truth[:, :3], atol=1e-3, rtol=0) or not np.allclose(corrected[:, 3:], truth[:, 3:], atol=1e-5, rtol=0):
                    raise ValueError("Corrected reconstruction does not match original MCFLIRT .par; inspect mapping")
                inputs.append(stamp(row["original_mcflirt_par"], full=True))
                summary["ground_truth"] = "matches_original_mcflirt_par"
            frames = pd.DataFrame({**{f"old_{name}": old[:, i] for i, name in enumerate(MOTION)},
                                   **{f"corrected_{name}": corrected[:, i] for i, name in enumerate(MOTION)},
                                   "exported_fd": exported_fd, "old_recomputed_fd": old_fd, "corrected_fd": new_fd})
            frames.to_csv(output / (row["unit"] + ".tsv"), sep="\t", index=False, na_rep="n/a")
            verify(inputs)
            provenance.extend(inputs)
            summaries.append(summary)
        except (ValueError, OSError, KeyError) as exc:
            missing.append({"unit": row["unit"], "status": "unavailable_or_invalid", "reason": str(exc)})
    table(output / "comparisons.tsv", summaries)
    table(output / "unavailable.tsv", missing)
    write_json(output / "provenance.json", {"fix_commit": FIX, "time": now(), "inputs": provenance,
               "existing_fd_thresholds_mm": thresholds, "fd_head_radius_mm": 50, "derivatives_modified": False})
    lines = ["# High-motion reconstruction diagnostic", "", f"Compared {len(summaries)} of {len(rows)} selected RF1 runs; unavailable/invalid: {len(missing)}.", "",
             "See comparisons.tsv for parameter discrepancies (translations in mm; rotations in radians), FD changes and threshold classification counts.",
             "Motion+FD nuisance-subspace differences are descriptive; deciding material effects on scientific COPEs requires an explicit sensitivity analysis.",
             "No QC decision, production nuisance matrix, derivative or preprocessing version was changed.", "",
             "Numerical evidence and materiality require review on Linux2; missing transforms are reported rather than approximated.",
             "", "Correction: https://github.com/nipreps/fmriprep/pull/3697"]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    return not missing
