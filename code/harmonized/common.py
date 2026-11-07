from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import nibabel as nib
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
VARIANT = "fsl-harmonized-fwhm6-baseonly-v1"
TASKS = ("doors", "socialdoors")
MOTION = ["trans_x", "trans_y", "trans_z", "rot_x", "rot_y", "rot_z"]
BASE = [f"a_comp_cor_{i:02}" for i in range(6)] + MOTION + ["framewise_displacement"]
CONTRASTS = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [1, -1, 0, 0]])
THREADS = {k: "1" for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "AFNI_OMP_NUM_THREADS", "FSLSUB_PARALLEL")}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for part in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def stamp(path, full=None):
    p = Path(path).absolute()
    if not p.exists():
        return {"path": str(p), "absent": True}
    s = p.stat()
    r = {"path": str(p), "size": s.st_size, "mtime_ns": s.st_mtime_ns}
    if full if full is not None else not p.name.endswith((".nii", ".nii.gz", ".simg", ".sif")):
        r["sha256"] = sha(p)
    return r


def verify(stamps):
    for s in stamps:
        if stamp(s["path"], full="sha256" in s) != s:
            raise ValueError(f"Frozen input changed: {s['path']}")


def write_json(path, value, exclusive=False):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if exclusive:
        with p.open("x") as f:
            f.write(text)
    else:
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_text(text)
        tmp.replace(p)


def table(path, rows, fields=None):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or list(dict.fromkeys(k for row in rows for k in row))
    if not fields:
        fields = ["status"]
    stream = io.StringIO()
    w = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(stream.getvalue())
    tmp.replace(p)


def read_table(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fields = reader.fieldnames or []
        if len(set(fields)) != len(fields):
            raise ValueError(f"Duplicate column names: {path}")
        rows = list(reader)
        if any(None in r for r in rows):
            raise ValueError(f"Malformed TSV: {path}")
        return fields, rows


def command(argv, log=None, env=None, cwd=None):
    argv = [str(a) for a in argv]
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with Path(log).open("a") as f:
            f.write(json.dumps({"time": now(), "argv": argv, "cwd": str(cwd or Path.cwd())}) + "\n")
            f.flush()
            subprocess.run(argv, check=True, stdout=f, stderr=subprocess.STDOUT,
                           cwd=cwd, env=dict(os.environ, **THREADS, **(env or {})), close_fds=False)
    else:
        return subprocess.check_output(argv, text=True, cwd=cwd, stderr=subprocess.STDOUT,
                                       env=dict(os.environ, **THREADS, **(env or {})), close_fds=False).strip()


@contextlib.contextmanager
def lock(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"Another writer owns {p}; wait, do not delete the lock")
        # Child processes retain this lock if the parent is interrupted.
        os.set_inheritable(handle.fileno(), True)
        old = os.environ.get("HARMONIZED_LOCK_FD")
        os.environ["HARMONIZED_LOCK_FD"] = str(handle.fileno())
        try:
            yield
        finally:
            if old is None:
                os.environ.pop("HARMONIZED_LOCK_FD", None)
            else:
                os.environ["HARMONIZED_LOCK_FD"] = old


def tools_snapshot(names):
    result = {"python": sys.version, "packages": {}}
    from importlib.metadata import version
    for name in ("numpy", "pandas", "nibabel", "scipy"):
        result["packages"][name] = version(name)
    for name in names:
        exe = shutil.which(name)
        if not exe:
            raise ValueError(f"Required executable unavailable: {name}")
        result[name] = stamp(exe, full=True)
    if "afni" in names:
        result["afni_version"] = command(["afni", "-ver"])
    if os.environ.get("FSLDIR"):
        result["fsl_version"] = stamp(Path(os.environ["FSLDIR"]) / "etc/fslversion")
    return result


def code_stamps():
    paths = [p for folder in ("code", "vendor", "templates", "config")
             for p in (ROOT / folder).rglob("*") if p.is_file()
             and "__pycache__" not in p.parts and p.suffix in (".py", ".sh", ".fsf", ".json")]
    return [stamp(p) for p in sorted(paths)]


def config(path):
    c = json.loads(Path(path).read_text())
    for name in ("reference", "container", "istart_root", "rf1_root", "rf1_analysis", "source_exclusions", "evaluation"):
        if not Path(c[name]).is_absolute():
            raise ValueError(f"Configuration path must be absolute: {name}")
    if not c["rf1_sessions"] or any(not re.fullmatch(r"[0-9]{2}", s) for s in c["rf1_sessions"]):
        raise ValueError("RF1 sessions must be two-digit labels")
    return c


def grid(image, reference):
    a, b = nib.load(image), nib.load(reference)
    checks = {"dimensions": a.shape[:3] == b.shape[:3],
              "voxel_sizes": np.allclose(a.header.get_zooms()[:3], b.header.get_zooms()[:3], rtol=0, atol=1e-5),
              "orientation": nib.aff2axcodes(a.affine) == nib.aff2axcodes(b.affine),
              "affine": np.allclose(a.affine, b.affine, rtol=0, atol=1e-5)}
    if not all(checks.values()):
        raise ValueError(f"Grid mismatch: {image}: {checks}")
    return {"checks": checks, "shape": list(a.shape[:3]), "zooms": list(map(float, a.header.get_zooms()[:3])),
            "orientation": list(nib.aff2axcodes(a.affine)), "affine": a.affine.tolist()}


def image_info(path, tr=None, mask=False):
    img = nib.load(path, keep_file_open=True)
    if mask:
        x = np.asanyarray(img.dataobj)
        if x.ndim != 3 or not np.isfinite(x).all() or not np.isin(x, [0, 1]).all() or not np.any(x):
            raise ValueError(f"Invalid binary brain mask: {path}")
    else:
        if len(img.shape) != 4 or img.shape[3] < 2:
            raise ValueError(f"Invalid 4D BOLD: {path}")
        units = img.header.get_xyzt_units()[1]
        scale = {"sec": 1, "msec": .001, "usec": .000001}.get(units)
        if scale is None:
            raise ValueError(f"BOLD header needs explicit time units: {path}")
        actual_tr = float(img.header.get_zooms()[3]) * scale
        if tr is not None and not np.isclose(actual_tr, tr, rtol=0, atol=1e-4):
            raise ValueError(f"Unexpected TR {actual_tr}: {path}")
        for i in range(img.shape[3]):
            if not np.isfinite(np.asanyarray(img.dataobj[..., i])).all():
                raise ValueError(f"Nonfinite BOLD volume {i}: {path}")
    return img


def key(row):
    return tuple(str(row.get(k, "")).removeprefix(prefix) for k, prefix in
                 (("subject", "sub-"), ("session", "ses-"), ("task", "task-"), ("run", "run-")))


def entities(path):
    return dict(item.split("-", 1) for item in Path(path).name.split("_") if "-" in item)


def unique(paths, label):
    paths = list(paths)
    if len(paths) != 1 or not paths[0].is_file():
        raise ValueError(f"Expected one {label}, found {len(paths)}: {paths}")
    return paths[0]
