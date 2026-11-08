#!/usr/bin/env python3
"""Refresh only the main status publisher, preserving active processing code."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path("/ZPOOL/data/projects/istart-ds004920")


def run(argv):
    return subprocess.run(list(map(str, argv)), check=True, text=True, capture_output=True,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0")).stdout


def process_args(pid):
    cmdline = Path(f"/proc/{pid}/cmdline")
    if not cmdline.exists():
        return None
    data = cmdline.read_bytes()
    return data.decode(errors="replace").split("\0") if data else None


def stop_publisher(state):
    pid = state.get("pid")
    if not pid:
        return
    argv = process_args(pid)
    if not argv:
        return
    if not any(Path(part).name == "publish_processing_status.py" for part in argv) or "watch" not in argv:
        raise ValueError("Recorded PID is not the status watcher; no process was stopped")
    if os.getpgid(int(pid)) != int(pid):
        raise ValueError("Status watcher has an unexpected process group; no process was stopped")
    os.killpg(int(pid), signal.SIGTERM)
    for _ in range(50):
        if not process_args(pid):
            return
        time.sleep(.1)
    raise ValueError("Old status watcher did not stop; preprocessing was not touched")


def publisher_start_args(python, publisher, state):
    argv = [python, publisher, "start", "--batch", state.get("batch", "cohort-expanded"), "--root", ROOT]
    watched = state.get("watched_state")
    if watched:
        try:
            Path(watched).relative_to(ROOT / "work/l1-runs")
        except ValueError:
            pass
        else:
            argv += ["--run-state", watched]
    return argv


def main():
    folder = ROOT / "work/github-status"
    folder.mkdir(parents=True, exist_ok=True)
    statefile = folder / "state.json"
    state = json.loads(statefile.read_text()) if statefile.exists() else {}
    # Wait for an existing push to finish, then stop only its detached watcher.
    with (folder / "publish.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        stop_publisher(state)
    content = run(["git", "-C", ROOT, "show", "FETCH_HEAD:code/publish_processing_status.py"])
    if 'BRANCH = "main"' not in content or 'PREFIX = "status/linux2/"' not in content:
        raise ValueError("Fetched publisher is not the main-only version")
    publisher = folder / "main/code/publish_processing_status.py"
    publisher.parent.mkdir(parents=True, exist_ok=True)
    publisher.write_text(content)
    python = ROOT / ".venv-preproc/bin/python"
    print(run([python, publisher, "publish", "--root", ROOT]), end="")
    print(run(publisher_start_args(python, publisher, state)), end="")
    old = run(["git", "-C", ROOT, "ls-remote", "--heads", "origin", "refs/heads/linux2-status"]).strip()
    if old:
        print(run(["git", "-C", ROOT, "push", "origin", "--delete", "linux2-status"]), end="")
    print("Status publisher refreshed on main. Active processing and its checkout were preserved.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("Status migration failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
