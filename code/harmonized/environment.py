"""Locate the existing Linux2 AFNI/FSL installations; install no software."""
import json
import os
from pathlib import Path
import shutil

from .common import write_json

AFNI = ("afni", "3dBlurToFWHM", "3dFWHMx", "3dAllineate", "3dresample")


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def configure(c):
    paths = []
    os.environ.setdefault("FSLOUTPUTTYPE", "NIFTI_GZ")
    fsl = Path(os.environ.get("FSLDIR", "/usr/local/fsl"))
    if fsl.is_dir():
        os.environ["FSLDIR"] = str(fsl)
        paths += [str(p) for p in (fsl / "bin", fsl / "share/fsl/bin") if p.is_dir()]
    candidates = []
    plans = [Path(c["rf1_root"]) / "work/cohort-smoothness-v1/plan.json", Path(c["evaluation"]) / "plan.json"]
    for path in plans:
        if path.is_file():
            for value in strings(json.loads(path.read_text())):
                if Path(value).name in AFNI:
                    candidates.append(Path(value).parent)
    for name in AFNI:
        current = shutil.which(name)
        if current:
            candidates.append(Path(current).parent)
    candidates += [Path.home() / "abin", Path("/usr/local/afni/bin"), Path("/usr/local/afni"),
                   Path("/opt/afni"), Path("/ZPOOL/data/tools/abin"), Path("/ZPOOL/data/tools/afni")]
    tools = Path("/ZPOOL/data/tools")
    if tools.is_dir():
        for pattern in ("*afni*", "*AFNI*"):
            for base in tools.glob(pattern):
                if base.is_dir():
                    candidates += [base, base / "bin", base / "abin", base / "linux_openmp_64"]
    selected = next((p for p in dict.fromkeys(candidates) if all((p / name).is_file() and os.access(p / name, os.X_OK) for name in AFNI)), None)
    if selected is not None:
        paths.append(str(selected))
    os.environ["PATH"] = os.pathsep.join([*paths, os.environ.get("PATH", "")])
    found = {name: shutil.which(name) for name in (*AFNI, "feat", "feat_model", "fslmeants")}
    result = {"executables": found, "FSLDIR": os.environ.get("FSLDIR"), "AFNI_directory": str(selected) if selected else None,
              "searched_AFNI_directories": list(map(str, dict.fromkeys(candidates))), "metadata_sources": list(map(str, plans))}
    write_json(Path(c["istart_root"]) / "work/l1-tools.json", result)
    missing = [name for name, path in found.items() if not path]
    if missing:
        raise ValueError("Existing L1 tools could not be located: " + ", ".join(missing) + "; see work/l1-tools.json")
    return result
