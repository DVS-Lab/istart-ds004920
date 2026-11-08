#!/usr/bin/env python3
"""Run missing Doors/Social Doors MRIQC using the existing Linux2 container."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path('/ZPOOL/data/projects/istart-ds004920')
sys.path.insert(0, str(ROOT / 'code'))
from harmonized.common import lock, now, read_table, sha, stamp, table, verify, write_json
from prepare_chloe_handoff import alive, publish
from inspect_delivery_environment import inspect

CONTAINER = Path('/ZPOOL/data/tools/mriqc-24.0.2.simg')


def folder():
    return ROOT / 'work/mriqc-runs/istart'


def iqm(unit):
    paths = list((ROOT / 'derivatives/mriqc' / unit['subject'] / 'func').glob(f"{unit['stem']}_bold.json"))
    if len(paths) != 1:
        return None
    import math
    try:
        data = json.loads(paths[0].read_text())
        fd, tsnr = float(data['fd_mean']), float(data['tsnr'])
        if not math.isfinite(fd) or not math.isfinite(tsnr) or fd < 0 or tsnr <= 0:
            return None
        return {'file': str(paths[0]), 'sha256': sha(paths[0]), 'fd_mean': fd, 'tsnr': tsnr,
                'version': data.get('provenance', {}).get('version', 'unknown')}
    except (ValueError, KeyError, TypeError):
        return None


def command(runtime, unit):
    output = ROOT / 'derivatives/mriqc'
    scratch = ROOT / 'work/mriqc' / unit['stem']
    cache = ROOT / 'work/templateflow'
    for path in (output, scratch, cache):
        path.mkdir(parents=True, exist_ok=True)
    return [runtime, 'run', '--cleanenv', '--bind', f'{ROOT / "bids"}:/data:ro',
            '--bind', f'{output}:/out', '--bind', f'{scratch}:/work', '--bind', f'{cache}:/opt/templateflow',
            '--env', 'TEMPLATEFLOW_HOME=/opt/templateflow', '--env', 'OPENBLAS_NUM_THREADS=1',
            str(CONTAINER), '/data', '/out', 'participant', '--participant-label', unit['subject'].removeprefix('sub-'),
            '--task-id', unit['task'], '-m', 'bold', '--nprocs', '4', '--omp-nthreads', '2',
            '--no-sub', '--bids-database-dir', '/work/bids-db', '-w', '/work']


def worker():
    statefile = folder() / 'state.json'
    with (folder() / 'launch.lock').open('a') as barrier:
        import fcntl
        fcntl.flock(barrier, fcntl.LOCK_EX)
    state = json.loads(statefile.read_text())
    stop = threading.Event()
    def monitor():
        while not stop.is_set():
            publish()
            stop.wait(300)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        with lock(folder() / 'worker.lock'):
            write_json(ROOT / 'work/delivery-environment.json', inspect(probe_containers=False))
            runtime = shutil.which('singularity') or shutil.which('apptainer')
            if not runtime or not CONTAINER.is_file():
                raise ValueError('Existing Singularity runtime or /ZPOOL/data/tools/mriqc-24.0.2.simg is unavailable')
            state.update(status='started', operation='checking_existing_container')
            write_json(statefile, state)
            # The quick inventory timed out at 60 seconds; allow initialization
            # to finish in this detached process with BLAS threads bounded.
            probe = subprocess.run([runtime, 'exec', '--cleanenv', '--env', 'OPENBLAS_NUM_THREADS=1',
                                    str(CONTAINER), 'mriqc', '--version'], capture_output=True, text=True, timeout=300)
            if probe.returncode:
                raise ValueError('Existing MRIQC container failed: ' + (probe.stdout + probe.stderr)[-4000:])
            print('MRIQC version: ' + probe.stdout.strip(), flush=True)
            help_result = subprocess.run([runtime, 'exec', '--cleanenv', '--env', 'OPENBLAS_NUM_THREADS=1',
                                          str(CONTAINER), 'mriqc', '--help'], capture_output=True, text=True, timeout=300, check=True)
            if any(flag not in help_result.stdout for flag in ('--task-id', '--bids-database-dir', '--no-sub', '--omp-nthreads')):
                raise ValueError('Installed MRIQC CLI differs from the existing 24.0.2 wrapper')
            _, roster = read_table(ROOT / 'bids/participants.tsv')
            units, missing = [], []
            for person in roster:
                subject = person['participant_id']
                for task in ('doors', 'socialdoors'):
                    paths = list((ROOT / 'bids' / subject / 'func').glob(f'{subject}_task-{task}_run-1_bold.nii.gz'))
                    if len(paths) != 1:
                        missing.append({'subject': subject, 'task': task, 'reason': f'Expected one raw BOLD, found {len(paths)}'})
                        continue
                    unit = {'subject': subject, 'task': task, 'stem': paths[0].name.removesuffix('_bold.nii.gz'),
                            'inputs': [stamp(paths[0], full=False), stamp(paths[0].with_name(paths[0].name.replace('.nii.gz', '.json')), full=True)]}
                    units.append(unit)
            if not units:
                raise ValueError('No task MRIQC inputs found')
            spec = {'container': stamp(CONTAINER, full=True), 'version': probe.stdout.strip(), 'units': units,
                    'missing_raw_runs': missing, 'subjects_parallel': 8, 'nprocs_each': 4, 'omp_each': 2, 'no_sub': True}
            specfile = folder() / 'spec.json'
            if specfile.exists() and json.loads(specfile.read_text()) != spec:
                raise ValueError('MRIQC inputs/container changed; existing work preserved')
            if not specfile.exists():
                write_json(specfile, spec, exclusive=True)
            def run(unit):
                verify(unit['inputs'])
                existing = iqm(unit)
                if existing:
                    return {'subject': unit['subject'], 'task': unit['task'], 'status': 'verified', 'iqm': existing}
                log = ROOT / 'logs/mriqc' / (unit['stem'] + '.log')
                log.parent.mkdir(parents=True, exist_ok=True)
                argv = command(runtime, unit)
                with log.open('a') as out:
                    out.write(json.dumps({'launched': now(), 'argv': argv}) + '\n')
                    out.flush()
                    subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT, check=True)
                result = iqm(unit)
                if not result:
                    raise ValueError(f'MRIQC did not produce valid FD/tSNR: {unit["stem"]}; see {log}')
                return {'subject': unit['subject'], 'task': unit['task'], 'status': 'verified', 'iqm': result}
            pilot = next((u for u in units if u['subject'] == 'sub-1002' and u['task'] == 'doors'), units[0])
            state.update(operation='pilot:sub-1002_doors', runs_total=len(units), runs_completed=0)
            write_json(statefile, state)
            results = [run(pilot)]
            write_json(folder() / 'pilot.json', results[0])
            # No manual gate: valid pilot IQMs release the rest automatically.
            state.update(operation='cohort', runs_completed=1)
            write_json(statefile, state)
            def subject_run(group):
                output = []
                for unit in group:
                    try:
                        output.append(run(unit))
                    except Exception as exc:
                        output.append({'subject': unit['subject'], 'task': unit['task'], 'status': 'failed', 'reason': str(exc)})
                return output
            groups = {}
            for unit in units:
                if unit is not pilot:
                    groups.setdefault(unit['subject'], []).append(unit)
            with ThreadPoolExecutor(max_workers=8) as pool:
                for future in as_completed([pool.submit(subject_run, group) for group in groups.values()]):
                    results.extend(future.result())
                    state.update(runs_completed=len(results), failed=sum(r['status'] == 'failed' for r in results))
                    write_json(statefile, state)
                    write_json(folder() / 'results.json', results)
            write_json(folder() / 'results.json', results)
            table(folder() / 'missing-raw-runs.tsv', missing)
            # Inventory the new IQMs without probing the containers again.
            environment = inspect(probe_containers=False)
            write_json(ROOT / 'work/delivery-environment.json', environment)
            state.update(status='complete' if all(r['status'] == 'verified' for r in results) else 'finished_with_failures',
                         operation='refreshing_handoff', finished=now())
            write_json(statefile, state)
            # Refresh staged QC after the initial copy job exits, reusing images.
            packaging_state = ROOT / 'work/delivery-runs/chloe/state.json'
            while packaging_state.exists() and alive(json.loads(packaging_state.read_text())):
                time.sleep(10)
            subprocess.run([sys.executable, str(Path(__file__).with_name('prepare_chloe_handoff.py')), 'launch'], check=True)
            state['operation'] = 'complete'
    except Exception as exc:
        state.update(status='failed', error=str(exc), finished=now())
        print(f'{now()} ERROR: {exc}', flush=True)
    finally:
        write_json(statefile, state)
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
    folder().mkdir(parents=True, exist_ok=True)
    statefile = folder() / 'state.json'
    if args.action == 'worker':
        return worker()
    if args.action == 'status':
        print(statefile.read_text() if statefile.exists() else 'Not launched')
        return 0
    with lock(folder() / 'launch.lock'):
        old = json.loads(statefile.read_text()) if statefile.exists() else {}
        if alive(old) or old.get('status') == 'complete':
            print(json.dumps(old, indent=2))
            return 0
        log = ROOT / 'logs/istart-mriqc.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(Path(__file__).resolve()), 'worker']
        with log.open('a') as out, open(os.devnull) as null:
            child = subprocess.Popen(argv, stdin=null, stdout=out, stderr=subprocess.STDOUT,
                                     start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED='1'))
        state = {'pid': child.pid, 'worker_script': Path(__file__).name, 'status': 'started', 'operation': 'initializing',
                 'launched': now(), 'log': str(log), 'launch_command': argv, 'subjects_parallel': 8}
        write_json(statefile, state)
        print(json.dumps(state, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
