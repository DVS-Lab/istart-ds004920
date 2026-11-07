from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .common import (BASE, ROOT, TASKS, VARIANT, code_stamps, entities, image_info,
                     key, now, read_table, sha, stamp, table, tools_snapshot,
                     unique, verify, write_json)


def base_confounds(source, nvol):
    fields, _ = read_table(source)
    if not set(BASE) <= set(fields):
        raise ValueError("Missing required base confounds: " + ",".join(sorted(set(BASE) - set(fields))))
    columns = BASE + [c for c in fields if c.startswith("cosine")] + [c for c in fields if c.startswith("non_steady_state")]
    frame = pd.read_csv(source, sep="\t")[columns].apply(pd.to_numeric, errors="raise").fillna(0)
    values = frame.to_numpy(dtype=float)
    if values.shape != (nvol, len(columns)) or not np.isfinite(values).all():
        raise ValueError(f"Confounds must be finite with exactly {nvol} rows")
    return values, columns


def events(source, duration):
    fields, rows = read_table(source)
    if not {"onset", "duration", "trial_type"} <= set(fields):
        raise ValueError("Events require onset, duration, trial_type")
    result = {k: [] for k in ("win", "loss", "decision", "decision-missed")}
    ignored = set()
    for row in rows:
        onset, length = float(row["onset"]), float(row["duration"])
        if not np.isfinite([onset, length]).all() or length < 0:
            raise ValueError("Malformed onset or duration")
        if onset >= duration or onset + length > duration + 1e-3:
            raise ValueError("Event extends beyond the BOLD acquisition")
        label = row["trial_type"]
        if label in result:
            result[label].append([onset, length, 1.0])
        else:
            ignored.add(label)
    if any(not result[k] for k in ("win", "loss", "decision")):
        raise ValueError("Missing required win/loss/decision events; no automatic model change")
    return result, sorted(ignored)


def policy(c):
    """Reuse settled RF1 roster/source/hold/disposition rules; QC never selects."""
    rf1 = Path(c["rf1_root"])
    required = [rf1 / "bids/participants.tsv", rf1 / "code/task_availability_holds.tsv", rf1 / "qc/run_qc.tsv"]
    for p in required:
        if not p.is_file():
            raise ValueError(f"Required RF1 policy input unavailable: {p}")
    exclusions_root = Path(c["source_exclusions"])
    if not exclusions_root.is_dir():
        raise ValueError("RF1 source-exclusion root unavailable")
    excluded = sorted(p.name.removeprefix("Smith-SRA-") for p in exclusions_root.glob("Smith-SRA-*") if p.is_dir())
    maps = {}
    for name, path in (("holds", required[1]), ("qc", required[2]),
                       ("rf1_dispositions", Path(c["rf1_dispositions"])),
                       ("istart_dispositions", Path(c["istart_dispositions"]))):
        maps[name] = {}
        if not path.exists():
            if name == "rf1_dispositions" and (rf1 / "qc/run_disposition.tsv").exists():
                raise ValueError("RF1 live dispositions exist; configuration must select them explicitly")
            continue
        fields, rows = read_table(path)
        if not {"subject", "session", "task", "run"} <= set(fields):
            raise ValueError(f"Policy file requires run key: {path}")
        for row in rows:
            k = key(row)
            if k in maps[name]:
                raise ValueError(f"Duplicate policy run: {path}: {k}")
            if name.endswith("dispositions"):
                if row.get("decision") not in ("include", "exclude", "review", "unavailable") or not row.get("reason") or not row.get("evidence"):
                    raise ValueError(f"Invalid disposition: {path}: {k}")
            maps[name][k] = row
    sources = required + [Path(c[k]) for k in ("rf1_dispositions", "istart_dispositions")]
    return maps, excluded, [stamp(p) for p in sources]


def inventory(c, maps, excluded):
    rows = []
    for dataset in ("istart", "rf1"):
        upstream = Path(c[dataset + "_root"])
        bids = upstream / "bids"
        _, roster = read_table(bids / "participants.tsv")
        subjects = [r["participant_id"].removeprefix("sub-") for r in roster]
        if not subjects or len(set(subjects)) != len(subjects):
            raise ValueError(f"Empty or duplicate roster: {dataset}")
        for sub in sorted(subjects):
            for session in ([""] if dataset == "istart" else c["rf1_sessions"]):
                relative = Path("sub-" + sub) / ("ses-" + session if session else "") / "func"
                for task in TASKS:
                    row = dict(dataset=dataset, subject=sub, session=session, task=task, run="1", status="candidate", reason="")
                    stem = f"sub-{sub}" + (f"_ses-{session}" if session else "") + f"_task-{task}_run-1"
                    row["unit"] = dataset + "_" + stem
                    k = key(row)
                    decision = maps[dataset + "_dispositions"].get(k, {})
                    hold = maps["holds"].get(k, {}) if dataset == "rf1" else {}
                    row["qc"] = json.dumps(maps["qc"].get(k, {}) if dataset == "rf1" else {}, sort_keys=True)
                    if (dataset == "rf1" and sub in excluded) or hold.get("reason") or decision.get("decision") in ("exclude", "review", "unavailable"):
                        row.update(status="held", reason=hold.get("reason") or decision.get("reason") or "existing_source_exclusion")
                    else:
                        try:
                            func = upstream / "derivatives/fmriprep" / relative
                            candidates = [p for p in func.glob(f"{stem}_*space-MNI152NLin6Asym*_desc-preproc_bold.nii.gz") if "echo" not in entities(p)]
                            bold = unique(candidates, "unsmoothed non-echo fMRIPrep MNI BOLD")
                            mask = bold.with_name(bold.name.replace("desc-preproc_bold", "desc-brain_mask"))
                            if not mask.is_file():
                                raise ValueError(f"Corresponding MNI mask missing: {mask}")
                            conf = unique(func.glob(f"{stem}_*desc-confounds_timeseries.tsv"), "fMRIPrep confounds")
                            event = bids / relative / (stem + "_events.tsv")
                            if not event.is_file():
                                raise ValueError(f"Events missing: {event}")
                            row.update(bold=str(bold), mask=str(mask), source_confounds=str(conf), events=str(event), stem=stem,
                                       tr=1.75 if dataset == "istart" else 1.615)
                        except (ValueError, OSError) as exc:
                            row.update(status="missing_or_invalid", reason=str(exc))
                    rows.append(row)
    return rows


def prepare(c, config_path, plan_dir):
    private = Path(plan_dir).absolute()
    if private.exists():
        raise ValueError("Plan directory already exists; resume it or choose a new variant plan")
    maps, excluded, policies = policy(c)
    rows = inventory(c, maps, excluded)
    reference = Path(c["reference"])
    if not reference.is_file():
        raise ValueError("RF1 reference grid unavailable")
    tools = tools_snapshot(["afni", "3dBlurToFWHM", "3dFWHMx", "3dAllineate", "3dresample", "feat", "feat_model"])
    # Both datasets must be preprocessing version 25.2.5.
    for dataset in ("istart", "rf1"):
        desc = Path(c[dataset + "_root"]) / "derivatives/fmriprep/dataset_description.json"
        generated = json.loads(desc.read_text()).get("GeneratedBy", [])
        if not any(g.get("Name", "").lower() == "fmriprep" and g.get("Version") == "25.2.5" for g in generated):
            raise ValueError(f"{dataset} derivatives are not documented as fMRIPrep 25.2.5")
        policies.append(stamp(desc))
        policies.append(stamp(Path(c[dataset + "_root"]) / "bids/participants.tsv"))
    stage = Path(c["istart_root"]) / "work/bids-stage.json"
    staged = json.loads(stage.read_text())
    if staged["snapshot"] != "2913a29de224f5364629cc39ac26045c06817b4f":
        raise ValueError("iSTART staged source snapshot differs")
    policies.append(stamp(stage))
    private.mkdir(parents=True, mode=0o700)
    jobs = []
    for row in rows:
        if row["status"] != "candidate":
            continue
        try:
            bold = image_info(row["bold"], tr=row["tr"])
            image_info(row["mask"], mask=True)
            from .common import grid
            grid(row["mask"], row["bold"])
            values, columns = base_confounds(row["source_confounds"], bold.shape[3])
            evs, ignored = events(row["events"], bold.shape[3] * row["tr"])
            folder = private / "inputs" / row["unit"]
            folder.mkdir(parents=True)
            confounds = folder / "baseonly.tsv"
            np.savetxt(confounds, values, delimiter="\t", fmt="%.17g")
            ev_paths = {}
            for kind, data in evs.items():
                ev_paths[kind] = str(folder / (kind + ".txt"))
                # Missing nuisance EV is represented by shape 10, not invented trials.
                np.savetxt(ev_paths[kind], np.array(data).reshape(-1, 3), delimiter="\t", fmt="%.17g")
            analysis = Path(c["istart_root"] if row["dataset"] == "istart" else c["rf1_analysis"])
            relative = Path("sub-" + row["subject"]) / ("ses-" + row["session"] if row["session"] else "")
            output = analysis / "derivatives" / VARIANT / relative / (row["stem"] + "_model-1_type-act.feat")
            spatial = analysis / "derivatives/harmonized-fwhm6-baseonly-v1" / relative / row["stem"]
            job = dict(row, output=str(output), spatial=str(spatial), confounds=str(confounds), evs=ev_paths,
                       missed=bool(evs["decision-missed"]), ignored_trial_types=ignored, columns=columns,
                       nvol=int(bold.shape[3]), spatial_voxels=int(np.prod(bold.shape[:3])),
                       inputs=[stamp(row[k]) for k in ("bold", "mask", "source_confounds", "events")],
                       prepared=[stamp(p) for p in [confounds, *map(Path, ev_paths.values())]])
            jobs.append(job)
            row.update(status="ready", reason="", nuisance_columns=len(columns), nvol=bold.shape[3], ignored_trial_types=",".join(ignored))
        except (ValueError, OSError, KeyError) as exc:
            row.update(status="missing_or_invalid", reason=str(exc))
    table(private / "run_review.tsv", rows)
    pilots = []
    for dataset in ("istart", "rf1"):
        for task in TASKS:
            choices = [j for j in jobs if j["dataset"] == dataset and j["task"] == task]
            if choices:
                # Prefer an observed miss EV to exercise the optional nuisance branch.
                pilots.append(sorted(choices, key=lambda j: (not j["missed"], j["unit"]))[0]["unit"])
    plan = {"schema": 1, "created": now(), "variant": VARIANT, "config": c,
            "sources": code_stamps() + policies + [stamp(config_path), stamp(reference, full=True), stamp(private / "run_review.tsv")],
            "source_exclusions": excluded, "tools": tools, "pilot": pilots, "jobs": jobs,
            "qc_used_for_selection": False, "scientific_inclusion_assigned": False}
    write_json(private / "plan.json", plan, exclusive=True)
    (private / "plan.sha256").write_text(sha(private / "plan.json") + "\n")
    table(private / "pilot.tsv", [{"unit": u} for u in pilots])
    print(f"Prepared {len(jobs)} technically ready runs; {len(rows) - len(jobs)} held/missing; {len(pilots)} pilot runs")
    if not jobs or len(pilots) != 4:
        raise ValueError("A representative pilot requires both datasets and both tasks; inspect run_review.tsv")


def load_plan(private):
    private = Path(private).absolute()
    if sha(private / "plan.json") != (private / "plan.sha256").read_text().strip():
        raise ValueError("Frozen plan changed")
    plan = json.loads((private / "plan.json").read_text())
    if plan["variant"] != VARIANT or plan["qc_used_for_selection"] is not False:
        raise ValueError("Unexpected analysis variant/policy")
    verify(plan["sources"])
    _, exclusions, _ = policy(plan["config"])
    if exclusions != plan["source_exclusions"]:
        raise ValueError("Source exclusions changed; existing outputs preserved")
    if tools_snapshot(["afni", "3dBlurToFWHM", "3dFWHMx", "3dAllineate", "3dresample", "feat", "feat_model"]) != plan["tools"]:
        raise ValueError("Software identity changed; make an isolated new plan")
    return plan
