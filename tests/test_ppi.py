import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from harmonized import common, inputs, ppi, workflow
from harmonized.common import PPI_CONTRASTS, ROOT, sha, write_json
from harmonized.export import export
from test_scientific_contracts import write_matrix
from test_workflow_synthetic import environment, simulated_commands


def seed_assets(c):
    masks = Path(c["istart_root"]) / "masks"
    masks.mkdir()
    reference = nib.load(c["reference"])
    names = ["mPFC_left", "mPFC_right", "hippo_left", "hippo_right", "IPL", "Fusiform"]
    rows = []
    for index, name in enumerate(names):
        data = np.zeros(reference.shape, dtype=np.uint8)
        data.reshape(-1)[index] = 1
        path = masks / f"seed-{name}.nii.gz"
        nib.save(nib.Nifti1Image(data, reference.affine), path)
        rows.append({"seed": name, "file": path.name, "sha256": sha(path)})
    write_json(masks / "seed_manifest.json", {"seeds": rows})


def test_authoritative_ppi_contrasts_and_interaction_settings():
    for task in ("doors", "socialdoors"):
        fields = workflow.fsf_values((ROOT / f"templates/L1_task-{task}_model-1_type-ppi.fsf").read_text())
        assert fields["fmri(smooth)"] == "0" and fields["fmri(tr)"] == "TR_SECONDS"
        assert fields["fmri(regstandard)"] == '"FSL_STANDARD"'
        assert fields["fmri(custom5)"] == '"PHYS"'
        for ev in range(1, 5):
            assert fields[f"fmri(convolve{ev})"] == "3"
            assert fields[f"fmri(interactions{ev+5}.{ev})"] == "1"
            assert fields[f"fmri(interactions{ev+5}.5)"] == "1"
            assert fields[f"fmri(interactionsd{ev+5}.5)"] == "2"
        actual = np.array([[float(fields[f"fmri(con_real{i}.{j})"]) for j in range(1, 10)] for i in range(1, 6)])
        assert np.array_equal(actual, PPI_CONTRASTS)


def test_ppi_design_rejects_activation_weights(tmp_path):
    rng = np.random.default_rng(1)
    design = rng.normal(size=(80, 22))
    contrasts = np.column_stack((PPI_CONTRASTS, np.zeros((5, 13))))
    mat, con = tmp_path / "design.mat", tmp_path / "design.con"
    write_matrix(mat, design)
    write_matrix(con, contrasts)
    assert workflow.validate_design(mat, con, {"nvol": 80, "model": "ppi"})["residual_dof_unwhitened"] > 0
    contrasts[0, 0], contrasts[0, 5] = 1, 0
    write_matrix(con, contrasts)
    with pytest.raises(ValueError, match="Contrasts"):
        workflow.validate_design(mat, con, {"nvol": 80, "model": "ppi"})


def test_ppi_pipeline_preserves_activation_and_all_seed_map_names(environment, simulated_commands, tmp_path, monkeypatch):
    c, cfg, legacy = environment
    seed_assets(c)
    executable = tmp_path / "fslmeants"
    executable.write_text("synthetic FSL executable identity")
    monkeypatch.setattr(ppi.shutil, "which", lambda name: str(executable))
    parent = tmp_path / "activation"
    inputs.prepare(c, cfg, parent, workers=2)
    for operation in ("spatial", "run", "check"):
        assert workflow.batch(parent, operation, "pilot", workers=2)
    activation = inputs.load_plan(parent)
    preserved = {job["output"]: sha(Path(job["output"]) / "stats/cope1.nii.gz") for job in activation["jobs"]}
    private = tmp_path / "ppi"
    plan = ppi.prepare(parent, private)
    assert len(plan["jobs"]) == len(plan["pilot"]) == 24
    assert sum(job["seed_priority"] == "primary" for job in plan["jobs"]) == 8
    assert not (private / "pilot_review.json").exists()
    for operation in ("run", "check"):
        assert workflow.batch(private, operation, "pilot", workers=2)
    # Cohort access requires verified pilots, not an invented human approval.
    assert workflow.batch(private, "check", "cohort", workers=2)
    for job in plan["jobs"]:
        assert (Path(job["output"]) / "stats/cope5.nii.gz").is_file()
    seed_calls = [call for call in simulated_commands if call[0] == "fslmeants"]
    assert len(seed_calls) == 24
    assert all("smoothToFWHM6" not in call[call.index("-i") + 1] for call in seed_calls)
    count = len(simulated_commands)
    assert workflow.batch(private, "run", "pilot", workers=2)
    assert len(simulated_commands) == count
    for output, expected in preserved.items():
        assert sha(Path(output) / "stats/cope1.nii.gz") == expected
    destination = Path(c["istart_root"]) / "derivatives/release-candidates/ppi"
    export(private, destination, [1, 5], ["cope"], execute=True)
    assert len(list(destination.rglob("*.nii.gz"))) == 48
    assert all("_seed-" in path.name for path in destination.rglob("*.nii.gz"))
    assert "Seed PPI interaction contrasts" in (destination / "README.md").read_text()
    release = json.loads((destination / "release.json").read_text())
    assert release["model"] == "ppi" and release["contrast_names"] == {"1": "ppi-win", "5": "phys"}
    changed = Path(plan["jobs"][0]["phys"])
    changed.write_text(changed.read_text() + "0\n")
    with pytest.raises(ValueError, match="Frozen input"):
        workflow.complete(plan["jobs"][0], plan, sha(private / "plan.json"))
