# Project working conventions

- Commit and push code, documentation, status and sanitized logs directly to `main`. Do not create branches or pull requests unless the user explicitly requests one.
- Linux2 executes the processing code; provide exact commands when execution is not available from this workspace.
- Do not put strict-shell settings in commands pasted into an interactive shell. Handle failures explicitly in child scripts.
- Launch the fMRIPrep cohort after the successful pilot structural checks. Do not require a visual-review prompt or approval file to begin preprocessing; visual QC remains a separate record.
- The current user-selected cohort budget is ten subjects with four processes and two OpenMP threads per subject, with no explicit memory budget (`--mem-mb 0`).
- Preserve existing imaging derivatives, cached work, scientific inclusion decisions and the pinned fMRIPrep 25.2.5 configuration.
