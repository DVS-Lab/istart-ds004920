#!/usr/bin/env python3
"""Require exact spatial-grid equivalence within an affine tolerance."""
import argparse,json
from pathlib import Path
import numpy as np
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--reference',required=True,type=Path);p.add_argument('--image',required=True,type=Path);p.add_argument('--atol',type=float,default=1e-5);p.add_argument('--json-output',type=Path);a=p.parse_args()
 try:import nibabel as nib
 except ImportError as e:p.error(f'nibabel is required: {e}')
 r=nib.load(a.reference);i=nib.load(a.image);checks={'shape':r.shape[:3]==i.shape[:3],'voxel_sizes':np.allclose(r.header.get_zooms()[:3],i.header.get_zooms()[:3],atol=a.atol),'affine':np.allclose(r.affine,i.affine,atol=a.atol),'orientation':nib.aff2axcodes(r.affine)==nib.aff2axcodes(i.affine)};out={'reference':str(a.reference.resolve()),'image':str(a.image.resolve()),'grid_match':all(checks.values()),'checks':checks}
 if a.json_output:a.json_output.parent.mkdir(parents=True,exist_ok=True);a.json_output.write_text(json.dumps(out,indent=2)+'\n')
 print(json.dumps(out));return 0 if out['grid_match'] else 1
if __name__=='__main__':raise SystemExit(main())
