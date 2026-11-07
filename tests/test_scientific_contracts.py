import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pandas as pd
import pytest
from scipy.spatial.transform import Rotation

from harmonized.common import BASE, CONTRASTS, ROOT, grid, image_info, sha, stamp, verify
from harmonized.inputs import base_confounds, events
from harmonized.motion import fd, from_fsl
from harmonized.workflow import fsf_values, validate_design


def confound_file(path, n=80):
    columns = ["non_steady_state_outlier00", "cosine01", *BASE[::-1], "cosine00", "rot_x_derivative1", "tedana_ica00"]
    frame = pd.DataFrame(np.random.default_rng(21).normal(size=(n, len(columns))), columns=columns)
    frame.loc[0, "framewise_displacement"] = np.nan
    frame.to_csv(path, sep="\t", index=False, na_rep="n/a")
    return frame


def test_base_model_order_and_missing_values(tmp_path):
    path = tmp_path / "confounds.tsv"
    original = confound_file(path)
    x, cols = base_confounds(path, 80)
    assert cols == BASE + ["cosine01", "cosine00", "non_steady_state_outlier00"]
    assert x[0, 12] == 0
    assert np.allclose(x[:, 0], original["a_comp_cor_00"])
    assert "rot_x_derivative1" not in cols and "tedana_ica00" not in cols


@pytest.mark.parametrize("case", ["missing", "malformed", "infinity", "rows", "duplicate"])
def test_invalid_confounds_fail(tmp_path, case):
    p = tmp_path / "c.tsv"
    frame = confound_file(p)
    if case == "missing":
        frame.drop(columns="rot_x").to_csv(p, sep="\t", index=False)
    elif case == "malformed":
        p.write_text(p.read_text().replace("n/a", "bad_number"))
    elif case == "infinity":
        frame.loc[1, "rot_x"] = np.inf
        frame.to_csv(p, sep="\t", index=False)
    elif case == "duplicate":
        p.write_text(p.read_text().replace("tedana_ica00", "rot_x"))
    with pytest.raises(ValueError):
        base_confounds(p, 81 if case == "rows" else 80)


def test_events_preserve_duration_amplitude_and_misses(tmp_path):
    p = tmp_path / "events.tsv"
    p.write_text("onset\tduration\ttrial_type\n0\t3.01\tdecision\n4\t1\twin\n10\t1\tloss\n15\t3\tdecision-missed\n")
    ev, ignored = events(p, 40)
    assert ev["decision"] == [[0, 3.01, 1]]
    assert ev["decision-missed"] == [[15, 3, 1]]
    assert ignored == []
    p.write_text(p.read_text().replace("decision\n", "decision-missed\n"))
    with pytest.raises(ValueError, match="Missing required"):
        events(p, 40)


def test_grid_rejects_affine_shift_even_same_dimensions(tmp_path):
    a, b = tmp_path / "a.nii.gz", tmp_path / "b.nii.gz"
    affine = np.diag([-2., 2, 2, 1])
    nib.save(nib.Nifti1Image(np.zeros((4, 5, 6)), affine), a)
    changed = affine.copy()
    changed[0, 3] += .01
    nib.save(nib.Nifti1Image(np.zeros((4, 5, 6)), changed), b)
    with pytest.raises(ValueError, match="Grid mismatch"):
        grid(a, b)


def test_tr_is_not_a_hardcoded_volume_count(tmp_path):
    p = tmp_path / "bold.nii.gz"
    img = nib.Nifti1Image(np.ones((3, 3, 3, 12)), np.eye(4))
    img.header.set_zooms((1, 1, 1, 1.75))
    img.header.set_xyzt_units("mm", "sec")
    nib.save(img, p)
    assert image_info(p, tr=1.75).shape[3] == 12
    with pytest.raises(ValueError, match="TR"):
        image_info(p, tr=1.615)


def write_matrix(path, x):
    path.write_text("/Matrix\n" + "\n".join(" ".join(map(str, r)) for r in x) + "\n")


def test_estimable_contrasts_allow_zero_miss_ev(tmp_path):
    rng = np.random.default_rng(12)
    x = np.column_stack((rng.normal(size=(80, 3)), np.zeros(80), rng.normal(size=(80, 13))))
    c = np.column_stack((CONTRASTS, np.zeros((4, 13))))
    m, con = tmp_path / "design.mat", tmp_path / "design.con"
    write_matrix(m, x)
    write_matrix(con, c)
    assert validate_design(m, con, {"nvol": 80})["residual_dof_unwhitened"] > 0
    c[[0, 1]] = c[[1, 0]]
    write_matrix(con, c)
    with pytest.raises(ValueError, match="Contrasts"):
        validate_design(m, con, {"nvol": 80})


def test_unestimable_scientific_contrast_fails(tmp_path):
    x = np.random.default_rng(8).normal(size=(80, 4))
    x[:, 1] = x[:, 0]
    m, c = tmp_path / "m", tmp_path / "c"
    write_matrix(m, x)
    write_matrix(c, CONTRASTS)
    with pytest.raises(ValueError, match="Unestimable"):
        validate_design(m, c, {"nvol": 80})


def test_source_template_science_is_preserved():
    for task in ("doors", "socialdoors"):
        f = fsf_values((ROOT / f"templates/L1_task-{task}_model-1_type-act.fsf").read_text())
        assert f["fmri(smooth)"] == "0"
        assert f["fmri(tr)"] == "TR_SECONDS"
        assert f["fmri(temphp_yn)"] == "0" and f["fmri(prewhiten_yn)"] == "1"
        assert all(f[f"fmri(convolve{i})"] == "3" for i in range(1, 5))
        for i in range(4):
            for j in range(4):
                assert float(f[f"fmri(con_real{i+1}.{j+1})"]) == CONTRASTS[i, j]


@pytest.mark.parametrize("orientation", ["LAS", "RAS", "AIL"])
@pytest.mark.parametrize("offset", [0, 200])
def test_corrected_motion_recovers_large_noncommuting_rotations(orientation, offset):
    # Independent FSL composition: M = Rx(-x) Ry(-y) Rz(-z), with CoG displacement t.
    shape = (9, 7, 8)
    data = np.arange(np.prod(shape), dtype=float).reshape(shape) + offset
    img = nib.Nifti1Image(data, np.diag([-2., 2, 2, 1]))
    ornt = nib.orientations.ornt_transform(nib.orientations.io_orientation(img.affine), nib.orientations.axcodes2ornt(orientation))
    img = img.as_reoriented(ornt)
    d = img.get_fdata()
    if np.linalg.det(img.affine) > 0:
        d = d[::-1]
    weights = d - d.min()
    coordinates = np.indices(d.shape)
    center = np.array([(coordinates[i] * weights).sum() / weights.sum() for i in range(3)]) * img.header.get_zooms()[:3]
    angles = np.array([[0, 0, 0], [.12, -.09, .11], [-.1, .08, -.13]])
    translations = np.array([[0, 0, 0], [5, -4, 3], [-3, 2, -4]])
    mats = np.tile(np.eye(4), (3, 1, 1))
    for i, (x, y, z) in enumerate(angles):
        rx = np.array([[1, 0, 0], [0, np.cos(x), np.sin(x)], [0, -np.sin(x), np.cos(x)]])
        ry = np.array([[np.cos(y), 0, -np.sin(y)], [0, 1, 0], [np.sin(y), 0, np.cos(y)]])
        rz = np.array([[np.cos(z), np.sin(z), 0], [-np.sin(z), np.cos(z), 0], [0, 0, 1]])
        mats[i, :3, :3] = rx @ ry @ rz
        mats[i, :3, 3] = translations[i] + center - mats[i, :3, :3] @ center
    recovered = from_fsl(mats, img)
    assert np.allclose(recovered[:, :3], translations, atol=1e-10)
    assert np.allclose(recovered[:, 3:], angles, atol=1e-10)
    assert np.isnan(fd(recovered)[0])
    assert np.isclose(fd(recovered)[1], 12 + 50 * (.12 + .09 + .11))


def test_frozen_input_mutation_is_detected(tmp_path):
    p = tmp_path / "ev.txt"
    p.write_text("1 1 1\n")
    s = stamp(p)
    p.write_text("2 1 1\n")
    with pytest.raises(ValueError, match="Frozen input"):
        verify([s])
