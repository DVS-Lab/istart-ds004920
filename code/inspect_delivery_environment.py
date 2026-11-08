#!/usr/bin/env python3
"""Read-only handoff/MRIQC discovery; publish bounded technical inventory."""
import csv
from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path('/ZPOOL/data/projects/istart-ds004920')


def inspect():
    config = json.loads((ROOT / 'config/linux2.json').read_text())
    report = {'mriqc': {}, 'containers': [], 'executables': {name: shutil.which(name) for name in ('apptainer', 'singularity', 'mriqc')},
              'source_qc_tables': [], 'images_modified': False}
    candidates = set()
    for base in (Path('/ZPOOL/data/tools'), Path('/data/tools')):
        if base.is_dir():
            for pattern in ('*mriqc*.simg', '*mriqc*.sif', '*/*mriqc*.simg', '*/*mriqc*.sif'):
                candidates.update(p for p in base.glob(pattern) if p.is_file())
    runtime = report['executables']['apptainer'] or report['executables']['singularity']
    for path in sorted(candidates):
        row = {'path': str(path), 'bytes': path.stat().st_size}
        if runtime:
            try:
                result = subprocess.run([runtime, 'exec', '--cleanenv', str(path), 'mriqc', '--version'], capture_output=True, text=True, timeout=60)
                row.update(version_output=(result.stdout + result.stderr)[-2000:], returncode=result.returncode)
            except subprocess.TimeoutExpired:
                row['version_error'] = 'Version check timed out'
        report['containers'].append(row)
    for dataset in ('istart', 'rf1'):
        upstream = Path(config[dataset + '_root'])
        roots = set()
        for base in (upstream, upstream / 'derivatives', upstream / 'bids/derivatives', upstream / 'qc'):
            if base.is_dir():
                roots.update(p for p in base.glob('*mriqc*') if p.is_dir())
        outputs = []
        versions = set()
        for base in sorted(roots):
            for path in base.rglob('*_bold.json'):
                if path.is_symlink():
                    continue
                data = json.loads(path.read_text())
                if 'fd_mean' not in data or 'tsnr' not in data:
                    continue
                metadata = data.get('bids_meta', {})
                task = metadata.get('task_id') or metadata.get('TaskName')
                if task not in ('doors', 'socialdoors') and not any(f'_task-{t}_' in path.name for t in ('doors', 'socialdoors')):
                    continue
                version = str(data.get('provenance', {}).get('version', 'unknown'))
                versions.add(version)
                outputs.append({'file': str(path), 'version': version})
        report['mriqc'][dataset] = {'roots': list(map(str, sorted(roots))), 'bold_iqm_files': len(outputs), 'versions': sorted(versions), 'files': outputs[:2000]}
        path = upstream / 'qc/run_qc.tsv'
        if path.is_file():
            with path.open() as stream:
                reader = csv.DictReader(stream, delimiter='\t')
                rows = list(reader)
                report['source_qc_tables'].append({'dataset': dataset, 'path': str(path), 'fields': reader.fieldnames,
                                                  'rows': len(rows)})
    return report


def main():
    report = inspect()
    target = ROOT / 'work/delivery-environment.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
