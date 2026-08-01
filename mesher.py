"""
mesher.py — gmsh meshing engine for the validator-gated repair loop (Phase 3).

Builds the geometry, meshes it, optimizes, writes mesh.msh (v2.2) and returns a
quality dict {n_elements, min(SICN), patches, ...}. The size_min/size_max knobs are
exactly what the caeloop repair backend rewrites on a validation failure.

Run inside the project's venv (gmsh installed there). Each call is initialize ->
... -> finalize, so it is safe to invoke once per subprocess (the repair loop does).
"""
from __future__ import annotations

from pathlib import Path


def _classify_surfaces(gmsh, x0, y0, x1, y1, T, eps=1e-3, body="cylinder") -> dict:
    """Assign each boundary surface to an OF patch by its bounding box. `body` names
    the inner-object patch (cylinder / airfoil / ...)."""
    g = {k: [] for k in ("inlet", "outlet", "top", "bottom", body, "front", "back")}
    for (d, t) in gmsh.model.getEntities(2):
        bx0, by0, bz0, bx1, by1, bz1 = gmsh.model.getBoundingBox(d, t)
        if abs(bz1 - bz0) < eps and abs(bz0) < eps:
            g["front"].append(t)
        elif abs(bz1 - bz0) < eps and abs(bz0 - T) < eps:
            g["back"].append(t)
        elif abs(bx0 - x0) < eps and abs(bx1 - x0) < eps:
            g["inlet"].append(t)
        elif abs(bx0 - x1) < eps and abs(bx1 - x1) < eps:
            g["outlet"].append(t)
        elif abs(by0 - y0) < eps and abs(by1 - y0) < eps:
            g["bottom"].append(t)
        elif abs(by0 - y1) < eps and abs(by1 - y1) < eps:
            g["top"].append(t)
        else:
            g[body].append(t)
    return g


def _read_airfoil_dat(path):
    """UIUC Selig .dat -> list of (x,y) on the closed contour (header/count lines skipped)."""
    pts = []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                x, y = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            if abs(x) <= 2.0 and abs(y) <= 2.0:   # skip name header & Lednicer count line
                pts.append((x, y))
    return pts


def mesh_airfoil_2d(out_dir, size_min, size_max, *, dat_path, chord=1.0, aoa=0.0,
                    domain=(-6.0, -6.0, 14.0, 6.0), thickness=1.0,
                    recombine=True, optimize=True, msh_name="mesh.msh") -> dict:
    """2D airfoil-in-channel as a 1-cell-thick OF slab (front/back empty). Reads a UIUC
    .dat, scales by chord, rotates by aoa(deg) about the quarter-chord, cuts it from the
    domain. size_min grades near the airfoil."""
    import math

    import gmsh

    pts = _read_airfoil_dat(dat_path)
    if len(pts) < 10:
        raise ValueError(f"not enough points in airfoil .dat: {len(pts)} ({dat_path})")
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    a = -math.radians(aoa)
    ca, sa = math.cos(a), math.sin(a)
    P = []
    for (x, y) in pts:
        xs, ys = x * chord, y * chord
        xr = ca * (xs - 0.25 * chord) - sa * ys + 0.25 * chord
        yr = sa * (xs - 0.25 * chord) + ca * ys
        P.append((xr, yr))

    x0, y0, x1, y1 = domain
    T = thickness
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("airfoil2d")
        occ = gmsh.model.occ
        gp = [occ.addPoint(x, y, 0.0) for (x, y) in P]
        lines = [occ.addLine(gp[i], gp[(i + 1) % len(gp)]) for i in range(len(gp))]
        loop = occ.addCurveLoop(lines)
        foil = occ.addPlaneSurface([loop])
        rect = occ.addRectangle(x0, y0, 0.0, x1 - x0, y1 - y0)
        cut, _ = occ.cut([(2, rect)], [(2, foil)])
        occ.synchronize()
        base = [t for (d, t) in cut if d == 2]
        ext = occ.extrude([(2, s) for s in base], 0.0, 0.0, T,
                          numElements=[1], recombine=recombine)
        occ.synchronize()
        vol = [t for (d, t) in ext if d == 3]

        groups = _classify_surfaces(gmsh, x0, y0, x1, y1, T, body="airfoil")
        gmsh.model.addPhysicalGroup(3, vol, name="internal")
        for nm, tags in groups.items():
            if tags:
                gmsh.model.addPhysicalGroup(2, tags, name=nm)

        gmsh.model.mesh.field.add("Distance", 1)
        gmsh.model.mesh.field.setNumbers(1, "SurfacesList", groups["airfoil"])
        gmsh.model.mesh.field.add("Threshold", 2)
        gmsh.model.mesh.field.setNumber(2, "InField", 1)
        gmsh.model.mesh.field.setNumber(2, "SizeMin", float(size_min))
        gmsh.model.mesh.field.setNumber(2, "SizeMax", float(size_max))
        gmsh.model.mesh.field.setNumber(2, "DistMin", 0.1 * chord)
        gmsh.model.mesh.field.setNumber(2, "DistMax", 4.0 * chord)
        gmsh.model.mesh.field.setAsBackgroundMesh(2)
        for o in ("MeshSizeExtendFromBoundary", "MeshSizeFromPoints", "MeshSizeFromCurvature"):
            gmsh.option.setNumber("Mesh." + o, 0)
        if recombine:
            gmsh.option.setNumber("Mesh.RecombineAll", 1)
        gmsh.option.setNumber("Mesh.Algorithm", 6)

        gmsh.model.mesh.generate(3)
        if optimize:
            for method in ("Netgen", "Laplace2D"):
                try:
                    gmsh.model.mesh.optimize(method)
                except Exception:
                    pass

        _, etags, _ = gmsh.model.mesh.getElements(3)
        tags = [t for sub in etags for t in sub]
        q = list(gmsh.model.mesh.getElementQualities(tags, "minSICN")) if tags else []
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        msh = str(out / msh_name)
        gmsh.write(msh)
        return {"n_elements": len(tags), "min": round(float(min(q)), 4) if q else -1.0,
                "patches": [k for k, v in groups.items() if v], "msh": msh,
                "size_min": float(size_min), "size_max": float(size_max)}
    finally:
        gmsh.finalize()


def mesh_cylinder_2d(out_dir, size_min, size_max, *,
                     domain=(-8.0, -8.0, 24.0, 8.0), radius=0.5, thickness=1.0,
                     recombine=True, optimize=True, msh_name="mesh.msh") -> dict:
    """2D cylinder-in-channel as a 1-cell-thick OpenFOAM-ready slab (front/back are
    the empty pair). size_min grades the mesh near the cylinder, size_max far field."""
    import gmsh

    x0, y0, x1, y1 = domain
    R, T = radius, thickness
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("cyl2d")
        occ = gmsh.model.occ
        rect = occ.addRectangle(x0, y0, 0.0, x1 - x0, y1 - y0)
        disk = occ.addDisk(0.0, 0.0, 0.0, R, R)
        cut, _ = occ.cut([(2, rect)], [(2, disk)])
        occ.synchronize()
        base = [t for (d, t) in cut if d == 2]
        ext = occ.extrude([(2, s) for s in base], 0.0, 0.0, T,
                          numElements=[1], recombine=recombine)
        occ.synchronize()
        vol = [t for (d, t) in ext if d == 3]

        groups = _classify_surfaces(gmsh, x0, y0, x1, y1, T)
        gmsh.model.addPhysicalGroup(3, vol, name="internal")
        for nm, tags in groups.items():
            if tags:
                gmsh.model.addPhysicalGroup(2, tags, name=nm)

        # grade: fine at the cylinder (size_min), coarse far (size_max)
        gmsh.model.mesh.field.add("Distance", 1)
        gmsh.model.mesh.field.setNumbers(1, "SurfacesList", groups["cylinder"])
        gmsh.model.mesh.field.add("Threshold", 2)
        gmsh.model.mesh.field.setNumber(2, "InField", 1)
        gmsh.model.mesh.field.setNumber(2, "SizeMin", float(size_min))
        gmsh.model.mesh.field.setNumber(2, "SizeMax", float(size_max))
        gmsh.model.mesh.field.setNumber(2, "DistMin", R)
        gmsh.model.mesh.field.setNumber(2, "DistMax", 8.0)
        gmsh.model.mesh.field.setAsBackgroundMesh(2)
        for o in ("MeshSizeExtendFromBoundary", "MeshSizeFromPoints", "MeshSizeFromCurvature"):
            gmsh.option.setNumber("Mesh." + o, 0)
        if recombine:
            gmsh.option.setNumber("Mesh.RecombineAll", 1)
        gmsh.option.setNumber("Mesh.Algorithm", 6)

        gmsh.model.mesh.generate(3)
        if optimize:
            for method in ("Netgen", "Laplace2D"):
                try:
                    gmsh.model.mesh.optimize(method)
                except Exception:
                    pass

        _, etags, _ = gmsh.model.mesh.getElements(3)
        tags = [t for sub in etags for t in sub]
        q = list(gmsh.model.mesh.getElementQualities(tags, "minSICN")) if tags else []
        n_el = len(tags)
        min_q = min(q) if q else -1.0

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        msh = str(out / msh_name)
        gmsh.write(msh)
        return {"n_elements": n_el, "min": round(float(min_q), 4),
                "patches": [k for k, v in groups.items() if v], "msh": msh,
                "size_min": float(size_min), "size_max": float(size_max)}
    finally:
        gmsh.finalize()


def _close_watertight(mesh):
    import numpy as np
    import pymeshfix
    import trimesh
    mf = pymeshfix.MeshFix(np.asarray(mesh.vertices, float), np.asarray(mesh.faces, np.int32))
    try:
        mf.repair(remove_smallest_components=False)   # keep all parts
    except TypeError:
        mf.repair()
    v, f = mf._return_arrays()
    return trimesh.Trimesh(v, f, process=False)


def _surface_deviation(orig, mesh):
    """How far the final (to-be-meshed) surface strays from the original STL — % of the body diagonal (max/mean).
    For reporting 'how much the STL was modified'. None if rtree/proximity is unavailable."""
    import numpy as np
    import trimesh
    try:
        o = trimesh.load(str(orig), force="mesh")
        pts = np.asarray(mesh.vertices)
        if len(pts) > 12000:
            pts = pts[:: max(1, len(pts) // 12000)]
        _, dist, _ = trimesh.proximity.closest_point(o, pts)
        diag = float(np.linalg.norm(o.extents)) or 1.0
        return {"max_pct": round(100 * float(np.max(dist)) / diag, 3),
                "mean_pct": round(100 * float(np.mean(dist)) / diag, 3)}
    except Exception:
        return None


def repair_to_watertight(src, dest_stl, target_faces=20000, smooth=0,
                         smooth_method="humphrey") -> dict:
    """Load an OBJ/STL, decimate, repair (pymeshfix) → WATERTIGHT STL. If smooth>0, sharp wall
    angles are flattened (humphrey/taubin) to raise boundary tet quality (non-ortho↓): measured,
    the coarser the decimation + the larger the smooth, the lower non-ortho goes, 89°→81°. Self-intersections
    created by smoothing are removed by re-closing. The deviation from the original is returned too —
    the STL is not sacrosanct; the point is to measure and report 'how much was repaired'."""
    import trimesh

    m = trimesh.load(str(src), force="mesh")
    if target_faces and len(m.faces) > target_faces:
        try:
            m = m.simplify_quadric_decimation(face_count=int(target_faces))
        except Exception:
            pass
    rep = _close_watertight(m)
    if smooth:
        import trimesh.smoothing as sm
        if smooth_method == "taubin":
            sm.filter_taubin(rep, iterations=int(smooth))
        else:
            sm.filter_humphrey(rep, iterations=int(smooth))
        rep = _close_watertight(rep)     # remove self-intersections the vertex move created
    Path(dest_stl).parent.mkdir(parents=True, exist_ok=True)
    rep.export(str(dest_stl))
    return {"n_faces": int(len(rep.faces)), "watertight": bool(rep.is_watertight),
            "smooth": int(smooth), "deviation": _surface_deviation(src, rep)}


def mesh_external_stl_3d(out_dir, stl, size_max=None, *, pad=(2, 5, 2, 3, 2),
                         optimize=True, opt_method="Netgen", opt_passes=2,
                         algo=10, grade=False, sizemin=0.03, msh_name="mesh.msh") -> dict:
    """External volume mesh around a WATERTIGHT body STL (box minus body, tets).
    The body is FLOATED off the box floor — letting it touch breaks HXT facet
    recovery. optimize/opt_passes/algo/grade/sizemin are the quality knobs the
    self-improve loop sweeps (3D algo: 1 Delaunay, 4 Frontal, 10 HXT; grade adds a
    Distance+Threshold field for near-wall refinement)."""
    import gmsh

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.merge(str(stl))
        gmsh.model.mesh.removeDuplicateNodes()
        gmsh.model.mesh.createTopology()
        xa, ya, za, xb, yb, zb = gmsh.model.getBoundingBox(-1, -1)
        L, W, H = xb - xa, yb - ya, zb - za
        body = [e[1] for e in gmsh.model.getEntities(2)]
        body_sl = gmsh.model.geo.addSurfaceLoop(body)

        sm = float(size_max) if size_max else max(L, W, H) * 0.25
        pu, pd, ps, pt, pb = pad
        bx0, bx1 = xa - pu * L, xb + pd * L
        by0, by1 = ya - ps * W, yb + ps * W
        bz0, bz1 = za - pb * H, zb + pt * H
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
        # box faces: [0]=-Z [1]=+Z [2]=-Y [3]=+X [4]=+Y [5]=-X. Flow is +X (U=(U 0 0)),
        # so inlet=-X, outlet=+X — must match the streamwise direction or the body sees no flow.
        gmsh.model.addPhysicalGroup(2, [faces[5]], name="inlet")
        gmsh.model.addPhysicalGroup(2, [faces[3]], name="outlet")
        gmsh.model.addPhysicalGroup(2, [faces[0]], name="bottom")
        gmsh.model.addPhysicalGroup(2, [faces[1], faces[2], faces[4]], name="farfield")
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
        gmsh.option.setNumber("Mesh.Algorithm3D", int(algo))   # 1 Delaunay, 4 Frontal, 10 HXT
        gmsh.model.mesh.generate(3)
        passes = int(opt_passes) if optimize else 0
        for _ in range(passes):
            try:
                gmsh.model.mesh.optimize(opt_method, niter=2)
            except Exception:
                pass

        _, et, _ = gmsh.model.mesh.getElements(3)
        tags = [t for s in et for t in s]
        q = list(gmsh.model.mesh.getElementQualities(tags, "minSICN")) if tags else []
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        msh = str(out / msh_name)
        gmsh.write(msh)
        return {"n_elements": len(tags), "min": round(float(min(q)), 4) if q else -1.0,
                "patches": ["inlet", "outlet", "bottom", "farfield", "body"], "msh": msh,
                "knobs": {"algo": int(algo), "opt_passes": (int(opt_passes) if optimize else 0),
                          "grade": bool(grade), "sizemin": float(sizemin)}}
    finally:
        gmsh.finalize()


def _stl_to_step_solid(stl, step):
    """OCP (cadquery-ocp): watertight STL through sew→solid→ShapeFix→STEP. Fails if dirty/non-watertight."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.StlAPI import StlAPI_Reader
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Shape

    sh = TopoDS_Shape()
    StlAPI_Reader().Read(sh, str(stl))
    sew = BRepBuilderAPI_Sewing(1e-3)
    sew.Add(sh)
    sew.Perform()
    exp = TopExp_Explorer(sew.SewedShape(), TopAbs_SHELL)
    if not exp.More():
        raise RuntimeError("OCC sewing could not build a closed shell (STL is not watertight)")
    solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(exp.Current())).Solid()
    fx = ShapeFix_Solid(solid)
    fx.Perform()
    solid = fx.Solid()
    w = STEPControl_Writer()
    w.Transfer(fx.Solid(), STEPControl_AsIs)
    w.Write(str(step))


def _netgen_to_gmsh(m, path, patches=("inlet", "outlet", "farfield", "body")) -> int:
    """netgen Mesh → gmsh v2.2 .msh (tet type4 + prism type6 BL + boundary tri type2).
    Patches classified by FaceDescriptor.bcname; the BL-internal interfaces (mapped_*) are left as internal."""
    pts = m.Points()
    fds = list(m.FaceDescriptors())
    bcname = {i: fd.bcname for i, fd in enumerate(fds, start=1)}
    physid = {nm: k + 1 for k, nm in enumerate(patches)}
    INT = 100
    elems = []
    eid = 0
    for el in m.Elements2D():
        nm = bcname.get(el.index, "")
        if nm not in physid:
            continue
        vs = [v.nr for v in el.vertices]
        if len(vs) != 3:
            continue
        eid += 1
        pid = physid[nm]
        elems.append(f"{eid} 2 2 {pid} {pid} {vs[0]} {vs[1]} {vs[2]}")
    for el in m.Elements3D():
        vs = [v.nr for v in el.vertices]
        eid += 1
        if len(vs) == 4:
            elems.append(f"{eid} 4 2 {INT} {INT} {vs[0]} {vs[1]} {vs[3]} {vs[2]}")
        elif len(vs) == 6:
            o = [vs[3], vs[4], vs[5], vs[0], vs[1], vs[2]]   # netgen→gmsh prism: swap tri faces
            elems.append(f"{eid} 6 2 {INT} {INT} " + " ".join(map(str, o)))
    npris = sum(1 for el in m.Elements3D() if len(el.vertices) == 6)
    with open(path, "w") as f:
        f.write("$MeshFormat\n2.2 0 8\n$EndMeshFormat\n")
        f.write(f"$PhysicalNames\n{len(patches) + 1}\n")
        for nm in patches:
            f.write(f"2 {physid[nm]} \"{nm}\"\n")
        f.write(f"3 {INT} \"internal\"\n$EndPhysicalNames\n")
        f.write(f"$Nodes\n{len(pts)}\n")
        for i, p in enumerate(pts, start=1):
            x, y, z = p.p
            f.write(f"{i} {x} {y} {z}\n")
        f.write("$EndNodes\n")
        f.write(f"$Elements\n{len(elems)}\n")
        f.write("\n".join(elems))
        f.write("\n$EndElements\n")
    return npris


def mesh_external_netgen_bl(out_dir, stl, *, n_layers=3, first_layer=None, expansion=1.3,
                            maxh_factor=0.22, pad=(2, 5, 2, 3, 2), msh_name="mesh.msh") -> dict:
    """**Clean watertight** body STL → OCC solid (OCP sew) → netgen meshes the OCC box−body **including
    the prism boundary layer in one shot** → gmsh .msh. For smooth shapes (sphere, Ahmed, a car without mirrors).
    On shapes with many thin features, like a bike, netgen BL breaks — use snappy add_boundary_layers instead."""
    import numpy as np
    import trimesh

    import netgen.meshing as NM
    from netgen.occ import Box, OCCGeometry, Pnt

    out = Path(out_dir)
    work = out / "meshWork"
    work.mkdir(parents=True, exist_ok=True)
    step = work / "body.step"
    _stl_to_step_solid(stl, step)

    body = OCCGeometry(str(step)).shape
    for f in body.faces:
        f.name = "body"
    b = trimesh.load(str(stl), force="mesh")
    mn = np.asarray(b.vertices).min(0)
    mx = np.asarray(b.vertices).max(0)
    L, W, H = (mx - mn)
    diag = float(np.linalg.norm(mx - mn))
    pu, pd, ps, pt, pb = pad
    bx0, bx1 = float(mn[0] - pu * L), float(mx[0] + pd * L)
    box = Box(Pnt(bx0, mn[1] - ps * W, mn[2] - pb * H),
              Pnt(bx1, mx[1] + ps * W, mx[2] + pt * H))
    tol = 0.01 * (bx1 - bx0)
    for f in box.faces:
        cx = f.center.x
        f.name = "inlet" if abs(cx - bx0) < tol else "outlet" if abs(cx - bx1) < tol else "farfield"

    geo = OCCGeometry(box - body)
    mh = float(max(L, W, H) * maxh_factor)
    flt = float(first_layer) if first_layer else diag * 0.0015
    th = [flt * (expansion ** i) for i in range(int(n_layers))]
    blp = NM.BoundaryLayerParameters(boundary="body", thickness=th, new_material="fluid",
                                     limit_growth_vectors=True)
    m = geo.GenerateMesh(maxh=mh, boundary_layers=[blp])
    msh = work / "mesh.msh"
    npris = _netgen_to_gmsh(m, str(msh))
    return {"msh": str(msh), "n_elements": int(m.ne), "n_prisms": npris,
            "n_layers": int(n_layers), "first_layer": round(flt, 6),
            "patches": ["inlet", "outlet", "farfield", "body"]}


GEOMETRIES = {"cylinder_2d": mesh_cylinder_2d}
