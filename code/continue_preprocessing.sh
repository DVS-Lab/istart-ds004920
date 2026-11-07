#!/usr/bin/env bash
# Execute with bash; this script does not set options in the caller's shell.
continue_preprocessing() {
  local project_root PY PILOT_NOTES
  project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" || return 1
  cd "$project_root" || return 1
  PY="$PWD/.venv-preproc/bin/python"
  export PATH="$PWD/.venv-preproc/bin:$PATH"

  if ! "$PY" code/preprocess_istart.py check --name pilot; then
    printf 'Pilot structural checks failed. Inspect the error above; no cohort was launched.\n' >&2
    return 1
  fi
  if ! "$PY" code/prepare_seed_masks.py; then
    printf 'Seed-mask validation needs review; preprocessing can continue independently.\n' >&2
  fi
  "$PY" code/preprocess_istart.py capacity --max-jobs 10 --nprocs 4 --omp 2 --mem-mb 0 || return 1

  if ! "$PY" code/publish_processing_status.py publish; then
    printf 'GitHub status publication failed; its error is above. Preprocessing remains available.\n' >&2
  fi

  if [[ ! -s work/preprocessing-runs/pilot/review.json ]]; then
    printf '\nInspect anatomy, distortion correction, coverage and alignment for both tasks:\n%s\n' \
      "$PWD/derivatives/fmriprep/sub-1002.html"
    if ! read -r -p 'After inspecting the report, enter your actual technical findings (empty input pauses launch): ' PILOT_NOTES; then
      printf '\nNo review findings entered. Cohort launch is paused; rerun this helper after inspection.\n'
      return 0
    fi
    if [[ -z "${PILOT_NOTES//[[:space:]]/}" ]]; then
      printf 'No review findings entered. Cohort launch is paused; rerun this helper after inspection.\n'
      return 0
    fi
    "$PY" code/preprocess_istart.py accept-pilot --reviewer "${USER:-linux2}" --notes "$PILOT_NOTES" || return 1
  fi

  "$PY" code/preprocess_istart.py launch --name cohort-expanded --acquire \
    --jobs 10 --nprocs 4 --omp 2 --mem-mb 0 || return 1
  if ! "$PY" code/publish_processing_status.py start --batch cohort-expanded --interval 300; then
    printf 'Cohort launch succeeded, but the status publisher needs attention. Preprocessing continues detached.\n' >&2
  fi
  "$PY" code/preprocess_istart.py status --name cohort-expanded || return 1
  "$PY" code/publish_processing_status.py status
  printf '\nGitHub handoff: https://github.com/DVS-Lab/istart-ds004920/tree/linux2-status\n'
  printf 'Progress log: %s/logs/preprocessing-cohort-expanded.log\n' "$PWD"
  printf 'Publisher log: %s/logs/github-status-publisher.log\n' "$PWD"
}

continue_preprocessing "$@"
