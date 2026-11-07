#!/usr/bin/env bash

# Resample one ds003745 BOLD or mask onto the authoritative RF1 grid.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=project_config.sh
source "$SCRIPT_DIR/project_config.sh"

usage() {
    echo "Usage: resample_to_rf1_grid.sh --input FILE --kind bold|mask --output FILE [--overwrite]" >&2
}

input=""
kind=""
output=""
overwrite=0
while (( $# )); do
    case "$1" in
        --input) input="$2"; shift 2 ;;
        --kind) kind="$2"; shift 2 ;;
        --output) output="$2"; shift 2 ;;
        --overwrite) overwrite=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

[[ -f "$input" && -f "$REFERENCE_GRID" && -n "$output" ]] || {
    usage
    exit 2
}
case "$kind" in
    bold) resampler=3dAllineate ;;
    mask) resampler=3dresample ;;
    *) echo "ERROR: --kind must be bold or mask" >&2; exit 2 ;;
esac
[[ "$input" != "$output" ]] || {
    echo "ERROR: input will not be overwritten" >&2
    exit 2
}
[[ ! -e "$output" || $overwrite -eq 1 ]] || {
    echo "ERROR: output exists: $output" >&2
    exit 1
}
command -v "$resampler" >/dev/null || {
    echo "ERROR: $resampler unavailable" >&2
    exit 1
}

mkdir -p "$(dirname "$output")"
work="$(mktemp -d "$(dirname "$output")/.resample.XXXXXX")"
tmp="$work/output.nii.gz"
tmp_json="$work/output_grid.json"
output_json="${output%.nii.gz}_grid.json"
trap 'rm -rf -- "$work"' EXIT

if [[ "$kind" == bold ]]; then
    # The data are already in MNI152NLin6Asym space. Apply an identity
    # transform only to change grids, using AFNI's sharp final interpolant.
    AFNI_WSINC5_SILENT=YES 3dAllineate \
        -input "$input" \
        -master "$REFERENCE_GRID" \
        -1Dmatrix_apply IDENTITY \
        -final wsinc5 \
        -prefix "$tmp"
else
    3dresample \
        -master "$REFERENCE_GRID" \
        -rmode NN \
        -input "$input" \
        -prefix "$tmp"
fi
[[ -s "$tmp" ]] || {
    echo "ERROR: resampling produced no output" >&2
    exit 1
}

"${IMAGING_PYTHON:-python3}" "$SCRIPT_DIR/check_grid.py" \
    --reference "$REFERENCE_GRID" \
    --image "$tmp" \
    --json-output "$tmp_json"

mv -f -- "$tmp" "$output"
mv -f -- "$tmp_json" "$output_json"
echo "Wrote verified RF1-grid $kind: $output"
