import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest

from harmonized import environment as tool_environment, ppi
from harmonized.common import ROOT, stamp, write_json
from test_ppi import seed_assets
from test_workflow_synthetic import environment, simulated_commands

spec = importlib.util.spec_from_file_location("l1_runner", ROOT / "code/run_harmonized_l1.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
spec = importlib.util.spec_from_file_location("preproc", ROOT / "code/preprocess_istart.py")
preproc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preproc)


def test_existing_afni_is_discovered_from_rf1_plan(tmp_path, monkeypatch):
    afni, fsl = tmp_path / "existing-afni", tmp_path / "existing-fsl"
    afni.mkdir()
    (fsl / "bin").mkdir(parents=True)
    for parent, names in ((afni, tool_environment.AFNI), (fsl / "bin", ("feat", "feat_model", "fslmeants"))):
        for name in names:
            path = parent / name
            path.write_text("existing executable identity\n")
            path.chmod(0o755)
    c = {"istart_root": str(tmp_path / "istart"), "rf1_root": str(tmp_path / "rf1"), "evaluation": str(tmp_path / "evaluation")}
    write_json(Path(c["rf1_root"]) / "work/cohort-smoothness-v1/plan.json", {"tools": {"afni": {"path": str(afni / "afni")}}})
    monkeypatch.setenv("FSLDIR", str(fsl))
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("FSLOUTPUTTYPE", "NIFTI_GZ")
    result = tool_environment.configure(c)
    assert result["AFNI_directory"] == str(afni)
    assert result["executables"]["fslmeants"] == str(fsl / "bin/fslmeants")
    assert json.loads((Path(c["istart_root"]) / "work/l1-tools.json").read_text())["AFNI_directory"] == str(afni)


def test_single_available_task_is_not_a_failed_subject(environment):
    c, cfg, legacy = environment
    root = Path(c["istart_root"])
    sub = "sub-1001"
    source = next((root / f"derivatives/fmriprep/{sub}/func").glob("*task-doors*desc-preproc_bold.nii.gz"))
    shutil.copyfile(source, root / f"bids/{sub}/func/{sub}_task-doors_run-1_bold.nii.gz")
    Path(c["container"]).write_bytes(b"synthetic container identity")
    (root / f"derivatives/fmriprep/{sub}.html").write_text("synthetic report")
    write_json(root / f"work/preprocessing/{sub}.json", {"status": "command_succeeded", "bids_inputs": [], "container": stamp(c["container"])})
    result = preproc.check_subject(c, sub)
    assert [r["status"] for r in result["checks"]] == ["structural_checks_passed", "raw_task_unavailable"]


def test_detached_worker_orchestrates_activation_and_ppi(environment, simulated_commands, tmp_path, monkeypatch):
    c, cfg, legacy = environment
    seed_assets(c)
    executable = tmp_path / "fslmeants"
    executable.write_text("synthetic executable identity")
    monkeypatch.setattr(ppi.shutil, "which", lambda name: str(executable))
    monkeypatch.setattr(runner, "configure", lambda config: {})
    root = Path(c["istart_root"])
    folder = root / "work/l1-runs/pilot"
    write_json(folder / "spec.json", {"phase": "pilot", "jobs": 2, "config": str(cfg),
        "activation_plan": str(root / "work/harmonized-l1-v1"), "ppi_plan": str(root / "work/harmonized-ppi-v1")})
    write_json(folder / "state.json", {"phase": "pilot", "status": "started", "pid": os.getpid()})
    assert runner.worker(c, "pilot") == 0
    state = json.loads((folder / "state.json").read_text())
    assert state["status"] == "complete" and len(state["operations"]) == 5
    activation = json.loads((root / "work/harmonized-l1-v1/pilot_summary.json").read_text())
    connectivity = json.loads((root / "work/harmonized-ppi-v1/pilot_summary.json").read_text())
    assert len(activation["runs"]) == 4 and len(connectivity["runs"]) == 24
    assert all(row["success_receipt_present"] for row in activation["runs"] + connectivity["runs"])
    assert not (root / "work/harmonized-l1-v1/pilot_review.json").exists()
