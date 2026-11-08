#!/usr/bin/env python3
"""Finish two reviewed smoothness exceptions using unchanged frozen L1 plans.

Only the two exact measurements reviewed on 2026-11-08 are accepted. This
adapter retains the original spatial verifier and all of its product hashes,
geometry, method and input checks. No images, plans or existing fits are edited.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path('/ZPOOL/data/projects/istart-ds004920')
sys.path.insert(0, str(ROOT / 'code'))
from harmonized.common import lock, now, sha, stamp, write_json
from harmonized.environment import configure
from harmonized.inputs import load_plan
from harmonized import workflow

EXPECTED = {
    'rf1_sub-10657_ses-01_task-doors_run-1': 5.3761,
    'rf1_sub-10657_ses-01_task-socialdoors_run-1': 5.3654,
}
STRICT_ERROR = 'Smoothing outside 5.4–6.6 mm: review required; no automatic retry'


def folder():
    return ROOT / 'work/l1-runs/sub-10657-review'


def alive(state):
    pid = state.get('pid')
    if not pid:
        return False
    path = Path(f'/proc/{pid}/cmdline')
    if not path.exists():
        return False
    args = path.read_bytes().decode(errors='replace').split('\0')
    return any(Path(arg).name == state.get('worker_script', 'run_harmonized_l1.py') for arg in args)


def reviewed_verifier(strict):
    def verify(job, plan):
        try:
            return strict(job, plan)
        except ValueError as exc:
            unit = job.get('activation_unit', job['unit'])
            if str(exc) != STRICT_ERROR or unit not in EXPECTED:
                raise
            # The strict verifier has already checked source identity, the
            # complete product hashes, geometry and measurement validity.
            d = Path(job['spatial'])
            rec = json.loads((d / 'receipt.json').read_text())
            actual = workflow.measure_values(d / 'after.tsv')
            if (rec.get('unit') != unit or rec.get('method') != 'default-blurmaster-classic6-v1'
                    or rec.get('target_mm') != 6 or rec.get('bmall') is not False
                    or rec.get('status') != 'outside_tolerance'
                    or actual != rec['after']
                    or abs(actual['classic_fwhm_combined'] - EXPECTED[unit]) > 1e-7
                    or not all(5.1 <= actual[k] <= 6.9 for k in
                               ('classic_fwhm_combined', 'classic_fwhm_x', 'classic_fwhm_y', 'classic_fwhm_z'))):
                raise ValueError('Run differs from the specifically reviewed smoothness exception') from exc
            return rec
    return verify


def targets(plan):
    jobs = [job for job in plan['jobs'] if job.get('activation_unit', job['unit']) in EXPECTED]
    expected = 12 if plan.get('model') == 'ppi' else 2
    if len(jobs) != expected or {job.get('activation_unit', job['unit']) for job in jobs} != set(EXPECTED):
        raise ValueError('Expected exactly two activation runs and their twelve existing seed models')
    if any(job['dataset'] != 'rf1' or job['subject'] != '10657' or job['session'] != '01' for job in jobs):
        raise ValueError('Unexpected participant or session in review targets')
    return jobs


def publish():
    publisher = Path(__file__).with_name('publish_processing_status.py')
    result = subprocess.run([sys.executable, str(publisher), 'publish', '--root', str(ROOT)])
    if result.returncode:
        print(f'{now()} Status publication failed; analysis continues', flush=True)
    return result.returncode


def run_targets(private, plan, review_file):
    jobs = targets(plan)
    plan_hash = sha(private / 'plan.json')
    results = []
    with lock(private / 'batch.lock'), ExitStack() as stack:
        roots = sorted({str(Path(j['output']).parents[2 if j['session'] else 1]) for j in jobs})
        for root in roots:
            stack.enter_context(lock(Path(root) / '.harmonized.lock'))
        def work(job):
            try:
                rec = workflow.run_l1(job, plan, private, plan_hash)
                note = stamp(review_file, full=True)
                if rec.get('smoothness_qc_review') != note:
                    if rec.get('smoothness_qc_review'):
                        raise ValueError('Existing fit records a different smoothness review')
                    rec['smoothness_qc_review'] = note
                    rec['provenance'].append(note)
                    write_json(Path(job['output']) / 'harmonized_success.json', rec)
                workflow.complete(job, plan, plan_hash)
                return {'unit': job['unit'], 'status': 'verified', 'reason': ''}
            except Exception as exc:
                return {'unit': job['unit'], 'status': 'review_or_failure', 'reason': str(exc)}
        with ThreadPoolExecutor(max_workers=14) as pool:
            for future in as_completed([pool.submit(work, job) for job in jobs]):
                row = future.result()
                results.append(row)
                print(row['unit'], row['status'], row['reason'], flush=True)
    write_json(folder() / f"{plan.get('model', 'activation')}-results.json", results)
    if any(row['status'] != 'verified' for row in results):
        raise ValueError('A reviewed model failed; existing products and diagnostics were preserved')


def worker():
    f = folder()
    with (f / 'launch.lock').open('a') as barrier:
        import fcntl
        fcntl.flock(barrier, fcntl.LOCK_EX)
    statefile = f / 'state.json'
    state = json.loads(statefile.read_text())
    stop = threading.Event()
    def monitor():
        while not stop.is_set():
            publish()
            stop.wait(300)
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        # Queue behind the original cohort; never replace or interrupt it.
        while True:
            busy = []
            for path in (ROOT / 'work/l1-runs').glob('*/state.json'):
                if path != statefile and alive(json.loads(path.read_text())):
                    busy.append(str(path.parent.name))
            if not busy:
                break
            state.update(operation='waiting_for_existing_cohort', waiting_for=busy)
            write_json(statefile, state)
            time.sleep(10)
        with lock(ROOT / 'work/l1-runs/schedule.lock'), lock(f / 'worker.lock'):
            # Recheck after obtaining the scheduler lock to close launch races.
            for path in (ROOT / 'work/l1-runs').glob('*/state.json'):
                if path != statefile and alive(json.loads(path.read_text())):
                    raise ValueError('Another L1 worker started; resume after it finishes')
            config = json.loads((ROOT / 'config/linux2.json').read_text())
            configure(config)
            private = {name: ROOT / f'work/{name}' for name in ('harmonized-l1-v1', 'harmonized-ppi-v1')}
            plans = {name: load_plan(path) for name, path in private.items()}
            workflow.verified_spatial = reviewed_verifier(workflow.verified_spatial)
            for plan in plans.values():
                for job in targets(plan):
                    workflow.verified_spatial(job, plan)
            review_file = f / 'smoothness-review.json'
            review = {'schema': 1, 'target_mm': 6, 'original_acceptance_mm': [5.4, 6.6],
                      'scope': 'Only these two existing outputs and their six-seed PPI models',
                      'basis': 'User accepted considering deviations within 15%; observed deviations are 10.40% and 10.58%',
                      'images_modified': False, 'frozen_plans_modified': False,
                      'scientific_inclusion_assigned': False,
                      'runner': {'path': str(Path(__file__).resolve()), 'sha256': sha(Path(__file__))},
                      'measurements_mm': EXPECTED,
                      'plan_sha256': {name: sha(path / 'plan.json') for name, path in private.items()}}
            if review_file.exists():
                if json.loads(review_file.read_text()) != review:
                    raise ValueError('Existing review evidence differs; no evidence overwritten')
            else:
                write_json(review_file, review, exclusive=True)
            state.update(operation='activation:reviewed_run', status='started')
            write_json(statefile, state)
            run_targets(private['harmonized-l1-v1'], plans['harmonized-l1-v1'], review_file)
            state.update(operation='ppi:reviewed_run')
            write_json(statefile, state)
            run_targets(private['harmonized-ppi-v1'], plans['harmonized-ppi-v1'], review_file)
            # Keep original reports, then verify every model against the same
            # frozen plan and strict verifier plus the two explicit exceptions.
            for name, path in private.items():
                backup = f / 'original-cohort-checks' / name
                backup.mkdir(parents=True, exist_ok=True)
                for old in path.glob('check_cohort*'):
                    dest = backup / old.name
                    if old.is_file() and not dest.exists():
                        with dest.open('xb') as out:
                            out.write(old.read_bytes())
                state.update(operation=f'{plans[name].get("model", "activation")}:check')
                write_json(statefile, state)
                if not workflow.batch(path, 'check', 'cohort', 32):
                    raise ValueError('Full cohort validation reported an issue; consult published status')
                validation = path / 'check_cohort_validation.json'
                data = json.loads(validation.read_text())
                data['smoothness_qc_review'] = stamp(review_file, full=True)
                write_json(validation, data)
            state.update(status='complete', operation='complete', finished=now(),
                         reviewed_activation=2, reviewed_ppi=12, all_cohort_checks_passed=True)
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
            if attempt < 2:
                time.sleep(5)
    return 0 if state['status'] == 'complete' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('launch', 'worker', 'status'))
    args = parser.parse_args()
    f = folder()
    f.mkdir(parents=True, exist_ok=True)
    statefile = f / 'state.json'
    if args.action == 'worker':
        return worker()
    if args.action == 'status':
        print(statefile.read_text() if statefile.exists() else 'Not launched')
        return 0
    with lock(f / 'launch.lock'):
        previous = json.loads(statefile.read_text()) if statefile.exists() else {}
        if alive(previous) or previous.get('status') == 'complete':
            print(json.dumps(previous, indent=2))
            return 0
        log = ROOT / 'logs/harmonized-l1-sub10657-review.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(Path(__file__).resolve()), 'worker']
        with log.open('a') as output, open(os.devnull) as null:
            child = subprocess.Popen(argv, stdin=null, stdout=output, stderr=subprocess.STDOUT,
                                     start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED='1'))
        state = {'pid': child.pid, 'phase': 'sub-10657-review', 'status': 'queued',
                 'operation': 'waiting_for_existing_cohort', 'launched': now(), 'jobs': 14,
                 'worker_script': Path(__file__).name, 'log': str(log), 'launch_command': argv}
        write_json(statefile, state)
        print(json.dumps(state, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
