# Harmonized iSTART and RF1 Doors/Social Doors L1

**Chloe and Johanna: [start here](docs/CHLOE_JOHANNA_README.txt).** The guide explains how to copy the completed folder directly between Linux computers, verify the copy, find the maps, and read the MRIQC quality summaries.

The final package is staged on Linux2 at:

```text
/ZPOOL/data/projects/istart-ds004920/derivatives/release-candidates/chloe-harmonized-l1-v1
```

The [completed export record](status/linux2/status.json) reports **47,096 effect and Z maps** and MRIQC summaries for **all 841 exported task runs**. The package also includes one brain mask per run. This Git repository tracks code, instructions, templates, reviewed seed/reference files, and processing status. The study images and run-level tables are in the staged Linux2 folder. The maintainer [handoff notes](docs/CHLOE_HANDOFF.txt) show how to add the recipient guide and verifier to the existing folder before copying it.

## Processing and reproducibility notes

The completed preprocessing and first-level workflow is documented in [PROCESSING_HANDOFF.txt](docs/PROCESSING_HANDOFF.txt), [TONIGHT.md](docs/TONIGHT.md), and [L1_NEXT.txt](docs/L1_NEXT.txt). These are processing instructions for maintainers. Recipients only need the staged package and the guide above.

This repository implements a separate first-level activation and seed-PPI variant for iSTART (OpenNeuro ds004920 1.1.1) and RF1. See [docs/LINUX2_COMMANDS.md](docs/LINUX2_COMMANDS.md) for the complete execution sequence. Local tests use synthetic images and simulated AFNI/FSL commands; real preprocessing, FEAT design validation, pilot inspection and cohort results must be produced on Linux2.

The OpenNeuro snapshot is pinned to commit `2913a29de224f5364629cc39ac26045c06817b4f`. iSTART preprocessing uses the existing `/ZPOOL/data/tools/fmriprep-25.2.5.simg`, single echo, existing GRE fieldmaps and no FreeSurfer reconstruction or CIFTI. RF1 preprocessing is reused. Original source and preprocessing images remain intact.

iSTART BOLD is placed on the established RF1 MNI152NLin6Asym grid using identity wsinc5 interpolation and nearest-neighbor masks. Both datasets use the same vendored AFNI `3dBlurToFWHM` wrapper with its default blurmaster behavior, measuring classic directional/combined FWHM and ACF before and after. The target is 6 mm total measured classic combined FWHM, with the existing 5.4–6.6 mm acceptance band. Baseline images with a combined or directional classic FWHM at/above 6 mm require review. There is no automatic `-bmall` fallback. Existing Linux2 smoothing evaluation images are not imported; a separate report extracts its five documented exceptions for review.

Task model 1 preserves Win, Loss, Decision and optional missed-decision EVs and four contrasts in order: Win, Loss, Decision, Win minus Loss. Double-gamma convolution, filtering and prewhitening follow the source templates. FEAT smoothing is zero. RF1 uses TR 1.615 s; iSTART uses TR 1.75 s. Base nuisance matrices contain six aCompCor components, six motion parameters, FD, available cosines and non-steady-state columns in production order; missing values become zero, malformed/nonfinite values fail. CompCor and anatomical segmentation are independently estimated across datasets, and iSTART omits FreeSurfer.

RF1 products live under `rf1-sra-socdoors/derivatives/fsl-harmonized-fwhm6-baseonly-v1/`; iSTART products use the same variant directory in the Linux2 project checkout. Spatial products use `derivatives/harmonized-fwhm6-baseonly-v1/`. Existing TEDANA confounds and FEAT results are never rewritten. `code/install_rf1_variant.py` installs a code-only copy into a new RF1 downstream subtree; its default is a dry run.

The workflow freezes input identity, policy files, scripts, templates, package versions and executable hashes. Original large BOLD inputs use path/size/mtime stamps; derived spatial products and all retained maps use full SHA256. Four activation pilot runs and the corresponding seed-PPI models must pass design and output checks before cohort execution. Optional visual-review notes remain separate; there is no interactive approval prompt. Output presence alone never implies success. Partial, unowned or changed products fail without overwriting. A technically ready manifest is not a scientific inclusion decision. Source exclusions, task holds and explicit dispositions are preserved; QC flags are reported without becoming new automatic exclusions.

COPE, VARCOPE and ZSTAT images for all four contrasts are retained. The exporter defaults to a read-only dry run and can assemble a checksummed local release candidate in an ignored derivatives directory. It never publishes imaging derivatives. L2/L3 are out of scope.

The separate high-motion diagnostic selects 5–10 RF1 runs by translation range, rotation range and FD. It requires original motion transforms and the exact HMC reference image; it does not substitute a final SDC/coregistration reference. Missing original inputs are reported. No production nuisance matrix or QC policy is changed.
