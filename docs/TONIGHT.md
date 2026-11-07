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

## Continue the successful pilot into the cohort

The pilot has passed both task structural checks. Start the cohort directly:

```bash
set +e
cd /ZPOOL/data/projects/istart-ds004920 &&
git pull --ff-only &&
bash code/continue_preprocessing.sh
```

The helper starts detached acquisition/preprocessing with ten subjects, four
processes and two OpenMP threads per subject, and no explicit memory budget.
It skips verified completed subjects and reuses work caches. Visual QC is
separate and does not block launch. Status and sanitized log tails are committed
and pushed directly to `main` under `status/linux2/` every five minutes.

For an initial installation, `code/start_tonight.sh` acquires sub-1002, validates
the pinned command and launches the detached pilot. After its structural checks
pass, use the continuation command above. No smoothing, PPI or L1 is required.

See [PROCESSING_HANDOFF.txt](PROCESSING_HANDOFF.txt) for exact status, completion,
PID/log and failure-resume commands.

`git-annex` and either Apptainer or Singularity must already be available in the Linux2 shell. The wrapper reuses the existing RF1 production license exactly: `/ZPOOL/data/tools/licenses` is bound to `/opts`, and fMRIPrep receives `--fs-license-file /opts/fs_license.txt`. The license must be readable and nonempty even though `--fs-no-reconall` remains enabled. No new license is generated, copied into the repository, or published. `start_tonight.sh` installs the three Python preprocessing dependencies in a dedicated environment. It does not install system packages or alter the container. If a prerequisite check fails, processing is not claimed to have started; fix that specific prerequisite and rerun the same block.

Pilot supervisor state: `work/preprocessing-runs/pilot/state.json`; cohort supervisor state: `work/preprocessing-runs/cohort-expanded/state.json`. Each includes PID, actual launch command, start/end time and supervisor log. `logs/fmriprep/sub-1002.log` contains the actual container invocation and output. Other participants get the analogous log. Per-subject provenance, container hash, original staged input signatures and completion records are under `work/preprocessing/`. Separate per-subject fMRIPrep work directories make simultaneous subjects safe to resume.

Completion checks require successful fMRIPrep command receipts, the 25.2.5 derivative dataset description, nonempty participant HTML reports, finite 4D MNI BOLD with TR 1.75 s for both tasks, matching binary masks and confound row counts. Visual inspection remains separate. The cohort result table counts both successful and failed attempts; missing raw inputs remain visible in `input_inventory.tsv`. A batch does not assign scientific inclusion decisions.

After SSH reconnects, the `status`, `launch`, `wait` and `check` commands above can be rerun directly. Do not edit processing scripts/configuration while a saved batch is active; their identities are frozen. Keep `work/fmriprep/` to resume failures. An unfinished stage created by interrupted data acquisition is preserved for inspection instead of overwritten.
