#!/usr/bin/env python3
"""Check or reslice Chloe's six supplied binary MNI seeds onto the RF1 grid."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

import nibabel as nib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "hippo_left": "PPI_hippo_left.nii.gz",
    "hippo_right": "PPI_hippo_right.nii.gz",
    "IPL": "PPI_IPL.nii.gz",
    "Fusiform": "PPI_Fusiform.nii.gz",
    "mPFC_left": "PPI_mPFC_left+tlrc.HEAD",
    "mPFC_right": "PPI_mPFC_right+tlrc.HEAD",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_binary(image):
    data = np.asarray(image.dataobj)
    if data.ndim == 4 and data.shape[3] == 1:
        data = data[..., 0]
    if data.ndim != 3 or not np.isfinite(data).all() or not np.isin(data, [0, 1]).all() or not np.any(data):
        raise ValueError("Expected a nonempty finite 3D binary seed")
    return data.astype(np.uint8)


def geometry(image):
    return {"dimensions": list(image.shape[:3]), "voxel_sizes_mm": list(map(float, image.header.get_zooms()[:3])),
            "orientation": list(nib.aff2axcodes(image.affine)), "affine": image.affine.tolist()}


def describe(image):
    data = check_binary(image)
    coords = nib.affines.apply_affine(image.affine, np.argwhere(data))
    return {**geometry(image), "nonzero_voxels": int(data.sum()),
            "volume_mm3": float(data.sum() * abs(np.linalg.det(image.affine[:3, :3]))),
            "centroid_ras_mm": coords.mean(axis=0).tolist(),
            "bounds_ras_mm": [coords.min(axis=0).tolist(), coords.max(axis=0).tolist()]}


def check(folder):
    manifest = json.loads((folder / "seed_manifest.json").read_text())
    for rel, expected in manifest["source_files_sha256"].items():
        if sha(folder / rel) != expected:
            raise ValueError(f"Original/reference identity changed: {rel}")
    reference = nib.load(folder / manifest["reference"])
    for row in manifest["seeds"]:
        path = folder / row["file"]
        image = nib.load(path)
        check_binary(image)
        if image.shape != reference.shape[:3] or not np.allclose(image.affine, reference.affine, atol=1e-5, rtol=0):
            raise ValueError(f"RF1 geometry mismatch: {path}")
        if not np.allclose(image.header.get_zooms()[:3], reference.header.get_zooms()[:3], atol=1e-5, rtol=0):
            raise ValueError(f"RF1 voxel size mismatch: {path}")
        if nib.aff2axcodes(image.affine) != nib.aff2axcodes(reference.affine) or sha(path) != row["sha256"]:
            raise ValueError(f"Output identity/orientation changed: {path}")
    print(f"Verified {len(manifest['seeds'])} binary seed masks on the RF1 grid; original archive unchanged.")


def reslice(folder):
    from nibabel.processing import resample_from_to
    archive = folder / "originals/PPI.zip"
    reference = folder / "reference/rf1_MNI152NLin6Asym_reference_grid.nii.gz"
    target = nib.load(reference)
    if (folder / "seed_manifest.json").exists():
        check(folder)
        return
    rows = []
    with tempfile.TemporaryDirectory(prefix="chloe-seeds-") as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(archive) as z:
            for entry in z.infolist():
                if Path(entry.filename).is_absolute() or ".." in Path(entry.filename).parts:
                    raise ValueError("Unsafe archive member")
            z.extractall(tmp)
        for name, source_name in SOURCES.items():
            source_path = tmp / "PPI" / source_name
            original = nib.load(source_path)
            data = check_binary(original)
            source = nib.Nifti1Image(data, original.affine)
            # Identity in world coordinates; labels use nearest-neighbor only.
            resampled = resample_from_to(source, (target.shape[:3], target.affine), order=0, mode="constant", cval=0)
            out = nib.Nifti1Image(check_binary(resampled), target.affine)
            out.set_qform(target.get_qform(), int(target.header["qform_code"]))
            out.set_sform(target.get_sform(), int(target.header["sform_code"]))
            out.header.set_xyzt_units("mm")
            out.header["descrip"] = b"Chloe seed; NN identity reslice to RF1 MNI152NLin6Asym grid"
            path = folder / f"seed-{name}.nii.gz"
            if path.exists():
                raise ValueError(f"Existing output without receipt is preserved: {path}")
            nib.save(out, path)
            reopened = nib.load(path)
            a, b = describe(original), describe(reopened)
            # Supplied hemisphere labels are checked against RAS world coordinates.
            if name.endswith("left") and b["centroid_ras_mm"][0] >= 0:
                raise ValueError(f"Left seed centroid is in the wrong hemisphere: {name}")
            if name.endswith("right") and b["centroid_ras_mm"][0] <= 0:
                raise ValueError(f"Right seed centroid is in the wrong hemisphere: {name}")
            rows.append({"seed": name, "source_member": "PPI/" + source_name,
                         "source_sha256": sha(source_path), "file": path.name, "sha256": sha(path),
                         "original": a, "resliced": b})
    from importlib.metadata import version
    manifest = {"source": "Chloe-supplied PPI.zip, provided by the user", "seeds": rows,
                "reference": str(reference.relative_to(folder)),
                "reference_repository": "DVS-Lab/rf1-sra-sharedreward",
                "reference_commit": "d5331d38c298023eeaf094e6e83ee0df65434ac2",
                "reference_blob": "233c636fabb91f4b85f62a0e78f752768cc4d643",
                "source_files_sha256": {str(p.relative_to(folder)): sha(p) for p in (archive, reference)},
                "method": "nibabel.processing.resample_from_to, nearest-neighbor order=0, world-coordinate identity, background=0",
                "registration_applied": False, "assumed_coordinate_space": "MNI (source headers); exact original nonlinear template variant not specified",
                "versions": {name: version(name) for name in ("numpy", "nibabel", "scipy")}}
    (folder / "seed_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    check(folder)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--masks", type=Path, default=ROOT / "masks")
    p.add_argument("--reslice", action="store_true", help="Create missing derived seeds; default verifies the committed results")
    a = p.parse_args()
    (reslice if a.reslice else check)(a.masks)


if __name__ == "__main__":
    main()
