import os
from pathlib import Path
import subprocess

import pytest

from harmonized.common import ROOT


@pytest.fixture
def helper(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    script = code / "continue_preprocessing.sh"
    script.write_text((ROOT / "code/continue_preprocessing.sh").read_text())
    python = tmp_path / ".venv-preproc/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$TEST_CALLS"
case "$*" in
  *"check --name pilot"*) exit "${TEST_PILOT_RC:-0}" ;;
  *"launch --name cohort-expanded"*) exit "${TEST_LAUNCH_RC:-0}" ;;
esac
exit 0
''')
    python.chmod(0o755)
    env = dict(os.environ, TEST_CALLS=str(tmp_path / "calls.txt"))
    return script, env, tmp_path


@pytest.mark.parametrize("stdin", ["", "\n", "  \n"])
def test_empty_or_closed_input_pauses_without_failure_or_launch(helper, stdin):
    script, env, root = helper
    result = subprocess.run(["bash", str(script)], input=stdin, text=True, capture_output=True, env=env)
    assert result.returncode == 0 and "launch is paused" in result.stdout
    calls = (root / "calls.txt").read_text()
    assert "accept-pilot" not in calls and "launch --name cohort-expanded" not in calls


def test_real_notes_reach_ten_subject_launch(helper):
    script, env, root = helper
    result = subprocess.run(["bash", str(script)], input="Anatomy and both task alignments reviewed.\n", text=True, capture_output=True, env=env)
    assert result.returncode == 0
    calls = (root / "calls.txt").read_text()
    assert "accept-pilot" in calls and "--jobs 10 --nprocs 4 --omp 2 --mem-mb 0" in calls
    assert "start --batch cohort-expanded --interval 300" in calls


def test_failure_stops_helper_but_parent_shell_survives(helper):
    script, env, root = helper
    env["TEST_PILOT_RC"] = "1"
    result = subprocess.run(["bash", "-c", 'set +e; bash "$1"; printf "PARENT_ALIVE\\n"', "test", str(script)],
                            input="", text=True, capture_output=True, env=env)
    assert result.returncode == 0 and "PARENT_ALIVE" in result.stdout
    calls = (root / "calls.txt").read_text()
    assert "capacity" not in calls and "launch --name cohort-expanded" not in calls


def test_failed_launch_does_not_claim_publisher_started(helper):
    script, env, root = helper
    env["TEST_LAUNCH_RC"] = "1"
    result = subprocess.run(["bash", str(script)], input="Actual visual review passed.\n", text=True, capture_output=True, env=env)
    assert result.returncode == 1
    assert "start --batch" not in (root / "calls.txt").read_text()
