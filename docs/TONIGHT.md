# Priority 0: preprocessing tonight

## Restart the pilot after the missing-license failure

The original wrapper omitted the production FreeSurfer license argument. The corrected wrapper reuses `/ZPOOL/data/tools/licenses/fs_license.txt` through the existing RF1 `/opts` bind; `--fs-no-reconall` remains enabled. If sub-1002 failed with the license error, run:

```bash
cd /ZPOOL/data/projects/istart-ds004920
git pull --ff-only
export PATH="$PWD/.venv-preproc/bin:$PATH"
.venv-preproc/bin/python code/restart_pilot_after_license_fix.py
tail -n 50 logs/preprocessing-pilot.log logs/fmriprep/sub-1002.log
```

The helper first validates the corrected command and existing license. It refuses to proceed if the pilot PID or subject/worker locks are active. Otherwise, it archives the failed pilot's frozen batch metadata and a copy of its original command receipt, then relaunches detached with the same BIDS, derivative and per-subject work directories. It does not delete or redownload data, clear cached work, or change RF1. After the pilot finishes, use the `wait`, `check`, inspection and cohort commands below.

The code is prepared locally; no Linux2 job has been launched by Codex. These commands create the production checkout, acquire only sub-1002's required data first, dry-run the exact pinned container command and launch its fMRIPrep process detached. They do not require FSL, AFNI, smoothing, L1, TEDANA or the motion diagnostic. Two concurrent cohort subjects each request 16 processes, 2 OpenMP threads and 24 GB, leaving CPU capacity within the 96-processor host. The launcher checks current available memory and refuses an oversubscribed budget.

Paste this whole block into Bash on Linux2. The pilot remains alive if SSH closes. The block waits for its structural checks and asks for your actual HTML-report inspection before starting the detached cohort download/preprocessing. Press Ctrl-C at the prompt or wait to return to it later; that does not stop an already detached pilot. Do not answer yes without reviewing the report. All paths and participant labels below are concrete.

```bash
cd /ZPOOL/data/projects
if [[ -d istart-ds004920/.git ]]; then
  git -C istart-ds004920 pull --ff-only
else
  git clone https://github.com/DVS-Lab/istart-ds004920.git
fi
cd /ZPOOL/data/projects/istart-ds004920
bash code/start_tonight.sh
export PATH="/ZPOOL/data/projects/istart-ds004920/.venv-preproc/bin:$PATH"
export PY="/ZPOOL/data/projects/istart-ds004920/.venv-preproc/bin/python"
export SCRIPT="/ZPOOL/data/projects/istart-ds004920/code/preprocess_istart.py"

# This waits for the detached pilot; closing SSH does not stop fMRIPrep.
"$PY" "$SCRIPT" wait --name pilot
"$PY" "$SCRIPT" check --name pilot
cat /ZPOOL/data/projects/istart-ds004920/work/preprocessing-runs/pilot/state.json
printf '\nInspect this HTML report before continuing:\n%s\n' \
  /ZPOOL/data/projects/istart-ds004920/derivatives/fmriprep/sub-1002.html
read -r -p 'Have you inspected sub-1002 anatomy, GRE correction, alignment and both tasks, and does the pilot pass? Type yes: ' PILOT_PASS
if [[ "$PILOT_PASS" != yes ]]; then
  printf 'Cohort has not been launched. The detached pilot and its outputs are preserved.\n'
else
  read -r -p 'Enter your actual pilot inspection notes: ' PILOT_NOTES
  test -n "$PILOT_NOTES"
  "$PY" "$SCRIPT" accept-pilot --reviewer "$USER" --notes "$PILOT_NOTES"
  # Acquisition of the remaining subjects is also inside the detached process.
  "$PY" "$SCRIPT" launch --name cohort --acquire \
    --jobs 2 --nprocs 16 --omp 2 --mem-mb 24000
  "$PY" "$SCRIPT" status --name cohort
fi

# Progress and the exact PID, log and launch command are in these state files.
"$PY" "$SCRIPT" status --name pilot
if [[ -f /ZPOOL/data/projects/istart-ds004920/work/preprocessing-runs/cohort/state.json ]]; then
  "$PY" "$SCRIPT" status --name cohort
  tail -n 30 /ZPOOL/data/projects/istart-ds004920/logs/preprocessing-cohort.log
fi
tail -n 30 /ZPOOL/data/projects/istart-ds004920/logs/fmriprep/sub-1002.log

# Resume commands: running batches are reported without duplicate launches;
# completed subjects are verified/skipped, failed subjects reuse their work cache.
"$PY" "$SCRIPT" launch --name pilot --subjects 1002 \
  --jobs 1 --nprocs 16 --omp 2 --mem-mb 24000
if [[ -f /ZPOOL/data/projects/istart-ds004920/work/preprocessing-runs/pilot/review.json ]]; then
  "$PY" "$SCRIPT" launch --name cohort --acquire \
    --jobs 2 --nprocs 16 --omp 2 --mem-mb 24000
fi

# Run these final checks once the cohort finishes; they wait if still running.
if [[ -f /ZPOOL/data/projects/istart-ds004920/work/preprocessing-runs/cohort/state.json ]]; then
  "$PY" "$SCRIPT" wait --name cohort
  "$PY" "$SCRIPT" check --name cohort
  cat /ZPOOL/data/projects/istart-ds004920/work/preprocessing-runs/cohort/results.tsv
fi
```

`git-annex` and either Apptainer or Singularity must already be available in the Linux2 shell. The wrapper reuses the existing RF1 production license exactly: `/ZPOOL/data/tools/licenses` is bound to `/opts`, and fMRIPrep receives `--fs-license-file /opts/fs_license.txt`. The license must be readable and nonempty even though `--fs-no-reconall` remains enabled. No new license is generated, copied into the repository, or published. `start_tonight.sh` installs the three Python preprocessing dependencies in a dedicated environment. It does not install system packages or alter the container. If a prerequisite check fails, processing is not claimed to have started; fix that specific prerequisite and rerun the same block.

Pilot supervisor state: `work/preprocessing-runs/pilot/state.json`; cohort supervisor state: `work/preprocessing-runs/cohort/state.json`. Each includes PID, actual launch command, start/end time and supervisor log. `logs/fmriprep/sub-1002.log` contains the actual container invocation and output. Other participants get the analogous log. Per-subject provenance, container hash, original staged input signatures and completion records are under `work/preprocessing/`. Separate per-subject fMRIPrep work directories make two simultaneous subjects safe to resume.

Completion checks require successful fMRIPrep command receipts, the 25.2.5 derivative dataset description, nonempty participant HTML reports, finite 4D MNI BOLD with TR 1.75 s for both tasks, matching binary masks and confound row counts. Visual inspection remains separate. The cohort result table counts both successful and failed attempts; missing raw inputs remain visible in `input_inventory.tsv`. A batch does not assign scientific inclusion decisions.

After SSH reconnects, the `status`, `launch`, `wait` and `check` commands above can be rerun directly. Do not edit processing scripts/configuration while a saved batch is active; their identities are frozen. Keep `work/fmriprep/` to resume failures. An unfinished stage created by interrupted data acquisition is preserved for inspection instead of overwritten.
