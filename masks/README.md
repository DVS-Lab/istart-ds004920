# Chloe's PPI seed masks

The six `seed-*.nii.gz` files are binary seeds resliced onto the established RF1 MNI152NLin6Asym reference grid: **57 × 70 × 54**, **2.7 × 2.7 × 2.97 mm**, RAS orientation, exact reference affine. Use these filenames when the harmonized BOLD images are available:

| Seed | File | Original voxels (3 mm) | RF1-grid voxels |
| --- | --- | ---: | ---: |
| Left hippocampus | `seed-hippo_left.nii.gz` | 59 | 71 |
| Right hippocampus | `seed-hippo_right.nii.gz` | 44 | 54 |
| IPL (left) | `seed-IPL.nii.gz` | 52 | 62 |
| Fusiform (left) | `seed-Fusiform.nii.gz` | 56 | 56 |
| Left mPFC | `seed-mPFC_left.nii.gz` | 198 | 244 |
| Right mPFC | `seed-mPFC_right.nii.gz` | 198 | 199 |

`originals/PPI.zip` preserves the user-supplied archive byte-for-byte, including four NIfTI masks and the original two AFNI HEAD/BRIK pairs. The original masks share a 65 × 77 × 65, 3-mm RAS grid. NIfTI headers mark MNI coordinates; the AFNI headers explicitly report `TEMPLATE_SPACE = MNI`. The exact original nonlinear MNI template variant is not specified, so these are grid-only reslices assuming corresponding MNI world coordinates, not a new registration.

The mPFC datasets were converted from singleton AFNI volumes to 3D NIfTI as part of reslicing. All six seeds use nearest-neighbor interpolation, identity world coordinates and zero background. There is no smoothing, dilation, erosion, hemisphere clipping or ROI redefinition. The provided left/right labels were preserved and centroid signs checked in RAS coordinates.

Nearest-neighbor sampling changes the physical volumes of small ROIs: fusiform and right mPFC are approximately 20% smaller, while the other masks change by approximately 1–4%. The resliced left mPFC includes a boundary column with voxel centers at x = +0.8 mm, sampled from the original x = −0.5 mm column; its centroid remains left-sided. This sampling edge is retained, rather than silently clipping or altering the supplied seed. Inspect overlays and BOLD-mask coverage before using the seeds for PPI. No PPI analysis or new scientific inclusion decision is introduced by adding these assets.

`seed_manifest.json` records source/output SHA256 checksums, voxel counts, physical volumes, centroids, bounds, exact reference provenance and software versions. The small image in `reference/` is the established signal-free RF1 reference image, copied from `DVS-Lab/rf1-sra-sharedreward` at commit `d5331d38c298023eeaf094e6e83ee0df65434ac2`.

Check the committed masks on Linux2 (only NumPy and NiBabel are needed):

```bash
cd /ZPOOL/data/projects/istart-ds004920
.venv-preproc/bin/python code/prepare_seed_masks.py
```

To reproduce reslicing in a separate copy with no existing derived files, run `code/prepare_seed_masks.py --reslice` in the full scientific Python environment, which also provides SciPy. Existing recorded outputs are verified and left untouched.
