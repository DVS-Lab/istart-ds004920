#!/usr/bin/env bash
# Launch directly; technical checks are performed by the preprocessing launcher.
continue_preprocessing() {
  local project_root PY
  project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" || return 1
  cd "$project_root" || return 1
  PY="$PWD/.venv-preproc/bin/python"
  export PATH="$PWD/.venv-preproc/bin:$PATH"

  "$PY" code/preprocess_istart.py launch --name cohort-expanded --acquire \
    --jobs 10 --nprocs 4 --omp 2 --mem-mb 0 || return 1
  if ! "$PY" code/publish_processing_status.py start --batch cohort-expanded --interval 300; then
    printf 'Status publisher needs attention; preprocessing continues detached.\n' >&2
  fi
  "$PY" code/preprocess_istart.py status --name cohort-expanded
  printf '\nStatus: https://github.com/DVS-Lab/istart-ds004920/tree/main/status/linux2\n'
  printf 'Log: %s/logs/preprocessing-cohort-expanded.log\n' "$PWD"
}

continue_preprocessing "$@"
