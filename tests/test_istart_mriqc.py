import importlib.util
import json

from harmonized.common import ROOT

spec = importlib.util.spec_from_file_location('istart_mriqc', ROOT / 'code/run_istart_mriqc.py')
mriqc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mriqc)


def test_mriqc_reuses_known_container_readonly_raw_data_and_private_task_work(tmp_path, monkeypatch):
    monkeypatch.setattr(mriqc, 'ROOT', tmp_path)
    unit = {'subject': 'sub-1002', 'task': 'doors', 'stem': 'sub-1002_task-doors_run-1'}
    argv = mriqc.command('/usr/bin/singularity', unit)
    assert argv[:3] == ['/usr/bin/singularity', 'run', '--cleanenv']
    assert '/ZPOOL/data/tools/mriqc-24.0.2.simg' in argv
    assert str(tmp_path / 'bids') + ':/data:ro' in argv
    assert argv[argv.index('--task-id') + 1] == 'doors'
    assert argv[argv.index('-m') + 1] == 'bold'
    assert argv[argv.index('--participant-label') + 1] == '1002'
    assert argv[argv.index('--nprocs') + 1] == '4'
    assert argv[argv.index('--omp-nthreads') + 1] == '2'
    assert '--no-sub' in argv and '--bids-database-dir' in argv
    assert (tmp_path / 'work/mriqc/sub-1002_task-doors_run-1').is_dir()


def test_iqm_requires_real_finite_fd_and_positive_tsnr(tmp_path, monkeypatch):
    monkeypatch.setattr(mriqc, 'ROOT', tmp_path)
    unit = {'subject': 'sub-1002', 'stem': 'sub-1002_task-doors_run-1'}
    path = tmp_path / 'derivatives/mriqc/sub-1002/func/sub-1002_task-doors_run-1_bold.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'fd_mean': .12, 'tsnr': 30, 'provenance': {'version': 'existing'}}))
    result = mriqc.iqm(unit)
    assert result['fd_mean'] == .12 and result['tsnr'] == 30 and len(result['sha256']) == 64
    for value in (float('nan'), -1):
        path.write_text(json.dumps({'fd_mean': value, 'tsnr': 30}))
        assert mriqc.iqm(unit) is None
