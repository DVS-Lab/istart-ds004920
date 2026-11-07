#!/usr/bin/env python3
"""Resume sub-1002 after adding the existing RF1 production license argument."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def archive_failed_metadata(root):
    batch = root / "work/preprocessing-runs/pilot"
    if not batch.exists():
        return None
    # Do not rename a lock while a live worker or container still owns it.
    with ExitStack() as stack:
        for path in (batch / "worker.lock", batch / "launch.lock", root / "work/preprocessing/sub-1002.lock"):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context(path.open("a"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Pilot or subject process is still active; nothing archived or restarted")
        state_path = batch / "state.json"
        if state_path.exists():
            pid = json.loads(state_path.read_text()).get("pid")
            if pid:
                try:
                    os.kill(int(pid), 0)
                except ProcessLookupError:
                    pass
                else:
                    raise RuntimeError(f"Recorded pilot PID {pid} is still alive; nothing archived or restarted")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = root / "work/preprocessing-runs/archive" / ("pilot-before-license-fix-" + stamp)
        archive.mkdir(parents=True, exist_ok=False)
        receipt = root / "work/preprocessing/sub-1002.json"
        if receipt.is_file():
            shutil.copy2(receipt, archive / "sub-1002-before-license-fix.json")
        batch.rename(archive / "batch")
        print(f"Preserved failed pilot records: {archive}", flush=True)
        return archive


def main():
    entry = ROOT / "code/preprocess_istart.py"
    resources = ["--nprocs", "16", "--omp", "2", "--mem-mb", "24000"]
    # Validates the existing license and exact container version before changing records.
    subprocess.run([sys.executable, str(entry), "validate", "--subjects", "1002", *resources], check=True, cwd=ROOT)
    archive_failed_metadata(ROOT)
    # Same BIDS, derivatives and per-subject fMRIPrep work cache; only failed batch
    # metadata is archived because its old frozen script hash cannot be resumed.
    subprocess.run([sys.executable, str(entry), "launch", "--name", "pilot", "--subjects", "1002",
                    "--jobs", "1", *resources], check=True, cwd=ROOT)
    subprocess.run([sys.executable, str(entry), "status", "--name", "pilot"], check=True, cwd=ROOT)


if __name__ == "__main__":
    main()
