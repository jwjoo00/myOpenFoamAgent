"""
geom.py — CAD-free geometry download (Phase 3). Curated, no-login, script-friendly
sources only (from the geometry research). Downloads via urllib (no shell, so no
command injection from a model-supplied URL); the caller confines the destination.

  motorbike : OpenFOAM-12 resources/geometry/motorBike.obj.gz  (raw URL, HTTP 200)
  airfoil   : UIUC/m-selig Selig-format <name>.dat              (e.g. naca0012)
  url       : a generic URL with a whitelisted geometry extension
"""
from __future__ import annotations

import gzip
import re
import shutil
import urllib.request
from pathlib import Path

MOTORBIKE_URL = ("https://raw.githubusercontent.com/OpenFOAM/OpenFOAM-12/master/"
                 "tutorials/resources/geometry/motorBike.obj.gz")
# UIUC/m-selig uses short names (NACA 0012 = n0012). airfoiltools is the fallback.
UIUC_DAT = "https://m-selig.ae.illinois.edu/ads/coord_seligFmt/{name}.dat"
AIRFOILTOOLS = "http://airfoiltools.com/airfoil/seligdatfile?airfoil={name}-il"
ALLOWED_EXT = {".stl", ".stlb", ".obj", ".dat", ".vtk", ".ftr"}


def _download(url: str, dest: Path, timeout: float = 180) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "myOpenFoamAgent/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest


def fetch(kind: str, dest_dir, name: str | None = None, url: str | None = None) -> Path:
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    if kind == "motorbike":
        gz = dest / "motorBike.obj.gz"
        _download(MOTORBIKE_URL, gz)
        obj = dest / "motorBike.obj"
        with gzip.open(gz, "rb") as fi, open(obj, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        gz.unlink(missing_ok=True)
        return obj

    if kind == "airfoil":
        if not name:
            raise ValueError("airfoil requires name (e.g. naca0012, n0012, ...)")
        safe = "".join(c for c in name if c.isalnum() or c in "._-").lower()
        alias = re.sub(r"^naca", "n", safe) if safe.startswith("naca") else safe
        out = dest / f"{alias}.dat"
        last = None
        for nm in dict.fromkeys([alias, safe]):
            for tmpl in (UIUC_DAT, AIRFOILTOOLS):
                try:
                    _download(tmpl.format(name=nm), out)
                    if out.stat().st_size > 50:      # sanity: a real coordinate file
                        return out
                except Exception as e:               # noqa: BLE001
                    last = e
        raise last or ValueError(f"airfoil download failed: {name}")

    if kind == "url":
        if not url:
            raise ValueError("url required")
        clean = url.split("?")[0].split("#")[0]
        ext = Path(clean).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise ValueError(f"extension not allowed: {ext or '(none)'} (allowed: {sorted(ALLOWED_EXT)})")
        out = dest / Path(clean).name
        _download(url, out)
        return out

    raise ValueError(f"unknown kind: {kind} (motorbike|airfoil|url)")
