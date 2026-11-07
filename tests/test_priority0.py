import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harmonized import preprocess as pre
from harmonized.common import ROOT, stamp, write_json

spec = importlib.util.spec_from_file_location("priority0", ROOT / "code/preprocess_istart.py")
p0 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p0)


def test_fetch_pilot_subset_then_extend_without_touching_pilot(tmp_path, monkeypatch):
    c = {"istart_root": str(tmp_path)}
    source = tmp_path / "sourcedata/ds004920"
    source.mkdir(parents=True)
    (source / "participants.tsv").write_text("participant_id\nsub-1001\nsub-1002\n")
    (source / "dataset_description.json").write_text('{"Name":"test","BIDSVersion":"1.8.0"}')
    for subject in ("sub-1001", "sub-1002"):
        for folder in ("anat", "func", "fmap"):
            (source / subject / folder).mkdir(parents=True)
        for name in (f"{subject}/anat/{subject}_T1w.nii.gz",
                     f"{subject}/func/{subject}_task-doors_run-1_bold.nii.gz",
                     f"{subject}/func/{subject}_task-socialdoors_run-1_bold.nii.gz",
                     f"{subject}/func/{subject}_task-mid_run-1_bold.nii.gz",
                     f"{subject}/fmap/{subject}_acq-bold_magnitude1.nii.gz"):
            (source / name).write_bytes(b"test payload")
        write_json(source / f"{subject}/fmap/{subject}_acq-bold_phasediff.json", {
            "IntendedFor": [f"func/{subject}_task-doors_run-1_bold.nii.gz", f"func/{subject}_task-mid_run-1_bold.nii.gz"]})
    tracked = [str(p.relative_to(source)) for p in source.rglob("*") if p.is_file()]
    calls = []
    def command(argv, *args, **kwargs):
        argv = list(map(str, argv))
        calls.append(argv)
        if "rev-parse" in argv:
            return pre.SNAPSHOT
        if "status" in argv:
            return ""
        if "ls-files" in argv:
            return "\n".join(tracked)
        return ""
    monkeypatch.setattr(pre, "command", command)
    monkeypatch.setattr(pre, "inventory", lambda c: [])
    pre.fetch(c, ["1002"])
    pilot = tmp_path / "bids/sub-1002/func/sub-1002_task-doors_run-1_bold.nii.gz"
    saved = stamp(pilot, full=True)
    assert not (tmp_path / "bids/sub-1001").exists()
    assert not list((tmp_path / "bids").rglob("*task-mid*nii.gz"))
    gets = [a for a in calls if a[:2] == ["datalad", "get"]]
    assert gets and all(not any("sub-1001" in p or "task-mid" in p for p in a) for a in gets)
    fmap = json.loads((tmp_path / "bids/sub-1002/fmap/sub-1002_acq-bold_phasediff.json").read_text())
    assert len(fmap["IntendedFor"]) == 1
    assert len(json.loads((source / "sub-1002/fmap/sub-1002_acq-bold_phasediff.json").read_text())["IntendedFor"]) == 2
    pre.fetch(c)
    assert (tmp_path / "bids/sub-1001").exists()
    assert stamp(pilot, full=True) == saved


@pytest.mark.parametrize("memory_budget", [24000, 0])
def test_pinned_preprocess_dry_run_has_required_flags(tmp_path, monkeypatch, memory_budget):
    container = tmp_path / "fmriprep-25.2.5.simg"
    container.write_bytes(b"fake test container")
    (tmp_path / "bids/sub-1002").mkdir(parents=True)
    write_json(tmp_path / "bids/dataset_description.json", {})
    (tmp_path / "bids/participants.tsv").write_text("participant_id\nsub-1002\n")
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    (licenses / "fs_license.txt").write_text("synthetic test license fixture\n")
    c = {"istart_root": str(tmp_path), "container": str(container), "licenses_dir": str(licenses)}
    monkeypatch.setattr(pre, "inventory", lambda c: [{"subject": "sub-1002", "status": "ready"}])
    monkeypatch.setattr(pre.shutil, "which", lambda name: "/fake/apptainer")
    monkeypatch.setattr(pre, "command", lambda *a, **k: "fMRIPrep v25.2.5")
    pre.preprocess(c, ["1002"], 16, 2, memory_budget, False)
    plans = json.loads((tmp_path / "work/preprocessing_plan_sub-1002.json").read_text())
    cmd = plans[0]["command"]
    assert "--fs-no-reconall" in cmd and "--cifti-output" not in cmd
    assert cmd[cmd.index("--fs-license-file") + 1] == "/opts/fs_license.txt"
    assert f"{licenses}:/opts" in cmd
    assert plans[0]["license"]["path"] == str(licenses / "fs_license.txt")
    assert "--use-syn-sdc" not in cmd and "--ignore" not in cmd
    assert cmd[cmd.index("--output-spaces") + 1] == "MNI152NLin6Asym"
    assert cmd[-1].endswith("work/fmriprep/sub-1002")
    if memory_budget:
        assert cmd[cmd.index("--mem") + 1] == "24000"
    else:
        assert "--mem" not in cmd and "--mem-mb" not in cmd
    filters = json.loads((tmp_path / "work/bids-filter-doors.json").read_text())
    assert filters == {"bold": {"task": ["doors", "socialdoors"]}}


def test_wrong_container_version_is_rejected(tmp_path, monkeypatch):
    container = tmp_path / "fmriprep-25.2.5.simg"
    container.write_bytes(b"fake")
    monkeypatch.setattr(pre.shutil, "which", lambda name: "/fake/apptainer")
    monkeypatch.setattr(pre, "command", lambda *a, **k: "fMRIPrep v25.2.7")
    with pytest.raises(ValueError, match="Wrong fMRIPrep"):
        pre.preprocess({"istart_root": str(tmp_path), "container": str(container)}, ["1002"], 16, 2, 24000)


def test_missing_production_license_fails_before_workflow_launch(tmp_path, monkeypatch):
    container = tmp_path / "fmriprep-25.2.5.simg"
    container.write_bytes(b"fake")
    monkeypatch.setattr(pre.shutil, "which", lambda name: "/fake/apptainer")
    calls = []
    def command(argv, *args, **kwargs):
        calls.append(argv)
        return "fMRIPrep v25.2.5"
    monkeypatch.setattr(pre, "command", command)
    with pytest.raises(ValueError, match="FreeSurfer license"):
        pre.preprocess({"istart_root": str(tmp_path), "container": str(container),
                        "licenses_dir": str(tmp_path / "missing")}, ["1002"], 16, 2, 24000)
    assert len(calls) == 1 and calls[0][-1] == "--version"


def test_launch_is_detached_records_pid_and_deduplicates(tmp_path, monkeypatch):
    c = {"istart_root": str(tmp_path)}
    cfg = tmp_path / "config.json"
    write_json(cfg, c)
    calls = []
    monkeypatch.setattr(p0, "resources", lambda *args: None)
    def popen(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(pid=12345)
    monkeypatch.setattr(p0.subprocess, "Popen", popen)
    p0.launch(c, cfg, "pilot", ["1002"], 1, 16, 2, 24000)
    state = json.loads((tmp_path / "work/preprocessing-runs/pilot/state.json").read_text())
    assert state["pid"] == 12345 and state["log"].endswith("preprocessing-pilot.log")
    assert calls[0][1]["start_new_session"] is True
    monkeypatch.setattr(p0, "alive", lambda s: True)
    p0.launch(c, cfg, "pilot", ["1002"], 1, 16, 2, 24000)
    assert len(calls) == 1


def test_cohort_cannot_launch_without_pilot_review(tmp_path, monkeypatch):
    monkeypatch.setattr(p0, "resources", lambda *args: None)
    c = {"istart_root": str(tmp_path)}
    cfg = tmp_path / "c.json"
    write_json(cfg, c)
    with pytest.raises(FileNotFoundError):
        p0.launch(c, cfg, "cohort", None, 2, 16, 2, 24000, True)


def test_license_restart_preserves_failed_records_and_work_cache(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("license_restart", ROOT / "code/restart_pilot_after_license_fix.py")
    restart = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restart)
    batch = tmp_path / "work/preprocessing-runs/pilot"
    write_json(batch / "state.json", {"pid": 99999, "status": "finished_with_failures"})
    receipt = tmp_path / "work/preprocessing/sub-1002.json"
    write_json(receipt, {"status": "planned", "command": ["original-failed-command"]})
    cached = tmp_path / "work/fmriprep/sub-1002/cached-node.txt"
    cached.parent.mkdir(parents=True)
    cached.write_text("preserve cached work")
    before = stamp(cached)
    monkeypatch.setattr(restart.os, "kill", lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    archive = restart.archive_failed_metadata(tmp_path)
    assert not batch.exists()
    assert json.loads((archive / "batch/state.json").read_text())["pid"] == 99999
    assert (archive / "sub-1002-before-license-fix.json").read_text() == receipt.read_text()
    assert stamp(cached) == before


def test_license_restart_refuses_live_pilot(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("license_restart_live", ROOT / "code/restart_pilot_after_license_fix.py")
    restart = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restart)
    batch = tmp_path / "work/preprocessing-runs/pilot"
    write_json(batch / "state.json", {"pid": 99999})
    monkeypatch.setattr(restart.os, "kill", lambda *args: None)
    with pytest.raises(RuntimeError, match="still alive"):
        restart.archive_failed_metadata(tmp_path)
    assert (batch / "state.json").exists()


def test_larger_cohort_obeys_cpu_and_memory_limits(monkeypatch):
    monkeypatch.setattr(p0, "resource_snapshot", lambda: {"cpus": 96, "available_memory_mb": 240000, "load_average": [4, 4, 4]})
    assert p0.resources(6, 12, 2, 24000)["cpus"] == 96
    with pytest.raises(ValueError, match="processes"):
        p0.resources(9, 12, 2, 24000)
    with pytest.raises(ValueError, match="currently available"):
        p0.resources(8, 12, 2, 30001)
    with pytest.raises(ValueError, match="thread"):
        p0.resources(6, 0, 2, 24000)


def test_capacity_reserves_cpu_and_memory_and_detects_active_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(p0, "resource_snapshot", lambda: {"cpus": 96, "available_memory_mb": 120000, "load_average": [4, 4, 4]})
    c = {"istart_root": str(tmp_path)}
    report = p0.capacity(c, 12, 2, 24000)
    assert report["recommended_jobs"] == 4  # 80% of 120 GB, despite ample CPU.
    write_json(tmp_path / "work/preprocessing-runs/old/state.json", {"pid": 12345})
    monkeypatch.setattr(p0, "alive", lambda state: True)
    report = p0.capacity(c, 12, 2, 24000)
    assert report["recommended_jobs"] == 0 and report["active_batches"][0]["name"] == "old"


def test_larger_launch_refuses_overlapping_managed_batch(tmp_path, monkeypatch):
    c = {"istart_root": str(tmp_path)}
    cfg = tmp_path / "config.json"
    write_json(cfg, c)
    write_json(tmp_path / "work/preprocessing-runs/other/state.json", {"pid": 12345})
    monkeypatch.setattr(p0, "alive", lambda state: True)
    with pytest.raises(ValueError, match="Another managed preprocessing batch"):
        p0.launch(c, cfg, "pilot", ["1002"], 6, 12, 2, 24000)


def test_ten_subject_user_policy_does_not_gate_on_ram(tmp_path, monkeypatch):
    monkeypatch.setattr(p0, "resource_snapshot", lambda: {"cpus": 128, "available_memory_mb": 120337, "load_average": [4, 4, 4]})
    assert p0.resources(10, 4, 2, 0)["available_memory_mb"] == 120337
    report = p0.capacity({"istart_root": str(tmp_path)}, 4, 2, 0, max_jobs=10)
    assert report["recommended_jobs"] == 10
    monkeypatch.setattr(p0, "resource_snapshot", lambda: {"cpus": 128, "available_memory_mb": 100, "load_average": [4, 4, 4]})
    assert p0.resources(10, 4, 2, 0)["available_memory_mb"] == 100
