#!/usr/bin/env bash

# Measure classic Gaussian and ACF smoothness with one isolated AFNI workdir.
set -euo pipefail
usage() { echo "Usage: measure_smoothness.sh --input FILE --mask FILE --output-tsv FILE [--work-dir DIR]" >&2; }
input=""; mask=""; output=""; requested_work=""
while (( $# )); do case "$1" in
  --input) input="$2"; shift 2 ;; --mask) mask="$2"; shift 2 ;;
  --output-tsv) output="$2"; shift 2 ;; --work-dir) requested_work="$2"; shift 2 ;;
  -h|--help) usage; exit 0 ;; *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
esac; done
[[ -f "$input" && -f "$mask" && -n "$output" ]] || { usage; exit 2; }
for cmd in 3dFWHMx afni; do command -v "$cmd" >/dev/null || { echo "ERROR: $cmd is unavailable" >&2; exit 1; }; done
if [[ -n "$requested_work" ]]; then
  mkdir -p "$requested_work"
  work_parent="$(cd "$requested_work" && pwd)"
  work="$(mktemp -d "${work_parent}/smoothness.XXXXXX")"
else
  work="$(mktemp -d "${TMPDIR:-/tmp}/sharedreward-smoothness.XXXXXX")"
fi
trap 'rm -rf -- "$work"' EXIT
input_abs="$(cd "$(dirname "$input")" && pwd)/$(basename "$input")"
mask_abs="$(cd "$(dirname "$mask")" && pwd)/$(basename "$mask")"
raw="$work/3dFWHMx.txt"
(
  cd "$work"
  OMP_NUM_THREADS="${AFNI_OMP_NUM_THREADS:-4}" 3dFWHMx -ShowMeClassicFWHM -detrend -mask "$mask_abs" -acf NULL -input "$input_abs" > "$raw"
)
mapfile -t numeric < <(awk 'NF>=4 && $1 !~ /^#/ && $1+0==$1 {print $1,$2,$3,$4}' "$raw")
(( ${#numeric[@]} >= 2 )) || { echo "ERROR: could not parse 3dFWHMx output" >&2; cat "$raw" >&2; exit 1; }
read -r fx fy fz fcombined <<< "${numeric[${#numeric[@]}-2]}"
read -r acfa acfb acfc acffwhm <<< "${numeric[${#numeric[@]}-1]}"
for value in "$fx" "$fy" "$fz" "$fcombined" "$acfa" "$acfb" "$acfc" "$acffwhm"; do
  awk -v x="$value" 'BEGIN { exit !(x+0 > 0) }' || { echo "ERROR: non-positive smoothness result: $value" >&2; exit 1; }
done
mkdir -p "$(dirname "$output")"
if [[ ! -s "$output" ]]; then printf 'input\tmask\tclassic_fwhm_x\tclassic_fwhm_y\tclassic_fwhm_z\tclassic_fwhm_combined\tacf_a\tacf_b\tacf_c\tacf_effective_fwhm\tafni_version\n' > "$output"; fi
version="$(afni -ver 2>&1 | head -n 1 | tr '\t' ' ')"
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$input_abs" "$mask_abs" "$fx" "$fy" "$fz" "$fcombined" "$acfa" "$acfb" "$acfc" "$acffwhm" "$version" >> "$output"
printf 'Smoothness: classic combined=%s mm; ACF effective=%s mm\n' "$fcombined" "$acffwhm"
