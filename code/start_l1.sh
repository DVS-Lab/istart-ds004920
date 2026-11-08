#!/usr/bin/env bash
# Execute in a child shell; errors never change the interactive shell's options.
start_l1() {
  local project_root
  project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" || return 1
  cd "$project_root" || return 1
  if [[ ! -x .venv/bin/python ]]; then
    .venv-preproc/bin/python -m venv .venv || return 1
  fi
  if ! .venv/bin/python -c 'import numpy, pandas, nibabel, scipy, nitransforms' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -r requirements.txt || return 1
  fi
  .venv/bin/python code/run_harmonized_l1.py launch --phase pilot --jobs 10
}

start_l1 "$@"
