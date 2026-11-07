#!/usr/bin/env python3
"""Install a frozen code-only variant inside rf1-sra-socdoors; preserve existing files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rf1-repository", type=Path, default=Path("/ZPOOL/data/projects/rf1-sra-socdoors"))
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    source = Path(__file__).resolve().parents[1]
    target = a.rf1_repository.resolve() / "code/harmonized-l1-v1"
    if not (a.rf1_repository / ".git").exists():
        p.error("Target must be an existing rf1-sra-socdoors checkout")
    selected = []
    for folder in ("code", "templates", "vendor", "config", "tests"):
        selected += [f for f in (source / folder).rglob("*") if f.is_file() and "__pycache__" not in f.parts]
    selected += [source / f for f in ("README.md", "requirements.txt", "LICENSE", "NOTICE.md") if (source / f).is_file()]
    selected += [f for f in (source / "licenses").rglob("*") if f.is_file()]
    records = {str(f.relative_to(source)): digest(f) for f in sorted(selected)}
    if target.exists():
        receipt = json.loads((target / "installed.json").read_text())
        if receipt["files"] != records or any(not (target / rel).is_file() or digest(target / rel) != value for rel, value in records.items()):
            p.error("Existing installed code differs; preserved unchanged. Choose a new version after review")
        print("Existing RF1 variant code verified; unchanged")
        return
    print(json.dumps({"target": str(target), "files": len(records), "execute": a.execute}, indent=2))
    if a.execute:
        target.mkdir(parents=True, exist_ok=False)
        for f in selected:
            dest = target / f.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
        (target / "installed.json").write_text(json.dumps({"source": str(source), "files": records}, indent=2) + "\n")
        print(f"Installed code only at {target}; git add this directory to record the RF1 downstream variant")


if __name__ == "__main__":
    main()
