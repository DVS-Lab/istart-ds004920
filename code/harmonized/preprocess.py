"""Pinned metadata checkout, restricted BIDS staging, and participant fMRIPrep."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil

from .common import (ROOT, TASKS, command, entities, image_info, lock, now,
                     read_table, sha, stamp, table, unique, verify, write_json)

SNAPSHOT = "2913a29de224f5364629cc39ac26045c06817b4f"


def fetch(c, subjects=None):
    root = Path(c["istart_root"])
    source = root / "sourcedata/ds004920"
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        command(["datalad", "clone", "https://github.com/OpenNeuroDatasets/ds004920.git", source],
                root / "logs/download.log")
        command(["git", "-C", source, "checkout", "--detach", SNAPSHOT], root / "logs/download.log")
    if command(["git", "-C", source, "rev-parse", "HEAD"]) != SNAPSHOT:
        raise ValueError("Existing OpenNeuro checkout is not the pinned 1.1.1 commit; preserved")
    if command(["git", "-C", source, "status", "--porcelain", "--untracked-files=no"]):
        raise ValueError("Pinned OpenNeuro tracked files were modified; inspect first")
    tracked = command(["git", "-C", source, "ls-files"]).splitlines()
    wanted = {"sub-" + s.removeprefix("sub-") for s in subjects} if subjects else None
    known = {s.split("/")[0] for s in tracked if s.startswith("sub-")}
    if wanted is not None and not wanted <= known:
        raise ValueError("Requested participant is absent from the pinned snapshot")
    selected = []
    for rel in tracked:
        p = Path(rel)
        if not rel.startswith("sub-"):
            if p.suffix in (".json", ".tsv") or p.name in ("README", "README.md", "CHANGES", "LICENSE"):
                selected.append(rel)
            continue
        if wanted is not None and p.parts[0] not in wanted:
            continue
        if "/ses-" in rel:
            raise ValueError("ds004920 unexpectedly contains sessions")
        if "/anat/" in rel and re.search(r"_T1w\.(nii\.gz|json)$", rel):
            selected.append(rel)
        elif "/fmap/" in rel and re.search(r"_(phasediff|magnitude1|magnitude2)\.(nii\.gz|json)$", rel):
            selected.append(rel)
        elif "/func/" in rel and entities(p).get("task") in TASKS and re.search(r"_(bold\.(nii\.gz|json)|events\.(tsv|json)|sbref\.(nii\.gz|json))$", rel):
            if "echo" in entities(p):
                raise ValueError("iSTART input unexpectedly has an echo entity")
            selected.append(rel)
    if not any("_bold.nii.gz" in s for s in selected):
        raise ValueError("No selected BOLD files")
    # Fetch only explicitly selected anatomy, GRE, two tasks and their metadata.
    for start in range(0, len(selected), 100):
        command(["datalad", "get", "--", *selected[start:start + 100]], root / "logs/download.log", cwd=source)
    stage = root / "bids"
    receipt_path = root / "work/bids-stage.json"
    existing = {}
    mappings = []
    if stage.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt["snapshot"] != SNAPSHOT:
            raise ValueError("Existing staged BIDS snapshot differs")
        verify(receipt["files"])
        existing = {str(Path(s["path"]).relative_to(stage)): s for s in receipt["files"]}
        mappings = receipt["fieldmap_association_edits"]
    else:
        stage.mkdir(parents=True)
    for rel in selected:
        if rel in existing:
            continue
        src, dest = source / rel, stage / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".json":
            data = json.loads(src.read_text())
            if "IntendedFor" in data:
                original = data["IntendedFor"]
                values = [original] if isinstance(original, str) else original
                keep = [v for v in values if entities(Path(v)).get("task") in TASKS]
                data["IntendedFor"] = keep
                mappings.append({"file": rel, "original_IntendedFor": original, "staged_IntendedFor": keep})
            write_json(dest, data, exclusive=True)
        elif src.name.endswith(".nii.gz"):
            # A copy keeps raw snapshot objects and staged data independent.
            shutil.copyfile(src.resolve(strict=True), dest)
        else:
            shutil.copyfile(src, dest)
    all_files = sorted(set(existing) | set(selected))
    receipt = {"snapshot": SNAPSHOT, "version": "1.1.1", "created": now(),
               "selected_files": all_files, "fieldmap_association_edits": mappings,
               "files": [existing[p] if p in existing else stamp(stage / p, full=True) for p in all_files]}
    write_json(receipt_path, receipt)
    inventory(c)
    print(f"Staged {len(all_files)} files from ds004920 1.1.1; source snapshot unchanged")


def inventory(c):
    root = Path(c["istart_root"])
    bids = root / "bids"
    _, roster = read_table(bids / "participants.tsv")
    rows = []
    for person in roster:
        sub = person["participant_id"]
        if not re.fullmatch(r"sub-[a-zA-Z0-9]+", sub):
            raise ValueError("Invalid participant ID")
        folder = bids / sub
        for task in TASKS:
            row = {"subject": sub, "task": task, "status": "ready", "reason": ""}
            try:
                unique((folder / "anat").glob("*_T1w.nii.gz"), "T1w")
                bolds = list((folder / "func").glob(f"*_task-{task}_*bold.nii.gz"))
                bold = unique(bolds, f"{task} single-echo BOLD")
                meta = json.loads(bold.with_name(bold.name.replace(".nii.gz", ".json")).read_text())
                if abs(float(meta["RepetitionTime"]) - 1.75) > 1e-6 or "echo" in entities(bold):
                    raise ValueError("iSTART requires single-echo TR 1.75 s")
                gre = list((folder / "fmap").glob("*_phasediff.json"))
                associated = []
                for fmap in gre:
                    d = json.loads(fmap.read_text())
                    intended = d.get("IntendedFor", [])
                    if isinstance(intended, str):
                        intended = [intended]
                    if any(str(v).split("func/")[-1] == bold.name for v in intended):
                        if not 0 < float(d["EchoTime1"]) < float(d["EchoTime2"]):
                            raise ValueError("Invalid GRE echo times")
                        for suffix in ("phasediff", "magnitude1", "magnitude2"):
                            p = fmap.with_name(fmap.name.replace("phasediff.json", suffix + ".nii.gz"))
                            if not p.is_file():
                                raise ValueError(f"GRE input absent: {p}")
                        associated.append(fmap)
                unique(associated, "GRE association")
            except (ValueError, OSError, KeyError) as exc:
                row.update(status="missing_or_invalid", reason=str(exc))
            rows.append(row)
    table(root / "work/preprocessing_inventory.tsv", rows)
    print(json.dumps({s: sum(r["status"] == s for r in rows) for s in {r["status"] for r in rows}}))
    return rows


def preprocess(c, subjects, nprocs, omp, mem_mb, execute=False):
    root, container = Path(c["istart_root"]), Path(c["container"])
    if not container.is_file():
        raise ValueError(f"Pinned production container unavailable: {container}")
    runtime = shutil.which("apptainer") or shutil.which("singularity")
    if not runtime:
        raise ValueError("Apptainer or Singularity required")
    version = command([runtime, "run", "--cleanenv", container, "--version"])
    if not re.search(r"(?<![0-9])25\.2\.5(?![0-9a-zA-Z.+-])", version):
        raise ValueError(f"Wrong fMRIPrep version: {version}")
    # Reuse rf1-sra-linux2/code/fmriprep.sh and pipeline_common.sh verbatim:
    # LICENSES_DIR=/ZPOOL/data/tools/licenses, bound at /opts in the container.
    licenses = Path(c.get("licenses_dir", "/ZPOOL/data/tools/licenses"))
    license_file = licenses / "fs_license.txt"
    if not license_file.is_file() or not os.access(license_file, os.R_OK) or license_file.stat().st_size == 0:
        raise ValueError(f"Existing Linux2 FreeSurfer license is missing, unreadable or empty: {license_file}")
    rows = inventory(c)
    ready = {r["subject"] for r in rows if r["status"] == "ready"}
    requested = ["sub-" + s.removeprefix("sub-") for s in subjects] if subjects else sorted(ready)
    if not requested or not set(requested) <= ready:
        raise ValueError("No requested subjects, or subject lacks valid preprocessing inputs")
    filters = root / "work/bids-filter-doors.json"
    write_json(filters, {"bold": {"task": list(TASKS)}})
    cache = root / "work/templateflow"
    cache.mkdir(parents=True, exist_ok=True)
    deriv = root / "derivatives/fmriprep"
    work = root / "work/fmriprep"
    deriv.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    plans = []
    for subject in requested:
        cmd = [runtime, "run", "--cleanenv", "--bind", f"{root}:{root}",
               "--bind", f"{licenses}:/opts",
               "--env", f"TEMPLATEFLOW_HOME={cache}", container,
               root / "bids", deriv, "participant", "--participant-label", subject[4:],
               "--bids-filter-file", filters, "--fs-no-reconall", "--fs-license-file", "/opts/fs_license.txt",
               "--output-spaces", "MNI152NLin6Asym",
               "--nprocs", str(nprocs), "--omp-nthreads", str(omp), "--mem-mb", str(mem_mb),
               "--notrack", "--stop-on-first-crash", "-w", work / subject]
        # 25.2.5 accepts --mem, documented CLI; do not depend on deprecated spelling.
        cmd[cmd.index("--mem-mb")] = "--mem"
        if mem_mb == 0:
            # Explicit user-selected policy: no per-subject memory scheduling budget.
            pos = cmd.index("--mem")
            del cmd[pos:pos + 2]
        subject_inputs = [root / "bids/dataset_description.json", root / "bids/participants.tsv"]
        subject_inputs += sorted(p for p in (root / "bids" / subject).rglob("*") if p.is_file())
        rec = {"subject": subject, "command": list(map(str, cmd)), "version": version,
               "container": stamp(container, full=True), "bids_inputs": [stamp(p) for p in subject_inputs],
               "license": stamp(license_file),
               "filter": stamp(filters), "started": now(), "status": "planned"}
        plans.append(rec)
        if execute:
            with lock(root / f"work/preprocessing/{subject}.lock"):
                receipt = root / f"work/preprocessing/{subject}.json"
                if receipt.exists():
                    old = json.loads(receipt.read_text())
                    if old["container"] != rec["container"] or old["bids_inputs"] != rec["bids_inputs"]:
                        raise ValueError("Existing preprocessing belongs to different inputs; inspect")
                write_json(receipt, rec)
                command(cmd, root / f"logs/fmriprep/{subject}.log")
                verify(rec["bids_inputs"] + [rec["container"], rec["license"]])
                rec.update(status="command_succeeded", finished=now())
                write_json(receipt, rec)
        else:
            import shlex
            print(shlex.join(list(map(str, cmd))))
    label = requested[0] if len(requested) == 1 else "all"
    write_json(root / f"work/preprocessing_plan_{label}.json", plans)


def check_preproc(c):
    root = Path(c["istart_root"])
    rows = inventory(c)
    out = []
    for row in rows:
        result = dict(row)
        try:
            if row["status"] != "ready":
                raise ValueError(row["reason"])
            folder = root / "derivatives/fmriprep" / row["subject"] / "func"
            bold = unique(folder.glob(f"*_task-{row['task']}_*space-MNI152NLin6Asym*_desc-preproc_bold.nii.gz"), "preprocessed BOLD")
            image_info(bold, tr=1.75)
            report = root / "derivatives/fmriprep" / (row["subject"] + ".html")
            receipt = json.loads((root / f"work/preprocessing/{row['subject']}.json").read_text())
            if not report.is_file() or receipt["status"] != "command_succeeded":
                raise ValueError("Preprocessing HTML report or successful command receipt missing")
            result.update(status="structural_checks_passed_visual_qc_pending", bold=str(bold), report=str(report))
        except (ValueError, OSError, KeyError) as exc:
            result.update(status="missing_or_invalid", reason=str(exc))
        out.append(result)
    table(root / "work/preprocessing_completion.tsv", out)
    print(json.dumps({s: sum(r["status"] == s for r in out) for s in {r["status"] for r in out}}))
    return all(r["status"] != "missing_or_invalid" for r in out)
