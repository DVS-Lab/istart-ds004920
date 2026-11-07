#!/usr/bin/env bash
# Run from the production checkout after pulling the matching source commit.
set -euo pipefail
cd /ZPOOL/data/projects/istart-ds004920
PY="$PWD/.venv-preproc/bin/python"
export PATH="$PWD/.venv-preproc/bin:$PATH"

"$PY" code/preprocess_istart.py check --name pilot
"$PY" code/prepare_seed_masks.py
"$PY" code/preprocess_istart.py capacity --max-jobs 10 --nprocs 4 --omp 2 --mem-mb 0

# Push actual pilot state and log tails now; authentication errors leave the
# preprocessing data and cached work untouched. No imaging products are uploaded.
"$PY" code/publish_processing_status.py publish

if [[ ! -s work/preprocessing-runs/pilot/review.json ]]; then
  printf '\nInspect anatomy, distortion correction, coverage and alignment for both tasks:\n%s\n' \
    "$PWD/derivatives/fmriprep/sub-1002.html"
  read -r -p 'After inspecting the report, enter your actual technical findings (empty input stops here): ' PILOT_NOTES
  test -n "$PILOT_NOTES"
  "$PY" code/preprocess_istart.py accept-pilot --reviewer "$USER" --notes "$PILOT_NOTES"
fi

# New name preserves older two-worker batch specifications. Already successful
# subjects are checked and skipped; interrupted subjects reuse cached work.
"$PY" code/preprocess_istart.py launch --name cohort-expanded --acquire \
  --jobs 10 --nprocs 4 --omp 2 --mem-mb 0
"$PY" code/publish_processing_status.py start --batch cohort-expanded --interval 300
"$PY" code/preprocess_istart.py status --name cohort-expanded
"$PY" code/publish_processing_status.py status
printf '\nGitHub handoff: https://github.com/DVS-Lab/istart-ds004920/tree/linux2-status\n'
printf 'Progress log: %s/logs/preprocessing-cohort-expanded.log\n' "$PWD"
printf 'Publisher log: %s/logs/github-status-publisher.log\n' "$PWD"
