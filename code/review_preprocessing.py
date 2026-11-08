#!/usr/bin/env python3
"""Read preprocessing metadata and events; never edit images or source events."""
from __future__ import annotations

from collections import Counter
import csv
import json
import math
import os
from pathlib import Path
import shutil

import nibabel as nib

from harmonized.common import ROOT, config, now, read_table, write_json
from harmonized.preprocess import inventory


def image_header(path):
    image = nib.load(path)
    zooms = image.header.get_zooms()
    scale = {"sec": 1, "msec": .001, "usec": .000001}.get(image.header.get_xyzt_units()[1])
    return {"filename": path.name, "shape": list(image.shape), "voxel_sizes": list(map(float, zooms[:3])),
            "tr_s": float(zooms[3]) * scale if len(zooms) > 3 and scale else None,
            "orientation": list(nib.aff2axcodes(image.affine))}


def event_summary(path):
    result = {"filename": path.name, "exists": path.is_file()}
    if not path.is_file():
        return result
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        fields = reader.fieldnames or []
        counts, malformed, ends = Counter(), 0, []
        for row in reader:
            label = row.get("trial_type", "")
            counts[label] += 1
            try:
                onset, duration = float(row["onset"]), float(row["duration"])
                if not math.isfinite(onset) or not math.isfinite(duration) or duration < 0:
                    raise ValueError("invalid timing")
                ends.append(onset + duration)
            except (ValueError, TypeError, KeyError):
                malformed += 1
    result.update(fields=fields, rows=sum(counts.values()), trial_type_counts=dict(counts), malformed_timing_rows=malformed,
                  last_event_end_s=max(ends) if ends else None)
    return result


def confound_summary(path):
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        fields = reader.fieldnames or []
        n, fd = 0, []
        for row in reader:
            n += 1
            value = row.get("framewise_displacement", "")
            if value not in ("", "n/a", "nan", "NaN"):
                value = float(value)
                if math.isfinite(value):
                    fd.append(value)
    return {"filename": path.name, "rows": n,
            "six_aCompCor_present": all(f"a_comp_cor_{i:02}" in fields for i in range(6)),
            "fd_mean_mm": sum(fd) / len(fd) if fd else None, "fd_max_mm": max(fd) if fd else None}


def review(c):
    root = Path(c["istart_root"])
    raw_inventory = inventory(c)
    by_key = {(row["subject"], row["task"]): row for row in raw_inventory}
    _, roster = read_table(root / "bids/participants.tsv")
    saved = {}
    batches = []
    for path in sorted((root / "work/preprocessing-runs").glob("*/results.tsv")):
        _, records = read_table(path)
        batches.append(path.parent.name)
        for row in records:
            saved.setdefault(row["subject"], []).append({"batch": path.parent.name, **row})
    records = []
    for person in roster:
        sub = person["participant_id"]
        receipt_path = root / f"work/preprocessing/{sub}.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
        subject = {"subject": sub, "batch_results": saved.get(sub, []),
                   "command_status": receipt.get("status"), "version": receipt.get("version"),
                   "report_exists": (root / f"derivatives/fmriprep/{sub}.html").is_file(),
                   "T1w_files": [p.name for p in sorted((root / f"bids/{sub}/anat").glob("*_T1w.nii.gz"))], "tasks": []}
        for task in ("doors", "socialdoors"):
            raw = root / f"bids/{sub}/func"
            func = root / f"derivatives/fmriprep/{sub}/func"
            item = {"task": task, "raw_inventory": by_key.get((sub, task)),
                    "raw_bold": [], "derivative_bold": [], "confounds": [],
                    "raw_events": event_summary(raw / f"{sub}_task-{task}_run-1_events.tsv")}
            for field, folder, pattern in (("raw_bold", raw, f"*_task-{task}_*bold.nii.gz"),
                                           ("derivative_bold", func, f"*_task-{task}_*desc-preproc_bold.nii.gz")):
                for path in sorted(folder.glob(pattern)):
                    try:
                        item[field].append(image_header(path))
                    except Exception as exc:
                        item[field].append({"filename": path.name, "error": str(exc)})
            for path in sorted(func.glob(f"*_task-{task}_*desc-confounds_timeseries.tsv")):
                try:
                    item["confounds"].append(confound_summary(path))
                except Exception as exc:
                    item["confounds"].append({"filename": path.name, "error": str(exc)})
            subject["tasks"].append(item)
        records.append(subject)
    reference = Path(c["reference"])
    environment = {"executables": {name: shutil.which(name) for name in ("feat", "feat_model", "fslmeants", "3dBlurToFWHM", "3dFWHMx", "3dAllineate", "3dresample")},
                   "FSLDIR": os.environ.get("FSLDIR"), "reference_exists": reference.is_file(),
                   "rf1_root_exists": Path(c["rf1_root"]).is_dir(), "rf1_analysis_exists": Path(c["rf1_analysis"]).is_dir()}
    if reference.is_file():
        environment["reference"] = image_header(reference)
    report = {"created": now(), "participants": len(records), "batches": batches,
              "raw_input_status_counts": dict(Counter(row["status"] for row in raw_inventory)),
              "subjects_not_attempted": [row["subject"] for row in records if not row["batch_results"]],
              "failed_subjects": sorted({row["subject"] for row in records if any(r["status"] == "failed" for r in row["batch_results"])}),
              "environment": environment, "subjects": records, "images_modified": False, "source_events_modified": False,
              "scientific_inclusion_assigned": False, "review_scope": "header, confound and event metadata; previous structural receipts remain distinct from visual QC"}
    report["mni_bold_files"] = sum("space-MNI152NLin6Asym" in bold["filename"] for subject in records for task in subject["tasks"] for bold in task["derivative_bold"])
    report["ready_raw_tasks_without_mni_bold"] = [{"subject": subject["subject"], "task": task["task"]} for subject in records for task in subject["tasks"]
        if task["raw_inventory"] and task["raw_inventory"]["status"] == "ready" and not any("space-MNI152NLin6Asym" in bold["filename"] for bold in task["derivative_bold"])]
    write_json(root / "work/postpreproc-review.json", report)
    print(json.dumps({key: value for key, value in report.items() if key != "subjects"}, indent=2))
    return report


if __name__ == "__main__":
    review(config(ROOT / "config/linux2.json"))
