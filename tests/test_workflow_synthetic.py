"""Synthetic orchestration test: external AFNI/FSL commands are simulated explicitly."""
import json
from pathlib import Path
import shutil

import nibabel as nib
import numpy as np
import pandas as pd
import pytest

from harmonized import common, inputs, workflow
from harmonized.common import BASE, CONTRASTS, PPI_CONTRASTS, VARIANT, read_table, stamp, write_json
from harmonized.export import export
from test_scientific_contracts import confound_file, write_matrix


@pytest.fixture
def environment(tmp_path, monkeypatch):
    c = {k: str(tmp_path / k) for k in ("istart_root", "rf1_root", "rf1_analysis", "source_exclusions", "evaluation")}
    c.update(reference=str(tmp_path / "ref.nii.gz"), container=str(tmp_path / "25.2.5.simg"), rf1_sessions=["01"],
             rf1_dispositions=str(tmp_path / "rf1_root/qc/run_disposition.tsv"),
             istart_dispositions=str(tmp_path / "istart_root/config/run_disposition.tsv"))
    Path(c["source_exclusions"]).mkdir()
    affine = np.diag([-2., 2, 2, 1])
    nib.save(nib.Nifti1Image(np.ones((3, 4, 5)), affine), c["reference"])
    for dataset, sub, ses, tr in (("istart", "1001", "", 1.75), ("rf1", "10317", "01", 1.615)):
        root = Path(c[dataset + "_root"])
        (root / "bids").mkdir(parents=True)
        (root / "bids/participants.tsv").write_text("participant_id\nsub-" + sub + "\n")
        write_json(root / "derivatives/fmriprep/dataset_description.json", {"GeneratedBy": [{"Name": "fMRIPrep", "Version": "25.2.5"}]})
        for task in ("doors", "socialdoors"):
            relative = Path("sub-" + sub) / ("ses-" + ses if ses else "") / "func"
            raw, deriv = root / "bids" / relative, root / "derivatives/fmriprep" / relative
            raw.mkdir(parents=True, exist_ok=True)
            deriv.mkdir(parents=True, exist_ok=True)
            stem = f"sub-{sub}" + (f"_ses-{ses}" if ses else "") + f"_task-{task}_run-1"
            image = nib.Nifti1Image(np.random.default_rng(7).normal(size=(3, 4, 5, 80)).astype(np.float32) + 100, affine)
            image.header.set_xyzt_units("mm", "sec")
            image.header.set_zooms((2, 2, 2, tr))
            nib.save(image, deriv / (stem + "_space-MNI152NLin6Asym_desc-preproc_bold.nii.gz"))
            nib.save(nib.Nifti1Image(np.ones((3, 4, 5), dtype=np.uint8), affine), deriv / (stem + "_space-MNI152NLin6Asym_desc-brain_mask.nii.gz"))
            confound_file(deriv / (stem + "_desc-confounds_timeseries.tsv"))
            (raw / (stem + "_events.tsv")).write_text("onset\tduration\ttrial_type\n1\t3\tdecision\n6\t1\twin\n20\t1\tloss\n")
    rf1 = Path(c["rf1_root"])
    (rf1 / "code").mkdir()
    (rf1 / "qc").mkdir()
    (rf1 / "code/task_availability_holds.tsv").write_text("subject\tsession\ttask\trun\treason\tevidence\n")
    (rf1 / "qc/run_qc.tsv").write_text("subject\tsession\ttask\trun\tqc_status\n10317\t01\tdoors\t1\toutlier\n")
    write_json(Path(c["istart_root"]) / "work/bids-stage.json", {"snapshot": "2913a29de224f5364629cc39ac26045c06817b4f"})
    legacy = Path(c["rf1_analysis"]) / "derivatives/fsl/legacy.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("preserve historical outputs\n")
    monkeypatch.setenv("FSLDIR", str(tmp_path / "fsl"))
    monkeypatch.setattr(inputs, "tools_snapshot", lambda names: {"simulated": True})
    config = tmp_path / "config.json"
    write_json(config, c)
    return c, config, legacy


@pytest.fixture
def simulated_commands(monkeypatch):
    calls = []
    def fake(argv, log=None, env=None, cwd=None):
        argv = list(map(str, argv))
        calls.append(argv)
        if "resample_to_rf1_grid.sh" in argv[1]:
            shutil.copyfile(argv[argv.index("--input") + 1], argv[argv.index("--output") + 1])
        elif "measure_smoothness.sh" in argv[1] or "smooth_to_target.sh" in argv[1]:
            smooth = "smooth_to_target.sh" in argv[1]
            if smooth:
                shutil.copyfile(argv[argv.index("--input") + 1], argv[argv.index("--output") + 1])
            dest = argv[argv.index("--qc-tsv" if smooth else "--output-tsv") + 1]
            v = 5.85 if smooth else 3.5
            common.table(dest, [dict(classic_fwhm_x=v, classic_fwhm_y=v, classic_fwhm_z=v, classic_fwhm_combined=v,
                                     acf_a=.6, acf_b=3., acf_c=10., acf_effective_fwhm=8.5)])
        elif argv[0] == "feat_model":
            root = Path(argv[1])
            nuisance = np.loadtxt(argv[2], ndmin=2)
            fields = workflow.fsf_values(root.with_suffix(".fsf").read_text())
            ppi = fields["fmri(evs_orig)"] == "9"
            expected = PPI_CONTRASTS if ppi else CONTRASTS
            science = np.random.default_rng(33).normal(size=(len(nuisance), expected.shape[1]))
            science[:, 3] = 0
            if ppi:
                science[:, 8] = 0
            x = np.column_stack((science, nuisance))
            write_matrix(root.with_suffix(".mat"), x)
            write_matrix(root.with_suffix(".con"), np.column_stack((expected, np.zeros((len(expected), nuisance.shape[1])))))
        elif argv[0] == "feat":
            fsf = Path(argv[1])
            vals = workflow.fsf_values(fsf.read_text())
            out = Path(vals["fmri(outputdir)"].strip('"'))
            out.mkdir()
            for suffix in (".fsf", ".mat", ".con"):
                shutil.copyfile(fsf.with_suffix(suffix), out / ("design" + suffix))
            img = nib.load(vals["feat_files(1)"].strip('"'))
            for name in ("mask.nii.gz", "mean_func.nii.gz"):
                nib.save(nib.Nifti1Image(np.ones(img.shape[:3], dtype=np.uint8), img.affine), out / name)
            (out / "stats").mkdir()
            for kind in ("cope", "varcope", "zstat"):
                for i in range(1, int(vals["fmri(ncon_real)"]) + 1):
                    nib.save(nib.Nifti1Image(np.ones(img.shape[:3]), img.affine), out / "stats" / f"{kind}{i}.nii.gz")
            (out / "stats/dof").write_text("60\n")
        elif argv[0] == "fslmeants":
            bold = nib.load(argv[argv.index("-i") + 1]).get_fdata()
            mask = nib.load(argv[argv.index("-m") + 1]).get_fdata() > 0
            np.savetxt(argv[argv.index("-o") + 1], bold[mask].mean(axis=0))
        else:
            raise AssertionError(argv)
    monkeypatch.setattr(workflow, "command", fake)
    return calls


def test_prepare_pilot_resume_export_and_preservation(environment, simulated_commands, tmp_path):
    c, config, legacy = environment
    before = common.sha(legacy)
    private = tmp_path / "plan"
    inputs.prepare(c, config, private)
    p = inputs.load_plan(private)
    assert len(p["jobs"]) == 4 and len(p["pilot"]) == 4
    # RF1's outlier QC flag stays visible and does not become an exclusion.
    assert "outlier" in next(j for j in p["jobs"] if j["dataset"] == "rf1" and j["task"] == "doors")["qc"]
    with pytest.raises(FileNotFoundError):
        workflow.batch(private, "spatial", "cohort")
    for op in ("spatial", "render", "run", "check"):
        assert workflow.batch(private, op, "pilot", workers=2)
    count = len(simulated_commands)
    assert workflow.batch(private, "run", "pilot", workers=2)
    assert len(simulated_commands) == count
    workflow.accept_pilot(private, "synthetic test", "Simulated outputs only; acceptance tests orchestration")
    assert workflow.batch(private, "check", "cohort")
    destination = Path(c["istart_root"]) / "derivatives/release-candidates/example"
    export(private, destination, [1, 2], ["cope"], execute=False)
    assert not destination.exists()
    export(private, destination, [1, 2], ["cope"], execute=True)
    assert len(list(destination.rglob("*.nii.gz"))) == 8
    assert common.sha(legacy) == before
    assert all("--all-blurmaster" not in a for a in simulated_commands)
    assert all(VARIANT in j["output"] for j in p["jobs"])


def test_existing_partial_spatial_output_is_never_overwritten(environment, simulated_commands, tmp_path):
    c, config, _ = environment
    private = tmp_path / "plan"
    inputs.prepare(c, config, private)
    plan = inputs.load_plan(private)
    job = plan["jobs"][0]
    Path(job["spatial"]).mkdir(parents=True)
    marker = Path(job["spatial"]) / "partial.txt"
    marker.write_text("keep")
    with pytest.raises(FileNotFoundError):
        workflow.spatial(job, plan)
    assert marker.read_text() == "keep" and not simulated_commands


def test_existing_source_exclusion_stays_held(environment, tmp_path):
    c, config, _ = environment
    (Path(c["source_exclusions"]) / "Smith-SRA-10317").mkdir()
    maps, excluded, _ = inputs.policy(c)
    rows = inputs.inventory(c, maps, excluded)
    assert all(r["status"] == "held" for r in rows if r["dataset"] == "rf1")


def test_mutated_evs_block_resume(environment, simulated_commands, tmp_path):
    c, config, _ = environment
    private = tmp_path / "plan"
    inputs.prepare(c, config, private)
    plan = inputs.load_plan(private)
    p = Path(plan["jobs"][0]["evs"]["win"])
    p.write_text("8\t1\t1\n")
    with pytest.raises(ValueError, match="Frozen input"):
        workflow.verify_job(plan["jobs"][0])
