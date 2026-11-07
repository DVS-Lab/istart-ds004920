#!/usr/bin/env bash
# Idempotent Priority-0 pilot setup. It never waits on downstream L1 work.
set -euo pipefail
cd /ZPOOL/data/projects/istart-ds004920
mkdir -p logs work
if [[ ! -x .venv-preproc/bin/python ]]; then
    python3 -m venv .venv-preproc
fi
.venv-preproc/bin/python -m pip install -r requirements-preproc.txt
export PATH="$PWD/.venv-preproc/bin:$PATH"
export PYTHONUNBUFFERED=1
command -v git-annex
command -v apptainer || command -v singularity
test -f /ZPOOL/data/tools/fmriprep-25.2.5.simg
test -r /ZPOOL/data/tools/licenses/fs_license.txt
test -s /ZPOOL/data/tools/licenses/fs_license.txt
.venv-preproc/bin/python -m pip freeze > work/preprocessing-environment.txt
.venv-preproc/bin/python code/preprocess_istart.py fetch --subjects 1002 2>&1 | tee logs/download-sub-1002.log
.venv-preproc/bin/python code/preprocess_istart.py validate --subjects 1002 \
  --nprocs 16 --omp 2 --mem-mb 24000 2>&1 | tee logs/validate-sub-1002.log
.venv-preproc/bin/python code/preprocess_istart.py launch --name pilot --subjects 1002 \
  --jobs 1 --nprocs 16 --omp 2 --mem-mb 24000
.venv-preproc/bin/python code/preprocess_istart.py status --name pilot
printf '\nPilot launched detached. State: work/preprocessing-runs/pilot/state.json\n'
printf 'Log: /ZPOOL/data/projects/istart-ds004920/logs/preprocessing-pilot.log\n'
