# Source attribution

AFNI wrappers are copied from DVS-Lab/rf1-sra-sharedreward; the identity-grid resampling wrapper and its checker are copied from DVS-Lab/sharedreward-aging. The task templates derive from DVS-Lab/rf1-sra-socdoors model 1. Exact commits, paths and Git blob identities are in config/sources.lock.json. The MIT notice is retained in LICENSE.

The corrected motion-reconstruction equations in code/harmonized/motion.py are adapted from NiPreps fMRIPrep FSLMotionParams at commit 3e5b08b163fd9cf25bb62df9fc1b427bdec6c15b (pull request 3697), under Apache License 2.0. Copyright the fMRIPrep developers and contributors. See licenses/Apache-2.0.txt. The adaptation separates the numerical reconstruction from the Nipype interface and adds diagnostic validation; it does not modify the production container.

OpenNeuro ds004920 1.1.1 is CC0 according to its dataset_description.json. Dataset acknowledgments and references remain in the downloaded snapshot. No participant imaging data are included in this code repository.
