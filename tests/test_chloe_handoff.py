import importlib.util
from pathlib import Path

import numpy as np
import pytest

from harmonized.common import ROOT, sha

spec = importlib.util.spec_from_file_location('handoff', ROOT / 'code/prepare_chloe_handoff.py')
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


def test_fd_nan_omission_and_tsnr_nonsteady_constant_voxels(tmp_path):
    path = tmp_path / 'confounds.tsv'
    path.write_text('framewise_displacement\tnon_steady_state_outlier00\n'
                    'n/a\t1\n0.1\t0\n0.3\t0\n0.8\t0\n')
    fd, keep = handoff.numeric_fd_and_nss(path, 4)
    assert fd['fd_mean'] == pytest.approx(.4)
    assert fd['fd_valid_volumes'] == 3 and fd['fd_missing_volumes'] == 1
    assert fd['nonsteady_volumes'] == 1 and keep.tolist() == [False, True, True, True]
    data = np.array([1000, 2, 4, 6, 0, 9, 9, 9, 0, 4, 8, 12], dtype=np.float32).reshape(3, 1, 1, 4)
    result = handoff.temporal_snr(data, np.ones((3, 1, 1)), keep)
    assert result['tsnr'] == pytest.approx(4 / np.std([2, 4, 6]))
    assert result['tsnr_voxels'] == 2 and result['tsnr_undefined_voxels'] == 1
    assert result['tsnr_volumes'] == 3
    data[0, 0, 0, 2] = np.nan
    with pytest.raises(ValueError, match='Nonfinite'):
        handoff.temporal_snr(data, np.ones((3, 1, 1)), keep)


def test_negative_or_infinite_fd_is_not_silently_accepted(tmp_path):
    path = tmp_path / 'confounds.tsv'
    for value in ('-0.1', 'inf'):
        path.write_text('framewise_displacement\n0\n' + value + '\n')
        with pytest.raises(ValueError, match='Invalid framewise'):
            handoff.numeric_fd_and_nss(path, 2)


def test_study_subject_session_and_seed_names_remain_unambiguous():
    base = {'subject': '1002', 'session': '', 'stem': 'sub-1002_task-doors_run-1'}
    act = handoff.relative(base, 4)
    ppi = handoff.relative(dict(base, model='ppi', seed='mPFC_left'), 4)
    assert act.parts[:2] == ('sub-1002', 'func')
    assert '_model-activation_contrast-winMinusLoss_stat-effect_statmap.nii.gz' in str(act)
    assert '_model-ppiMPFCLeft_' in str(ppi) and act != ppi
    rf1 = handoff.relative(dict(base, session='01', stem='sub-1002_ses-01_task-doors_run-1'), 1)
    assert rf1.parts[:3] == ('sub-1002', 'ses-01', 'func')


def test_export_copy_is_byte_exact_resumable_and_preserves_mismatch(tmp_path):
    source, target = tmp_path / 'source.nii.gz', tmp_path / 'out/map.nii.gz'
    source.write_bytes(b'original compressed NIfTI bytes')
    expected = sha(source)
    handoff.copy_checked(source, target, expected)
    assert target.read_bytes() == source.read_bytes()
    handoff.copy_checked(source, target, expected)
    target.write_bytes(b'unexpected existing content')
    with pytest.raises(ValueError, match='preserved'):
        handoff.copy_checked(source, target, expected)
    assert target.read_bytes() == b'unexpected existing content'
    other = tmp_path / 'other.nii.gz'
    with pytest.raises(ValueError, match='Source checksum'):
        handoff.copy_checked(source, other, '0' * 64)
    assert not other.exists()


def test_rf1_mriqc_uses_existing_echo_selection_instead_of_averaging(tmp_path):
    import json
    paths = []
    for echo, tsnr in ((1, 10), (2, 20)):
        p = tmp_path / f'sub-001_ses-01_task-doors_run-1_echo-{echo}_bold.json'
        p.write_text(json.dumps({'fd_mean': .1, 'tsnr': tsnr, 'provenance': {'version': 'existing'}}))
        paths.append(p)
    qc = tmp_path / 'run_qc.tsv'
    qc.write_text('subject\tsession\ttask\trun\tmriqc_json\n001\t01\tdoors\t1\t' + str(paths[1]) + '\n')
    environment = {'mriqc': {'rf1': {'files': [{'file': str(p), 'version': 'existing'} for p in paths]}},
                   'source_qc_tables': [{'dataset': 'rf1', 'path': str(qc)}]}
    row = handoff.mriqc_index(environment)[('rf1', '001', '01', 'doors', '1')]
    assert len(row) == 1 and row[0]['mriqc_tsnr'] == 20
    assert row[0]['mriqc_echo'] == '2'
