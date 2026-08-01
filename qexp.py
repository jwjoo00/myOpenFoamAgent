#!/usr/bin/env python
"""qexp.py — gmsh 외부메시 품질 실험 하니스 (self-improve 루프 설계용 데이터 수집).

OBJ → pymeshfix repair(+decimate) → gmsh 외부 volume(차체 띄움, knob 적용) →
Netgen optimize → gmshToFoam → checkMesh. 마지막 줄에 JSON 한 줄(품질지표)만 stdout.
나머지 진단은 stderr. 같은 머신에서 여러 config를 병렬로 돌려 품질 landscape를 본다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT = str(Path(__file__).resolve().parent)
sys.path.insert(0, PROJECT)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def deviation_pct(orig_obj, final_stl):
    """원본 STL 대비 최종(메싱된) 표면이 얼마나 어긋났나 — geometric fidelity 측정.
    최종 표면 위 점들의 원본까지 최단거리(Hausdorff류)를 body 대각선 % 로."""
    import numpy as np
    import trimesh

    orig = trimesh.load(orig_obj, force="mesh")
    fin = trimesh.load(final_stl, force="mesh")
    pts = np.asarray(fin.vertices)
    if len(pts) > 12000:                      # cap query cost
        pts = pts[np.random.choice(len(pts), 12000, replace=False)]
    _, dist, _ = trimesh.proximity.closest_point(orig, pts)
    diag = float(np.linalg.norm(orig.extents)) or 1.0
    return {"max_pct": round(100 * float(np.max(dist)) / diag, 3),
            "mean_pct": round(100 * float(np.mean(dist)) / diag, 3),
            "diag": round(diag, 4)}


def repair(obj, dest, target_faces, smooth=0, acvd=0, manifold=False, smooth_method="taubin"):
    import numpy as np
    import trimesh

    def close_watertight(mesh):
        import pymeshfix
        mf = pymeshfix.MeshFix(np.asarray(mesh.vertices, float), np.asarray(mesh.faces, np.int32))
        try:
            mf.repair(remove_smallest_components=False)
        except TypeError:
            mf.repair()
        v, f = mf._return_arrays()
        return trimesh.Trimesh(v, f, process=False)

    m = trimesh.load(obj, force="mesh")
    raw = len(m.faces)
    if target_faces and raw > target_faces:
        try:
            m = m.simplify_quadric_decimation(face_count=int(target_faces))
        except Exception as e:
            log("decimation skip:", e)

    if manifold:
        # manifold3d: robust watertight repair (alternative to pymeshfix)
        import manifold3d
        mani = manifold3d.Manifold(manifold3d.Mesh(
            np.asarray(m.vertices, dtype=np.float32),
            np.asarray(m.faces, dtype=np.uint32)))
        mm = mani.to_mesh()
        rep = trimesh.Trimesh(np.asarray(mm.vert_properties)[:, :3],
                              np.asarray(mm.tri_verts), process=False)
    else:
        rep = close_watertight(m)

    if acvd:
        # pyacvd: uniform isotropic surface remesh -> attacks the jagged-surface cause
        # of high boundary non-orthogonality (re-triangulates instead of moving verts)
        import pyacvd
        import pyvista as pv
        clus = pyacvd.Clustering(pv.wrap(rep))
        clus.subdivide(2)
        clus.cluster(int(acvd))
        rm = clus.create_mesh()
        ff = np.asarray(rm.faces).reshape(-1, 4)[:, 1:]
        rep = trimesh.Trimesh(np.asarray(rm.points), ff, process=False)
        rep = close_watertight(rep)        # acvd can open the surface -> re-close

    if smooth:
        # geometry smoothing of the WALL — rounds the sharp angles that force bad
        # boundary tets. Humphrey preserves features better than Taubin; re-close
        # removes any self-intersections the vertex move created (so gmsh won't choke).
        import trimesh.smoothing as sm
        if smooth_method == "humphrey":
            sm.filter_humphrey(rep, iterations=int(smooth))
        else:
            sm.filter_taubin(rep, iterations=int(smooth))
        rep = close_watertight(rep)

    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    rep.export(dest)
    return raw, int(len(rep.faces)), bool(rep.is_watertight)


def mesh(stl, msh, *, algo, opt, passes, grade, sizemin, sizemax):
    import gmsh

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.merge(stl)
        gmsh.model.mesh.removeDuplicateNodes()
        gmsh.model.mesh.createTopology()
        xa, ya, za, xb, yb, zb = gmsh.model.getBoundingBox(-1, -1)
        L, W, H = xb - xa, yb - ya, zb - za
        body = [e[1] for e in gmsh.model.getEntities(2)]
        body_sl = gmsh.model.geo.addSurfaceLoop(body)
        sm = float(sizemax) if sizemax else max(L, W, H) * 0.25
        bx0, bx1 = xa - 2 * L, xb + 5 * L
        by0, by1 = ya - 2 * W, yb + 2 * W
        bz0, bz1 = za - 2 * H, zb + 3 * H     # float the body off the box floor
        c = [(bx0, by0, bz0), (bx1, by0, bz0), (bx1, by1, bz0), (bx0, by1, bz0),
             (bx0, by0, bz1), (bx1, by0, bz1), (bx1, by1, bz1), (bx0, by1, bz1)]
        p = [gmsh.model.geo.addPoint(*xyz, sm) for xyz in c]
        E = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
             (0, 4), (1, 5), (2, 6), (3, 7)]
        ln = [gmsh.model.geo.addLine(p[a], p[b]) for a, b in E]
        loops = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 9, -4, -8),
                 (1, 10, -5, -9), (2, 11, -6, -10), (3, 8, -7, -11)]
        faces = [gmsh.model.geo.addPlaneSurface(
            [gmsh.model.geo.addCurveLoop([ln[i] if i >= 0 else -ln[-i] for i in q])])
            for q in loops]
        box_sl = gmsh.model.geo.addSurfaceLoop(faces)
        vol = gmsh.model.geo.addVolume([box_sl, body_sl])
        gmsh.model.geo.synchronize()
        gmsh.model.addPhysicalGroup(3, [vol], name="internal")
        gmsh.model.addPhysicalGroup(2, [faces[2]], name="inlet")
        gmsh.model.addPhysicalGroup(2, [faces[4]], name="outlet")
        gmsh.model.addPhysicalGroup(2, [faces[0]], name="bottom")
        gmsh.model.addPhysicalGroup(2, [faces[1], faces[3], faces[5]], name="farfield")
        gmsh.model.addPhysicalGroup(2, body, name="body")
        if grade:
            gmsh.model.mesh.field.add("Distance", 1)
            gmsh.model.mesh.field.setNumbers(1, "SurfacesList", body)
            gmsh.model.mesh.field.add("Threshold", 2)
            gmsh.model.mesh.field.setNumber(2, "InField", 1)
            gmsh.model.mesh.field.setNumber(2, "SizeMin", float(sizemin))
            gmsh.model.mesh.field.setNumber(2, "SizeMax", sm)
            gmsh.model.mesh.field.setNumber(2, "DistMin", 0.02)
            gmsh.model.mesh.field.setNumber(2, "DistMax", max(L, W, H) * 0.5)
            gmsh.model.mesh.field.setAsBackgroundMesh(2)
            gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
            gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.Algorithm3D", int(algo))
        gmsh.model.mesh.generate(3)
        for _ in range(int(passes)):
            try:
                gmsh.model.mesh.optimize(opt, niter=2)
            except Exception as e:
                log("optimize skip:", e)
        _, et, _ = gmsh.model.mesh.getElements(3)
        tags = [t for s in et for t in s]
        q = list(gmsh.model.mesh.getElementQualities(tags, "minSICN")) if tags else []
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        Path(msh).parent.mkdir(parents=True, exist_ok=True)
        gmsh.write(msh)
        return len(tags), (float(min(q)) if q else -1.0)
    finally:
        gmsh.finalize()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--obj", required=True)
    ap.add_argument("--case", required=True)
    ap.add_argument("--target-faces", type=int, default=20000)
    ap.add_argument("--algo", type=int, default=10)        # 1 Delaunay, 4 Frontal, 10 HXT
    ap.add_argument("--opt", default="Netgen")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--grade", action="store_true")
    ap.add_argument("--sizemin", type=float, default=0.03)
    ap.add_argument("--sizemax", type=float, default=0.0)  # 0 -> auto
    ap.add_argument("--smooth", type=int, default=0)       # Taubin smoothing iterations on the body
    ap.add_argument("--acvd", type=int, default=0)         # pyacvd uniform remesh to N clusters
    ap.add_argument("--manifold", action="store_true")    # use manifold3d repair instead of pymeshfix
    ap.add_argument("--smooth-method", default="taubin", choices=["taubin", "humphrey"])
    args = ap.parse_args()

    import foam
    import meshgen

    cfg = {"target_faces": args.target_faces, "algo": args.algo, "opt": args.opt,
           "passes": args.passes, "grade": args.grade, "sizemin": args.sizemin,
           "sizemax": args.sizemax, "smooth": args.smooth, "smooth_method": args.smooth_method,
           "acvd": args.acvd, "manifold": args.manifold}
    out = {"config": cfg, "ok": False}
    try:
        case = Path(args.case)
        work = case / "meshWork"
        fixed = work / "body_fixed.stl"
        raw, nf, wt = repair(args.obj, str(fixed), args.target_faces, smooth=args.smooth,
                             acvd=args.acvd, manifold=args.manifold,
                             smooth_method=args.smooth_method)
        out.update(raw_faces=raw, repaired_faces=nf, watertight=wt)
        try:
            out["deviation"] = deviation_pct(args.obj, str(fixed))   # 원본 대비 변형량
        except Exception as e:
            log("deviation skip:", e)
        log("repaired", nf, "watertight", wt)
        if not wt:
            out["error"] = "not watertight after repair"
            print(json.dumps(out))
            return
        nel, minsicn = mesh(str(fixed), str(work / "mesh.msh"), algo=args.algo, opt=args.opt,
                            passes=args.passes, grade=args.grade,
                            sizemin=args.sizemin, sizemax=args.sizemax)
        out.update(n_elements=nel, min_sicn=round(minsicn, 4))
        log("meshed", nel, "minSICN", minsicn)
        meshgen._ensure_case_skeleton(case)
        r = foam.run_foam("gmshToFoam", [str(work / "mesh.msh")], case=case, timeout=600)
        if not r.ok:
            out["error"] = "gmshToFoam failed"
            print(json.dumps(out))
            return
        rc = foam.run_foam("checkMesh", case=case, timeout=600)
        cm = foam.parse_checkmesh(rc.stdout)
        out.update(ok=bool(cm.get("mesh_ok")), mesh_cells=cm.get("cells"),
                   max_non_ortho=cm.get("max_non_ortho"), max_skewness=cm.get("max_skewness"),
                   mesh_ok=cm.get("mesh_ok"))
    except Exception as e:
        import traceback
        log(traceback.format_exc())
        out["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(out))


if __name__ == "__main__":
    main()
