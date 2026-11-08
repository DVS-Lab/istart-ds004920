import importlib.util
import json
from pathlib import Path

import pytest

from harmonized.common import ROOT, write_json

spec = importlib.util.spec_from_file_location('finish_sub10657', ROOT / 'code/finish_sub10657.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def setup_receipt(tmp_path, value=5.3761):
    unit = next(iter(review.EXPECTED))
    values = {k: value for k in ('classic_fwhm_combined', 'classic_fwhm_x', 'classic_fwhm_y', 'classic_fwhm_z')}
    rec = {'unit': unit, 'method': 'default-blurmaster-classic6-v1', 'target_mm': 6,
           'bmall': False, 'status': 'outside_tolerance', 'after': values}
    write_json(tmp_path / 'receipt.json', rec)
    return {'unit': unit, 'spatial': str(tmp_path)}, rec, values


def test_review_only_relaxes_exact_tolerance_error_after_strict_checks(tmp_path, monkeypatch):
    job, rec, values = setup_receipt(tmp_path)
    monkeypatch.setattr(review.workflow, 'measure_values', lambda p: values)
    calls = []
    def strict(job, plan):
        calls.append(job['unit'])
        raise ValueError(review.STRICT_ERROR)
    verify = review.reviewed_verifier(strict)
    assert verify(job, {}) == rec and calls == [job['unit']]
    # Seed models must use their matching activation unit, never their own ID.
    assert verify(dict(job, activation_unit=job['unit'], unit=job['unit'] + '_seed-IPL'), {}) == rec
    with pytest.raises(ValueError, match='Smoothing outside'):
        verify(dict(job, unit='rf1_sub-other'), {})
    def failed_hash(job, plan):
        raise ValueError('Frozen input changed')
    with pytest.raises(ValueError, match='Frozen input changed'):
        review.reviewed_verifier(failed_hash)(job, {})


def test_review_rejects_changed_measurement_even_if_within_fifteen_percent(tmp_path, monkeypatch):
    job, rec, values = setup_receipt(tmp_path, 5.3)
    monkeypatch.setattr(review.workflow, 'measure_values', lambda p: values)
    def strict(job, plan):
        raise ValueError(review.STRICT_ERROR)
    with pytest.raises(ValueError, match='specifically reviewed'):
        review.reviewed_verifier(strict)(job, {})


def test_review_selects_only_two_runs_and_their_twelve_seeds():
    jobs = [{'unit': unit, 'dataset': 'rf1', 'subject': '10657', 'session': '01'} for unit in review.EXPECTED]
    assert review.targets({'jobs': jobs + [dict(jobs[0], unit='rf1_other')]}) == jobs
    ppi = [dict(job, activation_unit=job['unit'], unit=job['unit'] + f'_seed-{n}') for job in jobs for n in range(6)]
    assert len(review.targets({'model': 'ppi', 'jobs': ppi})) == 12
    with pytest.raises(ValueError, match='exactly two'):
        review.targets({'model': 'ppi', 'jobs': ppi[:-1]})
