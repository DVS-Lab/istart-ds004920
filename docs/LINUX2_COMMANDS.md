# Linux2 commands

Run these blocks in order from an FSL/AFNI-enabled Bash shell. Stop after any error and inspect the named log/report. The preprocessing participant pilot and the four-run L1 pilot are separate. Do not launch the cohort before reviewing its pilot. The default configuration includes RF1 sessions 01 and 02 independently; change that explicit scope before preparing a plan if required by your existing analysis decision.

## 1. Pull and set up the code

After the public repository is populated:

```bash
set -euo pipefail
cd /ZPOOL/data/projects
git clone https://github.com/DVS-Lab/istart-ds004920.git
cd istart-ds004920
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt datalad
export PY="$PWD/.venv/bin/python"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export AFNI_OMP_NUM_THREADS=1 FSLSUB_PARALLEL=1
test -n "${FSLDIR:-}"
command -v feat feat_model 3dBlurToFWHM 3dFWHMx 3dAllineate 3dresample afni git-annex
command -v apptainer || command -v singularity
test -f /ZPOOL/data/tools/fmriprep-25.2.5.simg
"$PY" -m pytest -q tests
"$PY" -m pip freeze > work-environment.txt
mkdir -p work logs
mv work-environment.txt work/environment.txt
cat config/linux2.json
```

Use an existing checkout with `git pull --ff-only` instead of cloning again. Do not reset a dirty checkout. Install `git-annex` using your existing Linux2 software management if it is unavailable; the Python datalad package alone does not supply the executable. Preserve this environment for the prepared plan. Actual AFNI/FSL executable identities are frozen at plan creation. Ensure the pinned container can access the TemplateFlow cache or download needed template resources.

Install a frozen copy in the RF1 downstream analysis repository, with no changes to its legacy scripts/templates/results:

```bash
"$PY" code/install_rf1_variant.py
"$PY" code/install_rf1_variant.py --execute
git -C /ZPOOL/data/projects/rf1-sra-socdoors status --short
git -C /ZPOOL/data/projects/rf1-sra-socdoors add -- code/harmonized-l1-v1/
git -C /ZPOOL/data/projects/rf1-sra-socdoors diff --cached --stat
git -C /ZPOOL/data/projects/rf1-sra-socdoors commit -m "Add isolated measured-6mm base-only Doors L1 variant"
git -C /ZPOOL/data/projects/rf1-sra-socdoors push
```

Inspect the staged diff before committing, especially if the checkout already has staged changes. The installed RF1 entry point is `code/harmonized-l1-v1/code/harmonized_l1.py`; it is a frozen copy of this shared implementation. Use one entry point consistently per plan.

## 2. Download the pinned task subset and inspect inputs

```bash
cd /ZPOOL/data/projects/istart-ds004920
export PY="$PWD/.venv/bin/python"
"$PY" code/harmonized_l1.py fetch 2>&1 | tee logs/fetch.log
git -C sourcedata/ds004920 rev-parse HEAD
"$PY" code/harmonized_l1.py inventory-preproc
column -ts $'\t' work/preprocessing_inventory.tsv | less -S
```

Expected snapshot: `2913a29de224f5364629cc39ac26045c06817b4f`. Download uses DataLad for selected task BOLD/events, T1w anatomy, GRE phasediff/magnitude data and metadata. It stages an independent `bids/` subset and filters GRE IntendedFor associations there. `sourcedata/ds004920` remains the original pinned dataset. A partially constructed staging directory is never overwritten; inspect and archive it before retrying. BIDS validation remains enabled in fMRIPrep.

## 3. Preprocess one iSTART participant, inspect, then continue

Choose the first participant with a ready inventory entry (the following command obtains its actual ID):

```bash
export PREP_SUB=$(awk -F '\t' 'NR>1 && $3=="ready" {print $1; exit}' work/preprocessing_inventory.tsv)
test -n "$PREP_SUB"
"$PY" code/harmonized_l1.py preprocess --subjects "$PREP_SUB"
"$PY" code/harmonized_l1.py preprocess --subjects "$PREP_SUB" --execute
"$PY" code/harmonized_l1.py check-preproc || true
```

At this stage, missing cohort preprocessing is expected in `work/preprocessing_completion.tsv`. Inspect `logs/fmriprep/${PREP_SUB}.log` and `derivatives/fmriprep/${PREP_SUB}.html`: anatomy/segmentation, GRE correction and registration, BOLD coverage, confounds and absence of crashes. Preserve the fMRIPrep work directory for the motion provenance if useful. After reviewing the participant pilot, run the technically available iSTART cohort sequentially:

```bash
"$PY" code/harmonized_l1.py preprocess --execute 2>&1 | tee logs/preprocess-cohort.log
"$PY" code/harmonized_l1.py check-preproc
column -ts $'\t' work/preprocessing_completion.tsv | less -S
```

The CLI uses the pinned container, `--fs-no-reconall`, `--output-spaces MNI152NLin6Asym`, the two-task BIDS filter, 8 processes/2 OpenMP threads/24 GB by default. Adjust `--nprocs`, `--omp`, `--mem-mb` for Linux2 resources. It verifies the container version is exactly 25.2.5 and records its SHA256. It does not run RF1 preprocessing, TEDANA, Warpkit, FreeSurfer recon-all or CIFTI. Successful reruns use fMRIPrep's existing cache; command success is distinct from structural and visual QC.

## 4. Review the previous five smoothing exceptions and prepare a new plan

```bash
"$PY" code/harmonized_l1.py evaluation-report --output "$PWD/work/prior-smoothing-review"
column -ts $'\t' work/prior-smoothing-review/five_run_review.tsv | less -S
```

Inspect all five live evaluation rows, including the other tasks, and their referenced images/masks/logs/receipts. Record notes in that table. This implementation does not adopt any prior evaluation output. New out-of-band results remain blocked; there is no automatic alternative blurmaster retry or new scientific exclusion.

Inspect existing RF1 source exclusions, `code/task_availability_holds.tsv`, `qc/run_qc.tsv`, and `qc/run_disposition.tsv` if present. Configuration automatically reads the latter's established path; an alternate disposition file must be configured explicitly. Optional iSTART dispositions use `config/run_disposition.tsv` with columns `subject session task run decision reason evidence` separated by tabs; session is empty for iSTART. Only existing explicit include/exclude/review/unavailable decisions belong there. There is no new age filter or QC-driven exclusion rule.

```bash
export PLAN="$PWD/work/harmonized-l1-v1"
"$PY" code/harmonized_l1.py prepare --plan "$PLAN" 2>&1 | tee logs/prepare.log
column -ts $'\t' "$PLAN/run_review.tsv" | less -S
cat "$PLAN/pilot.tsv"
```

Review ready, held, and missing/invalid rows by dataset/task/session. Confounds/EVs are created only in this new plan. Original RF1 confounds are unchanged. Technical readiness does not assign scientific inclusion. Input changes after preparation deliberately block resume. Prepare a fresh plan only after inspecting affected old products; a different plan cannot take ownership of the same existing variant outputs.

## 5. Spatial preparation, real feat_model checks and four-run FEAT pilot

```bash
"$PY" code/harmonized_l1.py spatial --plan "$PLAN" --stage pilot --jobs 2
"$PY" code/harmonized_l1.py render --plan "$PLAN" --stage pilot --jobs 1
"$PY" code/harmonized_l1.py run --plan "$PLAN" --stage pilot --jobs 2
"$PY" code/harmonized_l1.py check --plan "$PLAN" --stage pilot
cat "$PLAN/check_pilot_validation.json"
```

`render` invokes the real `feat_model` with each run's exact confound matrix. Inspect `$PLAN/designs/*/design.{fsf,mat,con}`, design graphics and `preflight.json`. Verify Win, Loss, Decision and Win minus Loss, expected TR/volume count, zero FEAT smoothing, estimable contrasts and positive residual degrees of freedom. Inspect the four new FEAT reports, masks, residual/registration evidence and COPE/VARCOPE/ZSTAT images. Spatial receipts record classic directional/combined FWHM and ACF before/after. Require combined after-FWHM in 5.4–6.6 mm; anisotropy and ACF remain visible diagnostics.

Record your actual pilot review only after inspecting those outputs:

```bash
"$PY" code/harmonized_l1.py accept-pilot --plan "$PLAN" \
  --reviewer "$USER" --notes 'REPLACE WITH ACTUAL PILOT INSPECTION FINDINGS'
```

Replace the placeholder notes with your findings. Cohort operations require this plan-specific review record and unchanged successful pilot products.

## 6. Run the technically ready cohort and verify

```bash
"$PY" code/harmonized_l1.py spatial --plan "$PLAN" --stage cohort --jobs 4 2>&1 | tee logs/spatial-cohort.log
"$PY" code/harmonized_l1.py render --plan "$PLAN" --stage cohort --jobs 2 2>&1 | tee logs/design-cohort.log
"$PY" code/harmonized_l1.py run --plan "$PLAN" --stage cohort --jobs 4 2>&1 | tee logs/l1-cohort.log
"$PY" code/harmonized_l1.py check --plan "$PLAN" --stage cohort 2>&1 | tee logs/check-cohort.log
cat "$PLAN/check_cohort_validation.json"
column -ts $'\t' "$PLAN/check_cohort_summary.tsv"
```

Start at these worker counts and inspect memory, disk and I/O before increasing. All subprocesses use one thread by default. Resume with the same commands and PLAN; verified successes are skipped. Use `tmux`/`screen` or your established batch runner for long jobs. Locks prevent concurrent plans writing the variant. Do not delete locks or use legacy `--overwrite` scripts. Failed partial outputs must be inspected and explicitly moved to an archive directory before retrying their stage; keep their logs and receipts. For a persistent target failure, stop for methodological review and retain the run's status in reports.

Completion requires all four COPEs, VARCOPEs and ZSTATs, mask, design files, positive DOF, finite maps, matching RF1 grid and unchanged lineage. Summaries report selected technically ready runs; `run_review.tsv` retains every original omitted/held unit. An all-selected-verified result is structural completion, not visual-QC completion or final scientific inclusion.

## 7. Separate high-motion diagnostic

```bash
"$PY" code/harmonized_l1.py motion-select --plan "$PLAN" \
  --count 8 --output "$PWD/work/high-motion-mapping.tsv"
column -ts $'\t' work/high-motion-mapping.tsv | less -S
```

Fill `hmc_transform`, `hmc_reference`, `reference_evidence` and optionally `original_mcflirt_par` in the mapping TSV. Use the original `from-orig_to-boldref...desc-hmc_xfm.txt` transform series and the exact reference supplied to the HMC reconstruction. The fMRIPrep work graph/node inputs or retained HMC derivatives should establish this match. A final SDC/coregistration boldref is not an acceptable substitute. Leave unavailable paths empty; they are reported, not reconstructed from guesses.

Look up the existing FD threshold(s) in your current policy, then run with their actual values. No default is invented:

```bash
cat /ZPOOL/data/projects/rf1-sra-linux2/qc/qc_policy.json
read -r -p 'Existing FD threshold(s) in mm, separated by spaces: ' FD_VALUES
read -r -a FD_THRESHOLDS <<< "$FD_VALUES"
"$PY" code/harmonized_l1.py motion-compare \
  --mapping "$PWD/work/high-motion-mapping.tsv" \
  --output "$PWD/work/high-motion-diagnostic-v1" \
  --fd-thresholds "${FD_THRESHOLDS[@]}"
cat work/high-motion-diagnostic-v1/report.md
column -ts $'\t' work/high-motion-diagnostic-v1/comparisons.tsv | less -S
```

The report gives per-axis translation/rotation differences, existing versus corrected FD, threshold crossing counts, and nuisance-subspace differences. Review whether those differences alter QC classification or plausibly matter for nuisance regression. This is diagnostic evidence; it does not authorize replacement of production parameters or a cohort-wide rerun.

## 8. Dry-run export; no imaging publication

```bash
"$PY" code/harmonized_l1.py export --plan "$PLAN" \
  --destination "$PWD/derivatives/release-candidates/all-maps-v1" \
  > work/export-preflight.json
```

This writes only the redirected preflight text, not the candidate directory. After inspecting selection, you may assemble a local ignored candidate (still no publication):

```bash
"$PY" code/harmonized_l1.py export --plan "$PLAN" \
  --destination "$PWD/derivatives/release-candidates/all-maps-v1" --execute \
  > work/export-assembly.json
```

For an eventual main-effects-only candidate, use `--contrasts 1 2 --kinds cope` and a different fresh candidate name. `--units` accepts explicit frozen run IDs for a reviewed subset. All four COPE/VARCOPE/ZSTAT sets remain in original L1 directories. The utility cannot upload or publish. Do not add imaging derivatives, private plans, run-level QC or participant-level outputs to Git.
