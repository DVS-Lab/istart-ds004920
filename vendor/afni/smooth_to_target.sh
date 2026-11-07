#!/usr/bin/env bash

# Smooth a 4D input to a measured classic-FWHM target; never adds a fixed kernel.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
source "${SCRIPT_DIR}/project_config.sh"
usage() { echo "Usage: smooth_to_target.sh --input FILE --mask FILE --output FILE [--target MM] [--qc-tsv FILE] [--work-dir DIR] [--all-blurmaster] [--overwrite]" >&2; }
input=""; mask=""; output=""; target="${TARGET_FWHM_MM:-}"; qc=""; requested_work=""; overwrite=0; all_blurmaster=0
while (( $# )); do case "$1" in
  --input) input="$2"; shift 2 ;; --mask) mask="$2"; shift 2 ;; --output) output="$2"; shift 2 ;;
  --target) target="$2"; shift 2 ;; --qc-tsv) qc="$2"; shift 2 ;; --work-dir) requested_work="$2"; shift 2 ;;
  --all-blurmaster) all_blurmaster=1; shift ;; --overwrite) overwrite=1; shift ;; -h|--help) usage; exit 0 ;; *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
esac; done
[[ -f "$input" && -f "$mask" && -n "$output" ]] || { usage; exit 2; }
[[ "$target" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v x="$target" 'BEGIN{exit !(x>0)}' || { echo "ERROR: a positive --target or TARGET_FWHM_MM is required" >&2; exit 2; }
label="$(printf '%s' "$target" | sed 's/\.0*$//; s/\./p/g')"
[[ "$(basename "$output")" == *"smoothToFWHM${label}"* ]] || { echo "ERROR: output name must encode smoothToFWHM${label}" >&2; exit 2; }
[[ "$output" != "$input" ]] || { echo "ERROR: input will not be overwritten" >&2; exit 2; }
if [[ -e "$output" && "$overwrite" -ne 1 ]]; then echo "ERROR: output exists; use --overwrite: $output" >&2; exit 1; fi
for cmd in 3dBlurToFWHM 3dFWHMx; do command -v "$cmd" >/dev/null || { echo "ERROR: $cmd is unavailable" >&2; exit 1; }; done
if [[ -n "$requested_work" ]]; then
  mkdir -p "$requested_work"
  work_parent="$(cd "$requested_work" && pwd)"
  work="$(mktemp -d "${work_parent}/blur.XXXXXX")"
else
  work="$(mktemp -d "${TMPDIR:-/tmp}/sharedreward-blur.XXXXXX")"
fi
trap 'rm -rf -- "$work"' EXIT
input_abs="$(cd "$(dirname "$input")" && pwd)/$(basename "$input")"; mask_abs="$(cd "$(dirname "$mask")" && pwd)/$(basename "$mask")"
mkdir -p "$(dirname "$output")"; output_abs="$(cd "$(dirname "$output")" && pwd)/$(basename "$output")"; tmp_out="$work/smoothed.nii.gz"
start="$(date +%s)"
blur_options=(-quiet -FWHM "$target" -mask "$mask_abs" -input "$input_abs" -prefix "$tmp_out")
(( all_blurmaster == 1 )) && blur_options+=(-bmall)
(
  cd "$work"
  OMP_NUM_THREADS="${AFNI_OMP_NUM_THREADS:-4}" 3dBlurToFWHM "${blur_options[@]}"
)
[[ -s "$tmp_out" ]] || { echo "ERROR: AFNI did not create output" >&2; exit 1; }
mv -f -- "$tmp_out" "$output_abs"; runtime=$(( $(date +%s) - start ))
[[ -n "$qc" ]] || qc="${output_abs%.nii.gz}_smoothness.tsv"
mkdir -p "$(dirname "$qc")"
tmp_qc="$work/achieved-smoothness.tsv"
bash "${SCRIPT_DIR}/measure_smoothness.sh" --input "$output_abs" --mask "$mask_abs" --output-tsv "$tmp_qc" --work-dir "$work"
mv -f -- "$tmp_qc" "$qc"
printf 'Requested target: %s mm\nAll blurmaster sub-bricks: %s\nRuntime: %s seconds\nOutput: %s\nQC: %s\n' "$target" "$all_blurmaster" "$runtime" "$output_abs" "$qc"
