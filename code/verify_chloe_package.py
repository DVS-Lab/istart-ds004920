#!/usr/bin/env python3
"""Check a copied Chloe/Johanna package using only Python's standard library."""

import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import re


EXPECTED_RUNS = {'istart': 106, 'rf1': 735}
MODELS = [('activation', 'n/a')] + [
    ('ppi', seed) for seed in ('mPFC_left', 'mPFC_right', 'hippo_left', 'hippo_right', 'IPL', 'Fusiform')
]
CONTRASTS = ('win', 'loss', 'decision', 'winMinusLoss')
STATISTICS = ('cope', 'zstat')
EXPECTED_MAPS = {(model, seed, contrast, statistic) for model, seed in MODELS
                 for contrast in CONTRASTS for statistic in STATISTICS}
QC_FIELDS = ('mriqc_fd_mean', 'mriqc_tsnr', 'mriqc_version', 'mriqc_echo', 'mriqc_source_sha256')
STUDY_FILES = {'README', 'dataset_description.json', 'participants.tsv', 'maps.tsv',
               'run_quality.tsv', 'run_quality.json', 'mriqc_metrics.tsv', 'run_availability.tsv'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def index_table(path, key):
    records = {}
    with path.open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream, delimiter='\t'):
            identity = key(row)
            require(identity not in records, f'Duplicate entry in {path}: {identity}')
            records[identity] = row
    return records


def run_key(row):
    session = row['session']
    return (row['participant_id'], session.zfill(2) if session not in ('', 'n/a') else 'n/a',
            row['task'], int(row['run']))


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_study(root, count, expected_runs):
    name = root.name
    expected_maps = len(EXPECTED_MAPS) * expected_runs
    expected_counts = {'runs': expected_runs, 'maps': expected_maps, 'mriqc_available': expected_runs,
                       'cope_maps': expected_maps // 2, 'zstat_maps': expected_maps // 2}
    require(count == expected_counts, f'{name}: unexpected counts in READY.json')
    checksums = index_table(root / 'checksums.tsv', lambda row: row['file'])
    require(STUDY_FILES <= checksums.keys(), f'{name}: required documentation or tables are missing from the file list')
    actual = {path.relative_to(root).as_posix() for path in root.rglob('*')
              if path.is_file() and path != root / 'checksums.tsv'}
    missing = checksums.keys() - actual
    extra = actual - checksums.keys()
    require(not missing, f'{name}: missing file: {min(missing) if missing else ""}')
    require(not extra, f'{name}: unexpected file: {min(extra) if extra else ""}')

    quality = index_table(root / 'run_quality.tsv', run_key)
    mriqc = index_table(root / 'mriqc_metrics.tsv', run_key)
    require(len(quality) == expected_runs, f'{name}: incorrect number of run quality records')
    for key, row in quality.items():
        original = mriqc.get(key)
        label = f'{name} {key}'
        require(original is not None and row['mriqc_status'] == original['mriqc_status'] == 'available',
                f'{label}: missing or ambiguous MRIQC summary')
        require(all(row[field] == original[field] for field in QC_FIELDS),
                f'{label}: MRIQC summaries disagree between the two tables')
        fd, tsnr = float(row['mriqc_fd_mean']), float(row['mriqc_tsnr'])
        require(math.isfinite(fd) and fd >= 0 and math.isfinite(tsnr) and tsnr > 0,
                f'{label}: invalid MRIQC FD or tSNR')
        require(bool(row['mriqc_version']) and re.fullmatch(r'[0-9a-f]{64}', row['mriqc_source_sha256']),
                f'{label}: incomplete MRIQC provenance')

    maps = index_table(root / 'maps.tsv', lambda row: row['file'])
    require(len(maps) == expected_maps, f'{name}: incorrect number of maps')
    require({path for path in actual if path.endswith('_statmap.nii.gz')} == maps.keys(),
            f'{name}: statistical image files do not match maps.tsv')
    per_run = defaultdict(set)
    masks = defaultdict(set)
    for path, row in maps.items():
        require(all(row[field] == checksums[path][field] for field in ('sha256', 'bytes')),
                f'{name}: map and checksum tables disagree for {path}')
        key = run_key(row)
        unit = tuple(row[field] for field in ('model', 'seed', 'contrast', 'statistic'))
        require(unit not in per_run[key], f'{name} {key}: duplicate map type {unit}')
        per_run[key].add(unit)
        filename = Path(path)
        masks[key].add(filename.with_name(filename.name.split('_space-', 1)[0]
                       + '_space-MNI152NLin6Asym_desc-brain_mask.nii.gz').as_posix())
    require(per_run.keys() == quality.keys(), f'{name}: map and quality tables identify different runs')
    require(all(units == EXPECTED_MAPS for units in per_run.values()), f'{name}: a run is missing an expected map type')
    expected_masks = set().union(*masks.values())
    require(all(len(paths) == 1 for paths in masks.values()) and len(expected_masks) == expected_runs
            and {path for path in actual if path.endswith('_desc-brain_mask.nii.gz')} == expected_masks,
            f'{name}: expected one matching brain mask per exported run')

    for number, (relative, record) in enumerate(checksums.items(), 1):
        path = root / relative
        require(not path.is_symlink(), f'{name}: expected a file, found a link: {relative}')
        require(path.stat().st_size == int(record['bytes']), f'{name}: file size mismatch: {relative}')
        require(sha256(path) == record['sha256'], f'{name}: checksum mismatch: {relative}')
        if number % 5000 == 0:
            print(f'{name}: checked {number:,} of {len(checksums):,} files...', flush=True)
    print(f'{name}: {expected_maps:,} maps, {expected_runs:,} masks, '
          f'MRIQC summaries for {expected_runs:,}/{expected_runs:,} runs; all file checksums match.', flush=True)


def verify_package(package):
    for name in ('README.txt', 'VERIFY_PACKAGE.py'):
        require((package / name).is_file(), f'Missing {name} at the top of the package')
    ready = json.loads((package / 'READY.json').read_text(encoding='utf-8'))
    require(ready.get('export_profile') == 'cope-zstat-fourcontrasts-v2'
            and ready.get('statistics') == list(STATISTICS), 'READY.json does not describe the final COPE + Z export')
    require(ready.get('mriqc_complete_for_all_exported_runs') is True,
            'READY.json does not report complete MRIQC coverage')
    for name, expected_runs in EXPECTED_RUNS.items():
        verify_study(package / name, ready['counts'][name], expected_runs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path, nargs='?', default=Path(__file__).resolve().parent,
                        help='folder to check (default: the folder containing this script)')
    args = parser.parse_args()
    try:
        verify_package(args.package)
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
        parser.exit(1, f'FAIL: {error}\n')
    print('PASS: the package matches the verified Linux2 export.')


if __name__ == '__main__':
    main()
