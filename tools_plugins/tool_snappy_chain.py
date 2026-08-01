"""Plugin: snappy_chain
Robust external-flow snappyHexMesh chain that does NOT silently return the
background blockMesh as success. Copies STL into constant/triSurface/body.stl,
patches snappyHexMeshDict (maxGlobalCells + wake refinement box), runs
surfaceFeatures -> blockMesh -> snappyHexMesh (parallel or serial) -> checkMesh,
then parses checkMesh and FAILS if the final cell count is suspiciously equal to
the background (snap did not happen) or if body patch has no faces.
"""
import os
import re
import shutil
import subprocess

SPEC = {
    "name": "snappy_chain",
    "description": (
        "Robust snappyHexMesh chain for external flow (OF12). Copies STL to "
        "constant/triSurface/body.stl, patches snappyHexMeshDict with "
        "maxGlobalCells and an optional wake refinement box, runs "
        "surfaceFeatures->blockMesh->snappyHexMesh->checkMesh, and (unlike "
        "run_snappy_mesh) FAILS loudly if snap produced only the background "
        "mesh. Use for Ahmed/DDES where wake LES-grade refinement is needed. "
        "[approval required]"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "case_dir": {"type": "string"},
            "stl": {"type": "string", "description": "source STL path"},
            "refine_min": {"type": "integer"},
            "refine_max": {"type": "integer"},
            "n_layers": {"type": "integer"},
            "max_cells": {"type": "integer", "description": "maxGlobalCells"},
            "wake_box": {
                "type": "string",
                "description": "wake refinement box as 'x0 y0 z0 x1 y1 z1 level' (optional)",
            },
            "nprocs": {"type": "integer", "description": "0=serial"},
        },
        "required": ["case_dir", "stl"],
    },
}

GATED = {"snappy_chain"}


def _run(cmd, cwd, log_path):
    with open(log_path, "w") as f:
        p = subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)
    return p.returncode


def _patch_snappy_dict(case_dir, max_cells, wake_box):
    d = os.path.join(case_dir, "system", "snappyHexMeshDict")
    with open(d) as f:
        txt = f.read()
    if max_cells:
        txt = re.sub(r"maxGlobalCells\s+\d+", "maxGlobalCells %d" % int(max_cells), txt)
    if wake_box:
        parts = [float(x) for x in wake_box.split()]
        x0, y0, z0, x1, y1, z1 = parts[:6]
        lvl = int(parts[6]) if len(parts) > 6 else 4
        region = (
            "geometry { body { type triSurfaceMesh; file \"body.stl\"; } "
            "wakeBox { type searchableBox; min (%g %g %g); max (%g %g %g); } }"
            % (x0, y0, z0, x1, y1, z1)
        )
        txt = re.sub(r"geometry\s*\{[^}]*\}\s*\}", region, txt, count=1)
        rr = "refinementRegions { wakeBox { mode inside; levels ((1e15 %d)); } }" % lvl
        txt = re.sub(r"refinementRegions\s*\{\s*\}", rr, txt, count=1)
    with open(d, "w") as f:
        f.write(txt)


def _parse_checkmesh(log_path):
    cells = None
    body_faces = None
    ok = False
    nonortho = None
    skew = None
    if not os.path.exists(log_path):
        return {"cells": None, "mesh_ok": False}
    with open(log_path) as f:
        t = f.read()
    m = re.search(r"cells:\s+(\d+)", t)
    if m:
        cells = int(m.group(1))
    m = re.search(r"Mesh OK", t)
    ok = bool(m)
    m = re.search(r"non-orthogonality Max:\s*([\d.eE+-]+)", t)
    if m:
        nonortho = float(m.group(1))
    m = re.search(r"Max skewness\s*=\s*([\d.eE+-]+)", t)
    if m:
        skew = float(m.group(1))
    m = re.search(r"\bbody\b\s+(\d+)", t)
    if m:
        body_faces = int(m.group(1))
    return {
        "cells": cells,
        "body_faces": body_faces,
        "max_non_ortho": nonortho,
        "max_skewness": skew,
        "checkmesh_ok": ok,
    }


def handler(**args):
    case_dir = args["case_dir"]
    stl = args["stl"]
    refine_min = int(args.get("refine_min", 4))
    refine_max = int(args.get("refine_max", 5))
    n_layers = int(args.get("n_layers", 6))
    max_cells = int(args.get("max_cells", 12000000))
    wake_box = args.get("wake_box", "")
    nprocs = int(args.get("nprocs", 0))

    if not os.path.isdir(case_dir):
        return {"ok": False, "error": "case_dir not found: %s" % case_dir}
    if not os.path.exists(stl):
        return {"ok": False, "error": "stl not found: %s" % stl}

    tri = os.path.join(case_dir, "constant", "triSurface")
    os.makedirs(tri, exist_ok=True)
    shutil.copyfile(stl, os.path.join(tri, "body.stl"))

    # refinementSurfaces level
    d = os.path.join(case_dir, "system", "snappyHexMeshDict")
    with open(d) as f:
        txt = f.read()
    txt = re.sub(
        r"refinementSurfaces\s*\{\s*body\s*\{\s*level\s*\(\s*\d+\s+\d+\s*\)",
        "refinementSurfaces { body { level (%d %d)" % (refine_min, refine_max),
        txt,
        count=1,
    )
    txt = re.sub(r"nSurfaceLayers\s+\d+", "nSurfaceLayers %d" % n_layers, txt)
    with open(d, "w") as f:
        f.write(txt)

    _patch_snappy_dict(case_dir, max_cells, wake_box)

    steps = []
    rc = _run(["surfaceFeatures"], case_dir, os.path.join(case_dir, "log.surfaceFeatures"))
    steps.append(["surfaceFeatures", rc == 0])
    rc = _run(["blockMesh"], case_dir, os.path.join(case_dir, "log.blockMesh"))
    steps.append(["blockMesh", rc == 0])
    if rc != 0:
        return {"ok": False, "error": "blockMesh failed", "steps": steps}

    if nprocs and nprocs > 1:
        dp = os.path.join(case_dir, "system", "decomposeParDict")
        with open(dp) as f:
            dptxt = f.read()
        dptxt = re.sub(r"numberOfSubdomains\s+\d+", "numberOfSubdomains %d" % nprocs, dptxt)
        with open(dp, "w") as f:
            f.write(dptxt)
        _run(["decomposePar", "-force"], case_dir, os.path.join(case_dir, "log.decomposePar"))
        rc = _run(
            ["mpirun", "-np", str(nprocs), "snappyHexMesh", "-overwrite", "-parallel"],
            case_dir,
            os.path.join(case_dir, "log.snappyHexMesh"),
        )
        steps.append(["snappyHexMesh(parallel %d)" % nprocs, rc == 0])
        _run(["reconstructParMesh", "-constant", "-mergeTol", "1e-6"], case_dir,
             os.path.join(case_dir, "log.reconstructParMesh"))
    else:
        rc = _run(["snappyHexMesh", "-overwrite"], case_dir,
                  os.path.join(case_dir, "log.snappyHexMesh"))
        steps.append(["snappyHexMesh(serial)", rc == 0])

    _run(["checkMesh", "-constant"], case_dir, os.path.join(case_dir, "log.checkMesh"))
    cm = _parse_checkmesh(os.path.join(case_dir, "log.checkMesh"))

    # background cell count from blockMeshDict (product of block divisions)
    bg = None
    try:
        with open(os.path.join(case_dir, "system", "blockMeshDict")) as f:
            bm = f.read()
        m = re.search(r"hex\s*\([^)]*\)\s*\((\d+)\s+(\d+)\s+(\d+)\)", bm)
        if m:
            bg = int(m.group(1)) * int(m.group(2)) * int(m.group(3))
    except Exception:
        pass

    cells = cm.get("cells")
    snap_failed = False
    reason = None
    if cells is not None and bg is not None and cells <= int(bg * 1.05):
        snap_failed = True
        reason = "final cells (%s) ~ background (%s): snap produced no body" % (cells, bg)
    if cm.get("body_faces") in (0, None):
        # body_faces None may just be parse miss; only flag if also low cells
        if cells is not None and bg is not None and cells <= int(bg * 1.5):
            snap_failed = True
            reason = (reason or "") + " | body patch has ~no faces"

    ok = (not snap_failed) and bool(cm.get("checkmesh_ok"))
    return {
        "ok": ok,
        "snap_failed": snap_failed,
        "reason": reason,
        "background_cells": bg,
        "mesh_cells": cells,
        "body_faces": cm.get("body_faces"),
        "max_non_ortho": cm.get("max_non_ortho"),
        "max_skewness": cm.get("max_skewness"),
        "steps": steps,
        "wake_box": wake_box,
        "refine": [refine_min, refine_max],
        "n_layers": n_layers,
        "max_cells": max_cells,
    }
