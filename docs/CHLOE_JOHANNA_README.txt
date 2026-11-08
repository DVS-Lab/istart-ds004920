START HERE — Chloe and Johanna's Doors / Social Doors package
============================================================

This folder contains the first-level (individual task run) results from
two studies, iSTART and RF1. The results are already made. You only need
to copy the folder, check the copy, and open the files you need.

1. What to copy
---------------
On David's lab server (Linux2), the full folder is:

  /ZPOOL/data/projects/istart-ds004920/derivatives/release-candidates/chloe-harmonized-l1-v1

Copy that whole folder. Keep the two study folders and the files at the
top together. After the copy, you should see:

  chloe-harmonized-l1-v1/
    README.txt          This guide
    READY.json          Record of the completed export on Linux2
    VERIFY_PACKAGE.py   Checks that the files arrived intact
    istart/             iSTART results: 106 task runs, 5,936 maps
    rf1/                RF1 results: 735 task runs, 41,160 maps

The 841 counts are task runs, not participants. The 47,096 maps include
23,548 effect estimates and 23,548 corresponding Z maps. There is one
brain mask per run. GitHub tracks the instructions, analysis code, small
reference assets and processing status. The result images and run-level
quality tables are in the folder on Linux2. Cloning GitHub does not get
the package.

2. Copy directly between the Linux computers
---------------------------------------------
Pick ONE option below. Both copy the same folder over SSH (the connection
used to log in to a Linux server). No cloud upload is needed. Use rsync
when available: if the copy stops, run the same rsync command again to
continue. scp is an alternative; it can recopy files if interrupted.

Before starting, ask David for the folder size (he can run `du -sh` on
the source folder). Make sure the receiving folder has that much free
space, with some room to spare. `df -h /path/to/receiving/folder` shows
available space. Use an existing shared project folder if both of you
need access. The person running the copy needs permission to read the
source and write to the destination.

Option A — Chloe or Johanna pulls from Linux2

Open a terminal on the Linux computer that will RECEIVE the files. Ask
David for the Linux2 SSH hostname and confirm which account you can use
there. In the first line below, replace YOUR_LINUX2_USER and LINUX2_HOST
with those actual values. The example saves to a folder called `data` in
your home folder. Change receiving_parent to the full path of your
shared project folder if you want the files there instead. Keep the quotes.

  linux2_login='YOUR_LINUX2_USER@LINUX2_HOST'
  receiving_parent="$HOME/data"
  source_folder='/ZPOOL/data/projects/istart-ds004920/derivatives/release-candidates/chloe-harmonized-l1-v1'

  mkdir -p "$receiving_parent" &&
  rsync -rt --partial --info=progress2 \
    "$linux2_login:$source_folder" "$receiving_parent/"

If using scp instead, set the same three values, then run:

  mkdir -p "$receiving_parent" &&
  scp -r "$linux2_login:$source_folder" "$receiving_parent/"

Option B — David pushes from Linux2

Send David the destination computer's SSH hostname, the account he is
authorized to use, and the full path to an existing writable parent
folder. Confirm the recipients can read the files there. David runs this
in a terminal ON LINUX2, after replacing the first two example values:

  receiving_login='AUTHORIZED_USER@RECEIVING_HOST'
  receiving_parent='/absolute/path/to/existing/receiving/folder'
  source_folder='/ZPOOL/data/projects/istart-ds004920/derivatives/release-candidates/chloe-harmonized-l1-v1'

  rsync -rt --partial --info=progress2 \
    "$source_folder" "$receiving_login:$receiving_parent/"

If using scp instead, set the same three values, then run:

  scp -r "$source_folder" "$receiving_login:$receiving_parent/"

Use the source path as shown, without a slash at the end, so the copy
creates one chloe-harmonized-l1-v1 folder inside the receiving parent.
If SSH cannot connect or reports permission denied, ask the server owner
to check network access and your account. You can use the other option
if it is the only direction in which SSH works.

3. Check that the copy arrived intact
------------------------------------
On the receiving Linux computer, open a terminal. If you used the
example destination in Option A, copy and paste this command:

  cd "$HOME/data/chloe-harmonized-l1-v1" && python3 VERIFY_PACKAGE.py

For another destination, replace $HOME/data with the parent folder you
used. This reads the copied files, so it can take a few minutes. It checks
the file lists and sizes, the SHA256 checksums (digital fingerprints),
the map and mask counts, and the MRIQC summaries. It requires Python 3
and does not install anything or change the package.

Wait for `PASS`. Expect 106/106 iSTART runs and 735/735 RF1 runs to have
MRIQC summaries. If it prints `FAIL`, note the message. To repair the
copy, repeat the rsync command with `--checksum` after `-rt` so rsync
compares file contents. Then run the checker again. If it still fails,
send David the error and the path to your copy. READY.json records the
source export; run the check on the receiving computer after copying.

4. Find the right map or quality summary
----------------------------------------
Start in istart/ or rf1/. Both folders use the same layout.

  maps.tsv              Start here to find a map. It lists each file's
                        participant, session, task, run, model, seed,
                        contrast, and statistic. Open the TSV in a
                        spreadsheet as tab-separated text. Keep ID and
                        session columns as text to preserve leading zeros.
  run_quality.tsv       One row per exported task run. Use this table to
                        compare quality for the runs with maps.
  mriqc_metrics.tsv     The MRIQC summary table. It can also include raw
                        task runs that did not produce a map.
  run_quality.json      Definitions and units for the quality columns.
  run_availability.tsv  Explains which inputs were missing, held, or
                        otherwise unavailable for analysis.
  participants.tsv      IDs with exported results in that study.
  checksums.tsv         The file fingerprints used by the copy checker.
  README                More technical details for the study folder.

Read map names from left to right. `sub-` is the participant, `ses-` is
the session (RF1 sessions stay separate), and `task-` is Doors or Social
Doors. `model-activation` measures the task response. A `model-ppi...`
map measures how a seed region's connection with the rest of the brain
changes with the task. Left and right mPFC are the primary seeds. Left
and right hippocampus, IPL, and fusiform are exploratory seeds.

Each model has Win, Loss, Decision, and Win-minus-Loss maps. For example,
to find the iSTART participant sub-1002 Doors activation effect for Win
minus Loss, filter maps.tsv for `sub-1002`, `doors`, `activation`,
`winMinusLoss`, and `cope`. The `file` column gives the path within istart/.

  `stat-effect` / `cope`   Estimated size and direction of the effect.
  `stat-z` / `zstat`       Matching statistical evidence for that effect.

The Z maps are unthresholded. All maps share the same standard-space
grid (MNI152NLin6Asym). You can open the .nii.gz images as they are in a
NIfTI viewer such as FSLeyes. These are run-level results; the package
does not contain a combined group analysis. Ask David if you need
variance maps (VARCOPEs), the additional physiological PPI contrast,
full FEAT outputs, or original images. Those remain on Linux2.

5. Read the quality summaries
-----------------------------
For a run with a map, find the same participant, session, task, and run
in run_quality.tsv. The `mriqc_` columns come from MRIQC:

  mriqc_fd_mean  Mean framewise displacement, a measure of head movement
                 between consecutive images, in millimeters.
  mriqc_tsnr     Temporal signal-to-noise ratio, a measure of signal
                 stability across time.

The table also records the MRIQC version, source checksum, and echo used
where applicable. RF1 uses the echo selected in the original quality
table. The `fd_mean` and `tsnr` columns without the `mriqc_` prefix come
from the preprocessing workflow. The two sets use different inputs or
methods, so keep them separate when comparing runs. The package includes
these tabular MRIQC summaries; full MRIQC HTML reports remain on Linux2.

`classic_fwhm_mm` records the measured image smoothness. Two reviewed RF1
sub-10657 session 01 runs are flagged in `smoothness_reviewed_exception`:
Doors (5.3761 mm) and Social Doors (5.3654 mm). The flag records their
review. The quality tables describe the data and do not assign new
exclusions or a visual quality rating.
