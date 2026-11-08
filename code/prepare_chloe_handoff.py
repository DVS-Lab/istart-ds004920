#!/usr/bin/env python3
"""Stage a minimal, checksummed, participant-organized COPE handoff on Linux2."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import nibabel as nib
import numpy as np
import pandas as pd

ROOT = Path('/ZPOOL/data/projects/istart-ds004920')
sys.path.insert(0, str(ROOT / 'code'))
from harmonized.common import CONTRASTS as ACT_WEIGHTS, PPI_CONTRASTS, grid, lock, now, read_table, sha, stamp, table, verify, write_json
from harmonized.environment import configure
from harmonized.inputs import load_plan

CONTRASTS = {1: 'win', 2: 'loss', 3: 'decision', 4: 'winMinusLoss'}
SEEDS = {'hippo_left': 'HippoLeft', 'hippo_right': 'HippoRight', 'IPL': 'IPL',
         'Fusiform': 'Fusiform', 'mPFC_left': 'MPFCLeft', 'mPFC_right': 'MPFCRight'}
RECIPE = 'common-grid-unsmoothed-mask-median-tsnr-ddof0-drop-nss-v1'


def alive(state):
    pid = state.get('pid')
    path = Path(f'/proc/{pid}/cmdline')
    if not pid or not path.exists():
        return False
    args = path.read_bytes().decode(errors='replace').split('\0')
    return any(Path(arg).name == state.get('worker_script', 'run_harmonized_l1.py') for arg in args)


def work():
    return ROOT / 'work/delivery-runs/chloe'


def destination():
    return ROOT / 'derivatives/release-candidates/chloe-harmonized-l1-v1'


def relative(job, contrast):
    model = 'ppi' + SEEDS[job['seed']] if job.get('model') == 'ppi' else 'activation'
    name = f"{job['stem']}_space-MNI152NLin6Asym_model-{model}_contrast-{CONTRASTS[contrast]}_stat-effect_statmap.nii.gz"
    folder = Path('sub-' + job['subject'])
    if job['session']:
        folder /= 'ses-' + job['session']
    return folder / 'func' / name


def copy_checked(source, target, expected):
    """Copy unchanged bytes with SHA256; interrupted copies are resumable."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or target.is_symlink():
        raise ValueError('Export does not follow image symlinks')
    if target.exists():
        if sha(target) != expected:
            raise ValueError(f'Existing staged file differs; preserved: {target}')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.part')
    h = hashlib.sha256()
    with source.open('rb') as src, temp.open('wb') as out:
        for block in iter(lambda: src.read(8 * 1024 * 1024), b''):
            h.update(block)
            out.write(block)
    if h.hexdigest() != expected:
        raise ValueError(f'Source checksum differs from its verified receipt: {source}')
    temp.replace(target)


def numeric_fd_and_nss(confounds, nvol):
    frame = pd.read_csv(confounds, sep='\t')
    if len(frame) != nvol or 'framewise_displacement' not in frame:
        raise ValueError('QC confounds rows/FD do not match the BOLD series')
    fd = pd.to_numeric(frame['framewise_displacement'], errors='raise').to_numpy(dtype=float)
    if np.isinf(fd).any() or (fd[np.isfinite(fd)] < 0).any():
        raise ValueError('Invalid framewise displacement')
    valid = fd[np.isfinite(fd)]
    if not len(valid):
        raise ValueError('No finite FD values')
    columns = [c for c in frame if c.startswith('non_steady_state_outlier')]
    nss = np.zeros(nvol, dtype=bool)
    if columns:
        values = frame[columns].apply(pd.to_numeric, errors='raise').to_numpy(dtype=float)
        if not np.isfinite(values).all() or not np.isin(values, [0, 1]).all():
            raise ValueError('Invalid non-steady-state indicators')
        nss = np.any(values != 0, axis=1)
    return {'fd_mean': float(valid.mean()), 'fd_max': float(valid.max()), 'fd_valid_volumes': int(len(valid)),
            'fd_missing_volumes': int(nvol - len(valid)), 'fd_gt_0p2_fraction': float(np.mean(valid > .2)),
            'fd_gt_0p5_fraction': float(np.mean(valid > .5)), 'nonsteady_volumes': int(nss.sum())}, ~nss


def temporal_snr(data, mask, keep):
    if data.ndim != 4 or mask.shape != data.shape[:3] or len(keep) != data.shape[3] or np.sum(keep) < 2:
        raise ValueError('Invalid tSNR dimensions or fewer than two steady-state volumes')
    if not np.isfinite(mask).all() or not np.isin(mask, [0, 1]).all() or not np.any(mask):
        raise ValueError('Invalid brain mask for tSNR')
    values = data[mask.astype(bool)][:, keep]
    if not np.isfinite(values).all():
        raise ValueError('Nonfinite BOLD values inside the tSNR brain mask')
    mean = np.mean(values, axis=1, dtype=np.float64)
    sd = np.std(values, axis=1, ddof=0, dtype=np.float64)
    good = (sd > 0) & (mean > 0)
    if not np.any(good):
        raise ValueError('No positive-mean, nonconstant voxels for tSNR')
    ratios = mean[good] / sd[good]
    return {'tsnr': float(np.median(ratios)), 'tsnr_mean': float(np.mean(ratios)),
            'tsnr_voxels': int(good.sum()), 'tsnr_mask_voxels': int(mask.sum()),
            'tsnr_undefined_voxels': int((~good).sum()), 'tsnr_volumes': int(np.sum(keep))}


def mriqc_index(environment):
    records = defaultdict(list)
    for dataset, record in environment.get('mriqc', {}).items():
        for source in record.get('files', []):
            path = Path(source['file'])
            if not path.is_file():
                continue
            from harmonized.common import entities
            e = entities(path)
            key = (dataset, e.get('sub'), e.get('ses', '').zfill(2) if e.get('ses') else '',
                   e.get('task'), e.get('run', '1').lstrip('0') or '0')
            data = json.loads(path.read_text())
            try:
                fd, tsnr = float(data['fd_mean']), float(data['tsnr'])
                if not np.isfinite([fd, tsnr]).all() or fd < 0 or tsnr <= 0:
                    raise ValueError('Invalid MRIQC values')
                records[key].append({'mriqc_fd_mean': fd, 'mriqc_tsnr': tsnr, 'mriqc_version': source['version'],
                                     'mriqc_source_sha256': sha(path), 'mriqc_echo': e.get('echo', 'n/a')})
            except (KeyError, TypeError, ValueError):
                pass
    # RF1 has multiple acquired echoes. Reuse the original QC table's exact
    # mriqc_json selection rather than choosing or averaging echoes here.
    for source_table in environment.get('source_qc_tables', []):
        if source_table['dataset'] != 'rf1':
            continue
        _, rows = read_table(Path(source_table['path']))
        upstream = Path(source_table['path']).parents[1]
        for row in rows:
            if row.get('task') not in ('doors', 'socialdoors') or not row.get('mriqc_json'):
                continue
            path = Path(row['mriqc_json'])
            if not path.is_absolute():
                path = upstream / path
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
            try:
                fd, tsnr = float(data['fd_mean']), float(data['tsnr'])
                if not np.isfinite([fd, tsnr]).all() or fd < 0 or tsnr <= 0:
                    continue
                from harmonized.common import entities
                e = entities(path)
                session = row.get('session', '').removeprefix('ses-')
                key = ('rf1', row['subject'].removeprefix('sub-'), session.zfill(2) if session else '',
                       row['task'], str(int((row.get('run') or '1').removeprefix('run-'))))
                records[key] = [{'mriqc_fd_mean': fd, 'mriqc_tsnr': tsnr,
                                 'mriqc_version': str(data.get('provenance', {}).get('version', 'unknown')),
                                 'mriqc_source_sha256': sha(path), 'mriqc_echo': e.get('echo', 'n/a')}]
            except (KeyError, TypeError, ValueError):
                pass
    return records


def qc_run(job, candidates):
    spatial = Path(job['spatial'])
    receipt = json.loads((spatial / 'receipt.json').read_text())
    source = Path(receipt['seed_input']['path'])
    mask = spatial / 'mask.nii.gz'
    signature = {'recipe': RECIPE, 'bold': receipt['seed_input'], 'mask': stamp(mask, full=True),
                 'confounds': stamp(job['source_confounds'], full=True)}
    cache = work() / 'qc-cache' / (job['unit'] + '.json')
    verify([receipt['seed_input']])
    if cache.exists() and json.loads(cache.read_text()).get('signature') == signature:
        row = json.loads(cache.read_text())['row']
    else:
        grid(source, mask)
        data = nib.load(source).get_fdata(dtype=np.float32, caching='unchanged')
        fd, keep = numeric_fd_and_nss(job['source_confounds'], data.shape[3])
        metrics = temporal_snr(data, np.asanyarray(nib.load(mask).dataobj), keep)
        verify([receipt['seed_input'], signature['confounds'], signature['mask']])
        row = {'participant_id': 'sub-' + job['subject'], 'session': job['session'] or 'n/a',
               'task': job['task'], 'run': job['run'], 'unit': job['unit'],
               'nvol': job['nvol'], 'tr_seconds': job['tr'], **fd, **metrics,
               'fd_source': 'fMRIPrep', 'tsnr_source': 'fMRIPrep_unsmoothed_common_grid',
               'classic_fwhm_mm': receipt['after']['classic_fwhm_combined'],
               'smoothness_reviewed_exception': receipt['status'] == 'outside_tolerance',
               'exclusion_decision': 'not_assigned'}
        write_json(cache, {'signature': signature, 'row': row})
    key = (job['dataset'], job['subject'], job['session'], job['task'], str(int(job['run'])))
    matches = candidates.get(key, [])
    row = dict(row, mriqc_status='available' if len(matches) == 1 else 'ambiguous_multiple_outputs' if matches else 'not_found',
               mriqc_fd_mean='n/a', mriqc_tsnr='n/a', mriqc_version='n/a', mriqc_source_sha256='n/a', mriqc_echo='n/a')
    if len(matches) == 1:
        row.update(matches[0])
    return row


def copy_fit(job, plan_hash):
    receipt = json.loads((Path(job['output']) / 'harmonized_success.json').read_text())
    if receipt['unit'] != job['unit'] or receipt['plan_sha256'] != plan_hash:
        raise ValueError('Fit receipt belongs to another frozen plan')
    verify(receipt['provenance'])
    products = {r['path']: r for r in receipt['products']}
    rows = []
    for contrast in CONTRASTS:
        src = Path(job['output']) / 'stats' / f'cope{contrast}.nii.gz'
        frozen = products[str(src)]
        if any(stamp(src, full=False)[k] != frozen[k] for k in ('path', 'size', 'mtime_ns')):
            raise ValueError(f'COPE metadata changed: {src}')
        rel = relative(job, contrast)
        copy_checked(src, destination() / job['dataset'] / rel, frozen['sha256'])
        rows.append({'participant_id': 'sub-' + job['subject'], 'session': job['session'] or 'n/a',
                     'task': job['task'], 'run': job['run'], 'model': job.get('model', 'activation'),
                     'seed': job.get('seed', 'n/a'), 'contrast': CONTRASTS[contrast], 'file': str(rel),
                     'sha256': frozen['sha256'], 'bytes': frozen['size'], 'plan_sha256': receipt['plan_sha256']})
    return job['dataset'], rows


def publish():
    return subprocess.run([sys.executable, str(Path(__file__).with_name('publish_processing_status.py')),
                           'publish', '--root', str(ROOT)]).returncode


def assemble(state, save):
    config = json.loads((ROOT / 'config/linux2.json').read_text())
    configure(config)
    paths = [ROOT / 'work/harmonized-l1-v1', ROOT / 'work/harmonized-ppi-v1']
    plans = [load_plan(path) for path in paths]
    for path, plan in zip(paths, plans):
        validation = json.loads((path / 'check_cohort_validation.json').read_text())
        if not validation['all_selected_verified'] or validation['selected'] != len(plan['jobs']) or validation['plan_sha256'] != sha(path / 'plan.json'):
            raise ValueError('Final full-cohort verification has not passed')
        if validation.get('smoothness_qc_review'):
            verify([validation['smoothness_qc_review']])
    dest = destination()
    dest.mkdir(parents=True, exist_ok=True)
    environment_file = ROOT / 'work/delivery-environment.json'
    environment_sha = sha(environment_file) if environment_file.exists() else None
    if (dest / 'READY.json').exists() and json.loads((dest / 'READY.json').read_text()).get('mriqc_inventory_sha256') == environment_sha:
        state.update(status='complete', operation='complete', destination=str(dest))
        return
    if (dest / 'READY.json').exists():
        old = dest / 'READY.json'
        archived = work() / ('previous-ready-' + sha(old) + '.json')
        if not archived.exists():
            old.replace(archived)
        else:
            old.unlink()
    environment = json.loads(environment_file.read_text()) if environment_file.exists() else {}
    candidates = mriqc_index(environment)
    state.update(operation='qc', qc_total=len(plans[0]['jobs']), qc_completed=0)
    save()
    qc = defaultdict(list)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(qc_run, j, candidates): j for j in plans[0]['jobs']}
        for future in as_completed(futures):
            job = futures[future]
            qc[job['dataset']].append(future.result())
            state['qc_completed'] += 1
            if state['qc_completed'] % 10 == 0:
                save()
    state.update(operation='copying_cope_maps', maps_total=sum(len(p['jobs']) * 4 for p in plans), maps_completed=0)
    save()
    estimated = sum(Path(j['output']).joinpath('stats', f'cope{i}.nii.gz').stat().st_size for p in plans for j in p['jobs'] for i in CONTRASTS)
    if shutil.disk_usage(dest).free < estimated * 1.1:
        raise ValueError('Insufficient free space for minimal COPE handoff')
    manifests = defaultdict(list)
    for path, plan in zip(paths, plans):
        plan_hash = sha(path / 'plan.json')
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(copy_fit, job, plan_hash) for job in plan['jobs']]
            for future in as_completed(futures):
                dataset, records = future.result()
                manifests[dataset].extend(records)
                state['maps_completed'] += len(records)
                if state['maps_completed'] % 100 == 0:
                    save()
    state.update(operation='metadata')
    save()
    _, availability = read_table(paths[0] / 'run_review.tsv')
    for dataset in ('istart', 'rf1'):
        study = dest / dataset
        rows = sorted(qc[dataset], key=lambda r: r['unit'])
        table(study / 'run_quality.tsv', rows)
        mriqc_rows = []
        for key, matches in sorted(candidates.items()):
            if key[0] == dataset:
                mriqc_rows.append({'participant_id': 'sub-' + key[1], 'session': key[2] or 'n/a', 'task': key[3], 'run': key[4],
                                   'mriqc_status': 'available' if len(matches) == 1 else 'ambiguous_multiple_outputs',
                                   **(matches[0] if len(matches) == 1 else {})})
        table(study / 'mriqc_metrics.tsv', mriqc_rows, fields=['participant_id', 'session', 'task', 'run', 'mriqc_status',
              'mriqc_fd_mean', 'mriqc_tsnr', 'mriqc_version', 'mriqc_echo', 'mriqc_source_sha256'])
        table(study / 'participants.tsv', [{'participant_id': p} for p in sorted({r['participant_id'] for r in rows})])
        table(study / 'maps.tsv', sorted(manifests[dataset], key=lambda r: r['file']))
        table(study / 'run_availability.tsv', [{k: r.get(k, '') for k in ('subject', 'session', 'task', 'run', 'status', 'reason')}
                                              for r in availability if r['dataset'] == dataset])
        write_json(study / 'dataset_description.json', {'Name': f'{dataset} harmonized Doors and Social Doors statistical derivatives',
                   'BIDSVersion': '1.11.2', 'DatasetType': 'derivative',
                   'GeneratedBy': [{'Name': 'DVS-Lab harmonized L1', 'Version': 'fwhm6-baseonly-v1',
                                    'CodeURL': 'https://github.com/DVS-Lab/istart-ds004920'}]})
        for job in [j for j in plans[0]['jobs'] if j['dataset'] == dataset]:
            rel = relative(job, 1)
            mask_name = job['stem'] + '_space-MNI152NLin6Asym_desc-brain_mask.nii.gz'
            src = Path(job['spatial']) / 'mask.nii.gz'
            rec = json.loads((Path(job['spatial']) / 'receipt.json').read_text())
            frozen = next(r for r in rec['products'] if r['path'] == str(src))
            copy_checked(src, study / rel.parent / mask_name, frozen['sha256'])
        for model in ['activation'] + ['ppi' + v for v in SEEDS.values()]:
            for i, contrast in CONTRASTS.items():
                meta = {'Description': f'Unthresholded FSL COPE for {contrast}' + (' PPI interaction' if model != 'activation' else ' activation'), 'StatisticType': 'effect',
                        'Model': model, 'ContrastName': contrast, 'FSLContrastNumber': i,
                        'ContrastWeights': (PPI_CONTRASTS[i-1] if model != 'activation' else ACT_WEIGHTS[i-1]).tolist(),
                        'SpatialReference': 'MNI152NLin6Asym', 'VoxelSize': [2.7, 2.7, 2.97],
                        'TargetClassicFWHM': 6, 'MeasuredSmoothnessTable': 'run_quality.tsv',
                        'Confounds': 'a_comp_cor_00..05, 6 rigid-body motion parameters, FD, available cosines and non-steady-state columns; no TEDANA'}
                if model != 'activation':
                    seed = next(k for k, v in SEEDS.items() if model == 'ppi' + v)
                    meta.update(Seed=seed, SeedPriority='primary' if seed.startswith('mPFC') else 'exploratory')
                    seed_manifest = json.loads((ROOT / 'masks/seed_manifest.json').read_text())
                    meta['SeedMaskSHA256'] = next(r['sha256'] for r in seed_manifest['seeds'] if r['seed'] == seed)
                write_json(study / f'model-{model}_contrast-{contrast}_stat-effect_statmap.json', meta)
        write_json(study / 'run_quality.json', {'fd_mean': {'Description': 'Mean finite fMRIPrep framewise_displacement; missing values omitted, not zero-filled', 'Units': 'mm'},
                   'tsnr': {'Description': 'Median of temporal mean / population temporal SD in the run brain mask on unsmoothed common-grid fMRIPrep data; non-steady-state volumes omitted; no detrending or nuisance regression', 'Units': 'arbitrary'},
                   'mriqc_fd_mean': {'Description': 'Existing MRIQC fd_mean, kept separate from fMRIPrep FD', 'Units': 'mm'},
                   'mriqc_tsnr': {'Description': 'Existing MRIQC tsnr, not substituted by preprocessed tSNR'},
                   'MissingValues': 'n/a', 'ExclusionPolicy': 'No new exclusions assigned by this package'})
        (study / 'README').write_text('Minimal harmonized Doors/Social Doors handoff\n\n'
            'Participant/session/func hierarchy follows BIDS Derivatives. Statistical map entities use the statistical-model extension convention (BEP041); that extension remains in development.\n'
            'Only unthresholded COPE effect images for Win, Loss, Decision and Win-minus-Loss are included, for activation and all six seed-PPI models. The physiological main-effect contrast and other statistic types are retained on Linux2.\n'
            'mPFC Left/Right are primary; other seeds exploratory. These are run-level maps; RF1 sessions remain separate. There is no session averaging, group model or new scientific exclusion.\n'
            'run_quality.tsv distinguishes fMRIPrep-derived FD/tSNR from available original MRIQC metrics. mriqc_metrics.tsv also covers discovered task MRIQC runs without exported maps. RF1 uses the original run_qc.tsv echo selection. See run_quality.json for recipes. run_availability.tsv records missing/held inputs.\n'
            'Sub-10657 session 01 measured classic FWHM is 5.3761 mm (Doors) and 5.3654 mm (Social Doors); these two reviewed exceptions were accepted without changing the images.\n'
            'Dataset separation and participant IDs are preserved. All supplied maps are on the same MNI152NLin6Asym reference grid (57 x 70 x 54; 2.7 x 2.7 x 2.97 mm).\n')
        checksums = [{'file': str(p.relative_to(study)), 'sha256': sha(p), 'bytes': p.stat().st_size}
                     for p in sorted(study.rglob('*')) if p.is_file() and p.name != 'checksums.tsv']
        table(study / 'checksums.tsv', checksums)
    counts = {dataset: {'maps': len(manifests[dataset]), 'runs': len(qc[dataset]),
                       'mriqc_available': sum(r['mriqc_status'] == 'available' for r in qc[dataset])} for dataset in ('istart', 'rf1')}
    write_json(dest / 'READY.json', {'created': now(), 'counts': counts, 'cope_only': True,
               'plan_sha256': [sha(p / 'plan.json') for p in paths], 'uploaded': False,
               'mriqc_inventory_sha256': environment_sha,
               'mriqc_complete_for_all_exported_runs': all(v['runs'] == v['mriqc_available'] for v in counts.values())})
    state.update(status='complete', operation='complete', destination=str(dest), counts=counts, maps_completed=state['maps_total'])


def worker():
    statefile = work() / 'state.json'
    with (work() / 'launch.lock').open('a') as barrier:
        import fcntl
        fcntl.flock(barrier, fcntl.LOCK_EX)
    state = json.loads(statefile.read_text())
    save = lambda: write_json(statefile, state)
    stop = threading.Event()
    def monitor():
        while not stop.is_set():
            publish()
            stop.wait(300)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        while any(alive(json.loads(p.read_text())) for p in (ROOT / 'work/l1-runs').glob('*/state.json')):
            time.sleep(10)
        with lock(ROOT / 'work/l1-runs/schedule.lock'), lock(work() / 'worker.lock'):
            if any(alive(json.loads(p.read_text())) for p in (ROOT / 'work/l1-runs').glob('*/state.json')):
                raise ValueError('Another L1 process started; resume packaging after it finishes')
            state.update(status='started', operation='loading_verified_plans')
            save()
            assemble(state, save)
        state['finished'] = now()
    except Exception as exc:
        state.update(status='failed', error=str(exc), finished=now())
        print(f'{now()} ERROR: {exc}', flush=True)
    finally:
        save()
        stop.set()
        thread.join(timeout=60)
        for attempt in range(3):
            if publish() == 0:
                break
            time.sleep(5)
    return 0 if state['status'] == 'complete' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('launch', 'worker', 'status'))
    args = parser.parse_args()
    work().mkdir(parents=True, exist_ok=True)
    statefile = work() / 'state.json'
    if args.action == 'worker':
        return worker()
    if args.action == 'status':
        print(statefile.read_text() if statefile.exists() else 'Not launched')
        return 0
    with lock(work() / 'launch.lock'):
        old = json.loads(statefile.read_text()) if statefile.exists() else {}
        environment_file = ROOT / 'work/delivery-environment.json'
        readyfile = destination() / 'READY.json'
        already_current = readyfile.exists() and json.loads(readyfile.read_text()).get('mriqc_inventory_sha256') == (sha(environment_file) if environment_file.exists() else None)
        if alive(old) or (old.get('status') == 'complete' and already_current):
            print(json.dumps(old, indent=2))
            return 0
        log = ROOT / 'logs/chloe-handoff.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(Path(__file__).resolve()), 'worker']
        with log.open('a') as output, open(os.devnull) as null:
            child = subprocess.Popen(argv, stdin=null, stdout=output, stderr=subprocess.STDOUT,
                                     start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED='1'))
        state = {'pid': child.pid, 'worker_script': Path(__file__).name, 'status': 'queued',
                 'operation': 'waiting_for_final_L1_checks', 'launched': now(), 'log': str(log),
                 'launch_command': argv, 'destination': str(destination())}
        write_json(statefile, state)
        print(json.dumps(state, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
