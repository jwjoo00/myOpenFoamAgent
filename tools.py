"""
tools.py — the typed tools the Claude agent drives via tool-use.

Each tool is a pure function returning a JSON-able dict {ok, ...}. They wrap the
verified foam.py (OpenFOAM execution) and registry.py (persistence). No Anthropic
dependency here, so the tools are unit-testable on their own; agent.py owns the LLM
loop and the human-approval gate.

Convention: a run's case dir is RUNS_ROOT/<run_id>, so run_id == basename(case_dir).
That lets the tools auto-update the registry without the model threading run_id.
"""
from __future__ import annotations

import os
from pathlib import Path

import config
import foam
import registry

# Tools that change state / run compute -> agent.py asks the user before executing.
GATED = {"write_dict_entry", "run_mesh", "run_solver", "write_file",
         "generate_mesh", "fetch_geometry", "add_boundary_layers", "scaffold_external_ras",
         "run_snappy_mesh", "delete_path", "scaffold_mrf", "generate_propeller"}


def _run_id_of(case_dir: str | os.PathLike) -> str:
    return Path(case_dir).name


def _confined(path) -> Path | None:
    """Resolve `path` and return it ONLY if it lies within RUNS_ROOT or REPORTS_ROOT
    (model output is untrusted — rejects '..', symlinks, absolute escapes)."""
    try:
        rp = Path(os.path.abspath(os.path.expanduser(str(path)))).resolve()
    except Exception:
        return None
    for root in (config.RUNS_ROOT, config.REPORTS_ROOT):
        try:
            if rp == Path(root).resolve() or rp.is_relative_to(Path(root).resolve()):
                return rp
        except Exception:
            continue
    return None


def _ls_case(case_dir: Path) -> dict:
    out = {}
    for sub in ("0", "constant", "system"):
        d = case_dir / sub
        out[sub] = sorted(p.name for p in d.iterdir()) if d.is_dir() else []
    return out


# ----------------------------------------------------------------- discovery
def list_tutorials(filter: str = "", limit: int = 40) -> dict:
    """List installed OF12 tutorial cases (dirs containing system/controlDict),
    optionally filtered by a substring. The ground-truth corpus for OF12 syntax."""
    base = Path(config.FOAM_TUTORIALS)
    if not base.is_dir():
        return {"ok": False, "error": f"tutorials dir not found: {base}"}
    f = filter.lower()
    hits = []
    for dirpath, _dirs, files in os.walk(base):
        if os.path.basename(dirpath) == "system" and "controlDict" in files:
            rel = os.path.relpath(os.path.dirname(dirpath), base)
            if f in rel.lower():
                hits.append(rel)
    hits.sort()
    return {"ok": True, "count": len(hits), "tutorials": hits[:limit],
            "truncated": len(hits) > limit}


def copy_case(tutorial: str, run_name: str = "") -> dict:
    """Copy a tutorial case into a fresh run dir under RUNS_ROOT and register it."""
    try:
        case_kind = (run_name or tutorial.split("/")[-1]).strip()
        run_id = registry.new_run_id(case_kind)
        dest = config.RUNS_ROOT / run_id
        foam.copy_tutorial(tutorial, dest)
        solver = foam.get_dict_entry(dest, "system/controlDict", "solver")
        turb = foam.get_dict_entry(dest, "constant/momentumTransport", "model")
        end_time = foam.get_dict_entry(dest, "system/controlDict", "endTime")
        registry.record_run({
            "run_id": run_id, "case_kind": case_kind,
            "geometry_src": f"tutorial:{tutorial}", "solver": solver,
            "turb_model": turb, "end_time": _to_float(end_time),
            "status": "created", "case_dir": str(dest),
        })
        return {"ok": True, "run_id": run_id, "case_dir": str(dest),
                "solver": solver, "turb_model": turb, "end_time": end_time,
                "files": _ls_case(dest)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def new_case(name: str = "case") -> dict:
    """Create and register a new empty case folder + run_id for non-tutorial (from-scratch, e.g. gmsh) geometry.
    Fill in the mesh/case afterwards with generate_mesh(case_dir). The from-scratch counterpart of copy_case."""
    try:
        run_id = registry.new_run_id(name)
        dest = config.RUNS_ROOT / run_id
        dest.mkdir(parents=True, exist_ok=True)
        registry.record_run({"run_id": run_id, "case_kind": name,
                             "status": "created", "case_dir": str(dest)})
        return {"ok": True, "run_id": run_id, "case_dir": str(dest)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def fetch_geometry(case_dir: str, kind: str = "motorbike", name: str = "",
                   url: str = "") -> dict:
    """Download geometry from the internet without CAD (no-login curated sources) → case/geometry/.
    kind: motorbike | airfoil(name required, e.g. naca0012) | url(.stl/.obj/.dat etc). GATED."""
    dest_dir = _confined(str(Path(case_dir) / "geometry"))
    if dest_dir is None:
        return {"ok": False, "error": "case_dir is outside the allowed paths (must be a case folder)"}
    try:
        import geom
        path = geom.fetch(kind, dest_dir, name=name or None, url=url or None)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "path": str(path), "kind": kind,
            "format": path.suffix.lstrip("."), "bytes": path.stat().st_size}


# ----------------------------------------------------------------- inspect/edit
def read_dict(case_dir: str, rel_path: str, max_chars: int = 4000) -> dict:
    p = Path(case_dir) / rel_path
    if not p.is_file():
        return {"ok": False, "error": f"not found: {p}"}
    txt = p.read_text()
    return {"ok": True, "path": str(p), "text": txt[:max_chars],
            "truncated": len(txt) > max_chars}


def write_dict_entry(case_dir: str, rel_path: str, key: str, value: str) -> dict:
    """Set a top-level dictionary entry, e.g. controlDict endTime/deltaT, or the
    turbulence 'model'. GATED — agent.py confirms with the user first."""
    p = Path(case_dir) / rel_path
    if not p.is_file():
        return {"ok": False, "error": f"not found: {p}"}
    old = foam.get_dict_entry(case_dir, rel_path, key)
    changed = foam.set_dict_entry(case_dir, rel_path, key, str(value))
    if not changed:
        return {"ok": False, "error": f"'{key}' not found as a top-level entry in {rel_path}"}
    # keep the registry's quick columns in sync for the common knobs
    if rel_path.endswith("controlDict") and key == "endTime":
        registry.update_run(_run_id_of(case_dir), end_time=_to_float(value))
    return {"ok": True, "path": str(p), "key": key, "old": old, "new": str(value)}


# ----------------------------------------------------------------- mesh/run
def run_mesh(case_dir: str, tool: str = "blockMesh") -> dict:
    """Generate the mesh. GATED. (blockMesh now; snappyHexMesh in Phase 3.)"""
    if tool not in ("blockMesh", "snappyHexMesh"):
        return {"ok": False, "error": f"unsupported mesh tool: {tool}"}
    r = foam.run_foam(tool, case=case_dir, timeout=900)
    return {"ok": r.ok, "tool": tool, "returncode": r.returncode,
            "duration_s": round(r.duration_s, 1), "log_tail": r.tail(700)}


def check_mesh(case_dir: str) -> dict:
    """Run checkMesh and return parsed quality metrics (cells, non-ortho, skew, OK?)."""
    r = foam.run_foam("checkMesh", case=case_dir, timeout=300)
    mq = foam.parse_checkmesh(r.stdout)
    if mq.get("cells"):
        registry.update_run(_run_id_of(case_dir), mesh_cells=mq["cells"])
    return {"ok": r.ok, "metrics": mq, "log_tail": r.tail(400)}


def generate_mesh(case_dir: str, geometry: str = "cylinder_2d", target_cells: int = 8000,
                  size_min: float = 0.3, size_max: float = 2.0, scaffold: bool = True,
                  stl: str = "", improve: bool = True, aoa: float = 0.0, chord: float = 1.0) -> dict:
    """Generate the mesh with gmsh → gmshToFoam → checkMesh (instead of snappy).
    geometry=cylinder_2d: 2D + caeloop validator-gated repair(heuristic→LLM), plus the full case via scaffold.
    geometry=external_stl: the STL/OBJ from fetch_geometry (e.g. motorBike) is watertight-repaired with
    pymeshfix→decimate→gmsh external volume. With improve=True a quality self-improve loop (skew>=4→Frontal,
    insufficient nonortho/sicn→faces↑) searches for the best result, and if the non-ortho limit of gmsh tets
    cannot be beaten the result carries snappy_suggested=True + snappy_reason to suggest switching to snappy. GATED."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"gmsh/caeloop import failed: {e}"}
    try:
        if geometry == "external_stl":
            if not stl:
                return {"ok": False, "error": "external_stl needs an stl path (the STL/OBJ from fetch_geometry)"}
            res = meshgen.generate_external(case_dir, stl, target_faces=20000, size_max=None,
                                            improve=bool(improve))
        elif geometry == "airfoil_2d":
            if not stl:
                return {"ok": False, "error": "airfoil_2d needs stl=<.dat path> (the fetch_geometry kind=airfoil result)"}
            sm = 0.01 if abs(size_min - 0.3) < 1e-9 else float(size_min)   # airfoil-scale default
            sx = 0.5 if abs(size_max - 2.0) < 1e-9 else float(size_max)
            res = meshgen.generate_airfoil(case_dir, stl, aoa=float(aoa), chord=float(chord),
                                           size_min=sm, size_max=sx, scaffold=bool(scaffold))
        elif geometry == "external_netgen":
            if not stl:
                return {"ok": False, "error": "external_netgen needs an stl path (clean watertight STL/OBJ)"}
            res = meshgen.generate_external_netgen(case_dir, stl, n_layers=3)
        else:
            res = meshgen.generate(case_dir, geometry=geometry, target_cells=int(target_cells),
                                   size_min=float(size_min), size_max=float(size_max),
                                   scaffold=bool(scaffold))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    rid = _run_id_of(case_dir)
    upd = {"geometry_src": f"gmsh:{geometry}", "status": "meshed"}
    if res.get("mesh_cells"):
        upd["mesh_cells"] = res["mesh_cells"]
    if res.get("scaffolded"):
        upd["solver"] = foam.get_dict_entry(case_dir, "system/controlDict", "solver")
        upd["turb_model"] = foam.get_dict_entry(case_dir, "constant/momentumTransport",
                                                "simulationType")
    try:
        registry.update_run(rid, **upd)
        if geometry == "external_stl":
            registry.merge_results(rid, mesh={k: res.get(k) for k in
                ("target_faces", "smooth", "algo", "max_non_ortho", "max_skewness",
                 "min_sicn", "mesh_cells", "stl_deviation", "snappy_suggested", "rounds")})
        else:
            registry.merge_results(rid, mesh={k: res.get(k) for k in
                ("size_min", "size_max", "n_elements", "min_sicn", "mesh_cells",
                 "max_non_ortho", "max_skewness", "attempts")})
    except Exception:
        pass
    return res


def add_boundary_layers(case_dir: str, n_layers: int = 3, first_layer: float = 0.0,
                        expansion: float = 1.25, wall_faces: int = 30000) -> dict:
    """Add wall prism boundary layers to a gmsh external mesh (external_stl) with snappyHexMesh
    addLayers-only. Run after generate_mesh(geometry=external_stl). gmsh cannot build 3D BLs, so this is a
    hybrid: gmsh captures the geometry, snappy adds only the layers — essential for wall accuracy (drag·Cf)
    in 3D external-flow RANS. Layer coverage is governed by wall resolution, so wall_faces (default 30000)
    remeshes the wall finer to raise coverage (20k→67%, 30k→78%). Thin features get partial coverage
    (see layer_coverage_pct in the result). GATED."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import failed: {e}"}
    try:
        res = meshgen.add_boundary_layers(case_dir, n_layers=int(n_layers),
                                          first_layer=(float(first_layer) or None),
                                          expansion=float(expansion), wall_faces=int(wall_faces))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    rid = _run_id_of(case_dir)
    try:
        registry.update_run(rid, status="layered",
                            **({"mesh_cells": res["cells_after"]} if res.get("cells_after") else {}))
        registry.merge_results(rid, bl={k: res.get(k) for k in
            ("layer_coverage_pct", "avg_layers", "n_layers_wanted", "first_layer",
             "expansion", "cells_after", "max_non_ortho", "max_skewness", "remeshed")})
    except Exception:
        pass
    return res


def scaffold_external_ras(case_dir: str, flow_velocity: float = 20.0, nu: float = 1.5e-5,
                          end_time: int = 500, nnonortho: int = 3,
                          turbulence: str = "kOmegaSST", residual_tol: float = 1e-4) -> dict:
    """Lay down an external-flow RANS setup (0/, constant/, system/ + forceCoeffs for drag) on an
    external_stl(+BL) case. lRef/Aref/CofR are derived automatically from the body bbox. Includes
    stabilisation for high non-ortho (limited schemes, nNonOrthogonalCorrectors, SIMPLE + pressure
    relaxation). **With residual_tol>0, residualControl stops automatically on convergence, before
    end_time (=cap)** (the OF12 drivaerFastback approach); with 0 it iterates to a fixed end_time. A good
    cap comes from find_similar_runs (converged_iters of past converged runs) or web_search. Run after
    add_boundary_layers, before run_solver. GATED."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import failed: {e}"}
    try:
        res = meshgen.scaffold_external_ras(case_dir, flow_velocity=float(flow_velocity),
                                            nu=float(nu), end_time=int(end_time),
                                            nnonortho=int(nnonortho), turbulence=str(turbulence),
                                            residual_tol=float(residual_tol))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if res.get("ok"):
        rid = _run_id_of(case_dir)
        try:
            registry.update_run(rid, solver=res.get("solver"), turb_model=res.get("turbulence"),
                                end_time=float(end_time), status="scaffolded")
            registry.merge_results(rid, ras={k: res.get(k) for k in
                ("flow_velocity", "nu", "k", "omega", "lRef", "Aref", "CofR",
                 "nNonOrthogonalCorrectors", "turbulence", "solver",
                 "residual_tol", "end_time_cap", "convergence")})
        except Exception:
            pass
    return res


def generate_propeller(case_dir: str, preset: str = "DTMB4119", diameter: float = 0.3048,
                       n_blades: int = 3, hub_ratio: float = 0.2, handed: int = 1,
                       area_ratio: float = 0.70, pitch_ratio: float = 1.0) -> dict:
    """Generate propeller geometry from a published offset table (CAD-free) → case/constant/geometry gets
    propeller.stl (blades + hub) + rotatingZone.stl (MRF cellZone cylinder). preset:
    **DTMB4119** (validation standard, Jessup 1989) or **B-series** (Wageningen parametric: generate a
    family via n_blades=Z, area_ratio=AE/A0, pitch_ratio=P/D, e.g. B4-70=Z4·AE0.70). Wrapped-section loft +
    hub. Then MRF via snappy(cellZone)+scaffold_mrf. handed=±1 sets the handedness (thrust direction). GATED."""
    try:
        import propgen
    except Exception as e:
        return {"ok": False, "error": f"propgen import failed: {e}"}
    out = str(Path(case_dir) / "constant" / "geometry")
    p = str(preset).upper().replace("-", "").replace("_", "")
    if p.startswith("DTMB"):
        table, nb = propgen.DTMB_4119, int(n_blades)
    elif p.startswith("B") or "WAGENINGEN" in p:       # Wageningen B-series
        table = propgen.wageningen_b_series(Z=int(n_blades), AE_A0=float(area_ratio),
                                            PD=float(pitch_ratio))
        nb = int(n_blades)
    else:
        table, nb = None, int(n_blades)
    try:
        res = propgen.generate_propeller(out, table=table, n_blades=nb,
                                         diameter=float(diameter), hub_ratio=float(hub_ratio),
                                         handed=int(handed))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if res.get("ok"):
        res["preset"] = preset
        if p.startswith("B") and not p.startswith("DTMB"):
            res["AE_A0"] = float(area_ratio); res["PD"] = float(pitch_ratio)
        try:
            registry.merge_results(_run_id_of(case_dir), propeller={k: res.get(k) for k in
                ("diameter", "n_blades", "hub_ratio", "handed", "watertight", "n_faces",
                 "preset", "AE_A0", "PD")})
        except Exception:
            pass
    return res


def scaffold_mrf(case_dir: str, cellzone: str = "innerCylinder", rpm: float = 1500.0,
                 axis: str = "0 1 0", origin: str = "0 0 0", flow_velocity: float = 5.0,
                 nu: float = 1e-6, turbulence: str = "kEpsilon", rotor_patches: str = "propeller.*",
                 residual_tol: float = 1e-4, end_time: int = 3000) -> dict:
    """rotating machinery (MRF): give the rotating cellZone a frame rotation via MRFProperties and solve
    with steady SIMPLE (instead of a rotating mesh). The forces functionObject records axial thrust·torque
    → power=τ·ω. The mesh must contain a rotating cellZone. After run_solver, read the performance with
    rotor_performance. GATED."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import failed: {e}"}

    def _vec(s, d):
        try:
            p = [float(x) for x in str(s).replace(",", " ").split()]
            return tuple(p) if len(p) == 3 else d
        except Exception:
            return d
    try:
        res = meshgen.scaffold_mrf(case_dir, cellzone=str(cellzone), axis=_vec(axis, (0, 1, 0)),
                                   origin=_vec(origin, (0, 0, 0)), rpm=float(rpm),
                                   flow_velocity=float(flow_velocity), nu=float(nu),
                                   turbulence=str(turbulence), rotor_patches=str(rotor_patches),
                                   residual_tol=float(residual_tol), end_time=int(end_time))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if res.get("ok"):
        rid = _run_id_of(case_dir)
        try:
            registry.update_run(rid, solver=res.get("solver"), turb_model=res.get("turbulence"),
                                end_time=float(end_time), status="mrf_scaffolded")
            registry.merge_results(rid, mrf={k: res.get(k) for k in
                ("method", "cellZone", "axis", "origin", "rpm", "omega_rad_s", "turbulence",
                 "rotor_patches", "nu", "flow_velocity", "residual_tol", "end_time_cap")})
        except Exception:
            pass
    return res


def rotor_performance(case_dir: str, axis: str = "", rpm: float = 0.0) -> dict:
    """Read MRF rotating-machinery performance from postProcessing/forces1 → axial thrust·torque·power(=τ·ω).
    If axis·rpm are omitted, the values scaffold_mrf recorded in the DB are used. Call after run_solver."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import failed: {e}"}
    rid = _run_id_of(case_dir)
    mrf = ((registry.get_run(rid) or {}).get("results") or {}).get("mrf") or {}

    def _vec(s, d):
        try:
            p = [float(x) for x in str(s).replace(",", " ").split()]
            return tuple(p) if len(p) == 3 else d
        except Exception:
            return d
    ax = _vec(axis, tuple(mrf.get("axis") or (0, 1, 0)))
    om = mrf.get("omega_rad_s")
    if rpm:
        om = float(rpm) * 2.0 * 3.141592653589793 / 60.0
    fc = meshgen.parse_forces(case_dir, axis=ax, omega_rad_s=om)
    if not fc:
        return {"ok": False, "error": "no postProcessing/forces1 — the forces functionObject did not run, or run_solver has not been called yet"}
    try:
        registry.merge_results(rid, rotor={k: fc.get(k) for k in
            ("thrust", "torque", "power_W", "force", "moment", "axis", "omega_rad_s")})
        upd = {}
        if fc.get("thrust") is not None:
            upd["thrust"] = fc["thrust"]
        if fc.get("power_W") is not None:
            upd["power_w"] = fc["power_W"]
        if upd:
            registry.update_run(rid, **upd)
    except Exception:
        pass
    fc["ok"] = True
    return fc


def rotor_report(case_dir: str) -> dict:
    """Build a **visual diagnostic report for human judgement** of a rotating-machinery (MRF) case:
    ① geometry 3D, ② blade cross-section (is the airfoil shape correct?), ③ flow direction vs axis alignment,
    ④ rotation/thrust/torque direction schematic, ⑤ performance·convergence, ⑥ human-judgment checklist.
    Writes PNG + md into case/report/. Rotating machinery is hard to judge from numbers alone, so a human
    has to eyeball the geometry and directions (especially because thrust is a small net force of lift−drag,
    so its sign is easily wrong)."""
    try:
        import rotordiag
    except Exception as e:
        return {"ok": False, "error": f"rotordiag import failed (needs matplotlib/trimesh): {e}"}
    try:
        return rotordiag.make_rotor_report(case_dir)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def run_snappy_mesh(case_dir: str, refine_min: int = 2, refine_max: int = 4,
                    n_layers: int = 3, nprocs: int = 0, stl: str = "") -> dict:
    """Arbitrary STL → hex-dominant + prism BL mesh via full snappyHexMesh (castellate+snap+addLayers),
    in parallel. For quantitative external flow (drag) — better quality than gmsh-tet, and it survives
    dirty STLs too because it ray-casts. If stl is omitted it is auto-detected in case/constant/triSurface
    or case/geometry (the fetch_geometry output). Then scaffold_external_ras → run_solver for drag. GATED."""
    import glob
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import failed: {e}"}
    s = stl
    if not s:
        for pat in ("constant/triSurface/*", "geometry/*", "*.stl", "*.obj"):
            hits = [h for h in glob.glob(str(Path(case_dir) / pat))
                    if h.lower().endswith((".stl", ".obj"))]
            if hits:
                s = hits[0]
                break
    if not s:
        return {"ok": False, "error": "snappy needs an STL (pass stl= or run fetch_geometry first)"}
    try:
        res = meshgen.run_snappy_external(case_dir, s, refine=(int(refine_min), int(refine_max)),
                                          n_layers=int(n_layers), nprocs=(int(nprocs) or None))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    rid = _run_id_of(case_dir)
    try:
        registry.update_run(rid, status="snappy_meshed",
                            **({"mesh_cells": res["mesh_cells"]} if res.get("mesh_cells") else {}))
        registry.merge_results(rid, snappy={k: res.get(k) for k in
            ("method", "refine", "n_layers", "mesh_cells", "max_non_ortho", "max_skewness", "mesh_ok")})
    except Exception:
        pass
    return res


def _cell_count(case_dir: str) -> int | None:
    rec = registry.get_run(_run_id_of(case_dir)) or {}
    if rec.get("mesh_cells"):
        return rec["mesh_cells"]
    cm = Path(case_dir) / "log.checkMesh"
    if cm.is_file():
        return foam.parse_checkmesh(cm.read_text()).get("cells")
    return None


def run_solver(case_dir: str, timeout: int = 1800, nprocs: int | None = None,
               background: bool = True) -> dict:
    """Run foamRun, auto-parallelising via MPI when the mesh is large enough. **background=True (default):
    the solver is thrown into the background and the job_id returned immediately** — the chat does not
    block, it keeps running even if you exit, check progress with `job_status` / stop it with `stop_job`
    (the last checkpoint is preserved → resume from latestTime). background=False blocks to the end
    (for small cases/scripts). nprocs=None -> auto. GATED. Auto-records."""
    run_id = _run_id_of(case_dir)
    cells = _cell_count(case_dir)
    n = int(nprocs) if nprocs else foam.decide_nprocs(cells)
    mode = "serial" if n <= 1 else f"parallel({n})"
    if background:
        if n <= 1:
            bg = foam.run_foam_bg([("foamRun", [])], case=case_dir, log_name="foamRun")
        else:
            foam.write_decompose_par_dict(case_dir, n)
            bg = foam.run_foam_bg([("decomposePar", ["-force"]),
                                   ("mpirun", ["-np", str(n), "foamRun", "-parallel"]),
                                   ("reconstructPar", [])], case=case_dir, log_name="foamRun")
        if not bg.get("ok"):
            return {"ok": False, "error": bg.get("error"), "mode": mode}
        _job_add(run_id, pid=bg["pid"], log=bg["log"], mode=mode, case=str(case_dir),
                 kind="solver", started=_time.strftime("%Y-%m-%d %H:%M:%S"), status="running")
        try:
            registry.update_run(run_id, status="running_bg")
        except Exception:
            pass
        return {"ok": True, "background": True, "job_id": run_id, "pid": bg["pid"], "mode": mode,
                "status": "running", "log": bg["log"], "cells": cells,
                "note": f"Background run started ({mode}). Check progress with job_status('{run_id}'), "
                        f"stop it with stop_job('{run_id}'). It keeps running even if you exit."}
    if n <= 1:
        r = foam.run_foam("foamRun", case=case_dir, timeout=int(timeout))
        mode = "serial"
    else:
        foam.write_decompose_par_dict(case_dir, n)
        r = foam.run_parallel(case_dir, n, int(timeout))
        mode = f"parallel({n})"
    sl = foam.parse_solver_log(r.stdout)
    times = foam.list_time_dirs(case_dir)
    ran_ok = bool(r.ok and not sl["diverged"] and times)
    try:
        import meshgen
    except Exception:
        meshgen = None
    # residualControl-aware convergence: converged if it stopped before the cap (endTime) or a 'solution converged' message is present.
    # Distinct from merely 'it ran' (ran_ok) — converged_iters is the key number for setting the next run's cap from the DB/web.
    conv = {}
    try:
        if meshgen is not None:
            conv = meshgen.parse_convergence(case_dir, log_text=r.stdout) or {}
    except Exception:
        conv = {}
    # residualControl convergence is cross-checked against OF's explicit message + a clean exit (r.ok) + foam's
    # divergence verdict (incl. high-Courant/NaN). Only this stops a timeout/kill/divergence from masquerading as 'converged' and polluting converged_iters in the DB.
    residual_converged = bool(conv.get("converged")) and r.ok and not sl["diverged"]
    conv_iters = conv.get("converged_iters") if residual_converged else None
    cap = conv.get("end_time_cap")
    final_t = conv.get("final_time")
    hit_cap = bool(cap is not None and final_t is not None and final_t >= cap
                   and not residual_converged and not sl["diverged"])
    # Stopped below the cap with neither a convergence message nor divergence → suspect timeout/kill (the agent should read the log)
    early_suspect = bool(conv.get("stopped_early") and not residual_converged and not sl["diverged"])
    res = {"ok": r.ok and not sl["diverged"], "ran_ok": ran_ok, "returncode": r.returncode,
           "mode": mode, "nprocs": n, "cells": cells,
           "duration_s": round(r.duration_s, 1), "last_time": sl["last_time"],
           "residual_converged": residual_converged, "converged_iters": conv_iters,
           "end_time_cap": cap, "hit_cap_unconverged": hit_cap,
           "stopped_early_suspect": early_suspect, "max_courant": sl["max_courant"],
           "diverged": sl["diverged"], "last_residuals": conv.get("last_residuals"),
           "time_dirs": times, "log_tail": r.tail(700)}
    # drag/lift from a forceCoeffs functionObject, if one ran
    fc = None
    try:
        if meshgen is not None:
            fc = meshgen.parse_force_coeffs(case_dir)
    except Exception:
        fc = None
    # The 'converged' column keeps its coarse meaning of 'did it run to the end without diverging' (ran_ok; transient included).
    # The precise residualControl convergence / converged_iters are recorded only in the solve JSON (no fakes — cross-checked above).
    upd = {"wall_time": round(r.duration_s, 1), "converged": ran_ok, "status": "run"}
    if fc and fc.get("Cd") is not None:
        upd["cd"] = fc["Cd"]
    if fc and fc.get("Cl") is not None:
        upd["cl"] = fc["Cl"]
    registry.update_run(run_id, **upd)
    registry.merge_results(run_id, solve={
        "ran_ok": ran_ok, "residual_converged": residual_converged, "converged_iters": conv_iters,
        "end_time_cap": cap, "hit_cap_unconverged": hit_cap, "stopped_early_suspect": early_suspect,
        "diverged": sl["diverged"], "last_time": sl["last_time"],
        "last_residuals": conv.get("last_residuals"),
        "max_courant": sl["max_courant"], "mode": mode, "nprocs": n,
        "cd": (fc or {}).get("Cd"), "cl": (fc or {}).get("Cl")})
    if fc:
        res["forceCoeffs"] = fc
    return res


def read_log(case_dir: str, which: str = "foamRun", tail: int = 1500) -> dict:
    p = Path(case_dir) / f"log.{which}"
    if not p.is_file():
        return {"ok": False, "error": f"no log: {p}"}
    txt = p.read_text()
    return {"ok": True, "path": str(p), "tail": txt[-tail:]}


# ----------------------------------------------------------------- postproc
def build_report(case_dir: str) -> dict:
    """Post-processing/report: writes a residual convergence plot + velocity field + REPORT.md into case/report/."""
    try:
        import postproc
    except Exception as e:
        return {"ok": False, "error": f"postproc import failed (needs matplotlib in the venv): {e}"}
    return postproc.build_report(case_dir)


# ----------------------------------------------------------------- files / excel
def write_file(path: str, content: str) -> dict:
    """Create/overwrite an arbitrary text file (restricted to the case/reports folders). GATED."""
    dest = _confined(path)
    if dest is None:
        return {"ok": False, "error": f"outside the allowed paths (only the case folder or reports): {path}"}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        existed = dest.is_file()
        dest.write_text(content)
        return {"ok": True, "path": str(dest), "overwritten": existed,
                "bytes": len(content.encode("utf-8"))}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def build_excel(path: str, sheets: list) -> dict:
    """Build an Excel (.xlsx) file: per-sheet tables (columns/rows) + embedded images (png paths).
    Both path and the embedded image paths are restricted to the case/reports folders."""
    dest = _confined(path)
    if dest is None:
        return {"ok": False, "error": f"outside the allowed paths: {path}"}
    if dest.suffix.lower() not in (".xlsx", ".xlsm"):
        return {"ok": False, "error": "path must be .xlsx"}
    try:
        import xlsx
    except Exception as e:
        return {"ok": False, "error": f"openpyxl required: {e}"}
    try:
        return xlsx.build(dest, sheets, confine=_confined)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ----------------------------------------------------------------- registry
def list_runs(limit: int = 15, case_kind: str = "") -> dict:
    rows = registry.list_runs(limit=limit, case_kind=case_kind or None)
    return {"ok": True, "count": len(rows), "runs": rows}


def get_run(run_id: str) -> dict:
    row = registry.get_run(run_id)
    return {"ok": row is not None, "run": row} if row else {"ok": False, "error": "no such run"}


def find_similar_runs(case_kind: str = "", solver: str = "", turb_model: str = "",
                      converged_only: bool = True, limit: int = 10) -> dict:
    rows = registry.find_similar_runs(case_kind=case_kind or None,
                                      solver=solver or None,
                                      turb_model=turb_model or None,
                                      converged_only=converged_only, limit=limit)
    return {"ok": True, "count": len(rows), "runs": rows}


# ----------------------------------------------------------------- helpers
def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------- dispatch + schema
def delete_path(path: str, recursive: bool = False) -> dict:
    """Delete a file/folder. For safety only paths *inside* RUNS_ROOT·REPORTS_ROOT are allowed (_confined
    rejects '..', symlinks and outside paths), and the data roots themselves or system/project sources are
    blocked. Folders need recursive=True to delete their contents too. For cleaning up failed meshes, stale
    time/processor dirs and abandoned cases. Irreversible — GATED (approval required)."""
    import shutil
    rp = _confined(path)
    if rp is None:
        return {"ok": False, "error": f"refused: '{path}' is outside the allowed roots (RUNS_ROOT·REPORTS_ROOT) or cannot be resolved"}
    roots = {Path(config.RUNS_ROOT).resolve(), Path(config.REPORTS_ROOT).resolve()}
    if rp in roots:
        return {"ok": False, "error": f"refused: the data root itself cannot be deleted — only items *inside* it ({rp})"}
    if not rp.exists():
        return {"ok": False, "error": f"no such path: {rp}"}
    try:
        if rp.is_dir():
            files = [f for f in rp.rglob("*") if f.is_file()]
            nfiles = len(files)
            total = sum(f.stat().st_size for f in files if f.exists())
            if (nfiles > 0 or any(rp.iterdir())) and not recursive:
                return {"ok": False, "files": nfiles,
                        "error": f"folder is not empty ({nfiles} files) — use recursive=True to delete its contents too"}
            shutil.rmtree(rp)
            return {"ok": True, "deleted": str(rp), "type": "dir",
                    "files_removed": nfiles, "bytes_removed": total}
        size = rp.stat().st_size
        rp.unlink()
        return {"ok": True, "deleted": str(rp), "type": "file", "bytes_removed": size}
    except Exception as e:
        return {"ok": False, "error": f"delete failed: {type(e).__name__}: {e}"}


DISPATCH = {
    "list_tutorials": list_tutorials,
    "copy_case": copy_case,
    "new_case": new_case,
    "fetch_geometry": fetch_geometry,
    "read_dict": read_dict,
    "write_dict_entry": write_dict_entry,
    "run_mesh": run_mesh,
    "check_mesh": check_mesh,
    "generate_mesh": generate_mesh,
    "add_boundary_layers": add_boundary_layers,
    "scaffold_external_ras": scaffold_external_ras,
    "generate_propeller": generate_propeller,
    "scaffold_mrf": scaffold_mrf,
    "rotor_performance": rotor_performance,
    "rotor_report": rotor_report,
    "run_snappy_mesh": run_snappy_mesh,
    "run_solver": run_solver,
    "read_log": read_log,
    "build_report": build_report,
    "write_file": write_file,
    "build_excel": build_excel,
    "list_runs": list_runs,
    "get_run": get_run,
    "find_similar_runs": find_similar_runs,
    "delete_path": delete_path,
}


def _obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or []}


TOOL_SPECS = [
    {"name": "list_tutorials",
     "description": "List the installed OF12 tutorial cases (directories that contain system/controlDict), optionally filtered by a substring. The basis for picking a starting case and for checking correct OF12 syntax.",
     "input_schema": _obj({
         "filter": {"type": "string", "description": "path substring (e.g. 'cavity', 'incompressibleFluid')"},
         "limit": {"type": "integer"}})},
    {"name": "copy_case",
     "description": "Copy a tutorial case into a new run folder (RUNS_ROOT/<run_id>) and register it in the registry. Returns case_dir and run_id.",
     "input_schema": _obj({
         "tutorial": {"type": "string", "description": "path relative to $FOAM_TUTORIALS (e.g. 'incompressibleFluid/cavity')"},
         "run_name": {"type": "string", "description": "optional: case_kind label"}}, ["tutorial"])},
    {"name": "new_case",
     "description": "Create a new empty case folder + run_id for non-tutorial, from-scratch geometry (gmsh etc). Fill it in afterwards with generate_mesh(case_dir). The from-scratch counterpart of copy_case.",
     "input_schema": _obj({"name": {"type": "string", "description": "case_kind label (e.g. cylinder_2d)"}})},
    {"name": "fetch_geometry",
     "description": "Download geometry from the internet without CAD (curated, login-free sources) into case/geometry/. kind=motorbike (OpenFOAM motorBike.obj) | airfoil (name required, e.g. naca0012 — UIUC .dat) | url (direct URL to .stl/.obj/.dat). [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "kind": {"type": "string", "enum": ["motorbike", "airfoil", "url"]},
         "name": {"type": "string", "description": "name when kind=airfoil (e.g. naca0012)"},
         "url": {"type": "string", "description": "direct URL when kind=url (.stl/.obj/.dat etc)"}},
         ["case_dir", "kind"])},
    {"name": "read_dict",
     "description": "Read a dict file inside the case (e.g. system/controlDict, constant/momentumTransport, 0/U) to inspect the current settings.",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "rel_path": {"type": "string", "description": "path relative to the case"},
         "max_chars": {"type": "integer"}}, ["case_dir", "rel_path"])},
    {"name": "write_dict_entry",
     "description": "Change a top-level dict entry (e.g. controlDict endTime/deltaT/writeInterval, momentumTransport model). [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "rel_path": {"type": "string"},
         "key": {"type": "string"},
         "value": {"type": "string"}}, ["case_dir", "rel_path", "key", "value"])},
    {"name": "run_mesh",
     "description": "Generate the mesh (blockMesh or snappyHexMesh). [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "tool": {"type": "string", "enum": ["blockMesh", "snappyHexMesh"]}}, ["case_dir"])},
    {"name": "check_mesh",
     "description": "Run checkMesh and return the parsed quality metrics (cells, max non-ortho, max skewness, Mesh OK).",
     "input_schema": _obj({"case_dir": {"type": "string"}}, ["case_dir"])},
    {"name": "generate_mesh",
     "description": "Generate a geometry mesh with gmsh (a robust path instead of snappy). caeloop validator-gated repair: a quality/element-count validator decides, and on failure a heuristic→LLM fixes the size and retries automatically. On pass: gmshToFoam→OpenFOAM polyMesh→front/back empty (2D)→checkMesh. Use it for external/curved geometry that blockMesh cannot handle. [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "geometry": {"type": "string", "enum": ["cylinder_2d", "airfoil_2d", "external_stl", "external_netgen"],
                      "description": "cylinder_2d · airfoil_2d(.dat) · external_stl(gmsh+snappy BL, dirty OK) · external_netgen(OCP+netgen prism BL, for **clean watertight geometry**)"},
         "target_cells": {"type": "integer", "description": "target cell count for cylinder_2d (default 8000)"},
         "size_min": {"type": "number", "description": "initial mesh size near the geometry"},
         "size_max": {"type": "number", "description": "initial mesh size in the far field"},
         "scaffold": {"type": "boolean", "description": "also lay down a solvable case template (cylinder_2d, default true)"},
         "stl": {"type": "string", "description": "STL/OBJ path when external_stl (the fetch_geometry result)"},
         "improve": {"type": "boolean", "description": "external_stl quality self-improve loop on/off (default true). On hitting the limit the result carries snappy_suggested"},
         "aoa": {"type": "number", "description": "airfoil_2d angle of attack (deg, default 0)"},
         "chord": {"type": "number", "description": "airfoil_2d chord length (default 1.0)"}},
         ["case_dir"])},
    {"name": "add_boundary_layers",
     "description": "Add wall prism boundary layers to a gmsh external mesh (external_stl) with snappyHexMesh addLayers-only. Essential for wall accuracy (drag·Cf) in 3D external-flow RANS (meaningless without a BL). A hybrid: gmsh does the geometry, snappy adds only the layers. Run after generate_mesh(external_stl). Thin features get partial coverage (see layer_coverage_pct·avg_layers in the result). [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "n_layers": {"type": "integer", "description": "target number of prism layers (default 3)"},
         "first_layer": {"type": "number", "description": "first layer thickness (absolute; 0 = auto, 0.12% of the body diagonal)"},
         "expansion": {"type": "number", "description": "layer expansion ratio (default 1.25)"},
         "wall_faces": {"type": "integer", "description": "face count for wall remeshing (default 30000; raises coverage, 0 = use the existing mesh)"}},
         ["case_dir"])},
    {"name": "run_snappy_mesh",
     "description": "Arbitrary STL → hex-dominant + prism BL mesh via full snappyHexMesh (castellate+snap+addLayers), in parallel. For quantitative external-flow drag — better quality than gmsh-tet, and it survives dirty STLs (motorBike) too because it ray-casts. Run after fetching an STL with fetch_geometry → scaffold_external_ras → run_solver for drag. Resulting patches: inlet/outlet/bottom/farfield/body(wall). [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "stl": {"type": "string", "description": "STL/OBJ path (auto-detected in the case if omitted)"},
         "refine_min": {"type": "integer", "description": "snappy minimum surface refinement level (default 2)"},
         "refine_max": {"type": "integer", "description": "snappy maximum surface refinement level (default 4)"},
         "n_layers": {"type": "integer", "description": "number of prism layers (default 3)"},
         "nprocs": {"type": "integer", "description": "number of MPI processes (0 = auto)"}},
         ["case_dir"])},
    {"name": "scaffold_external_ras",
     "description": "Lay down a kOmegaSST external-flow RANS setup (0/·constant/·system/ + forceCoeffs to measure drag) on an external_stl(+BL) case. lRef/Aref/CofR automatic from the body bbox, includes stabilisation for high non-ortho meshes (limited schemes·nNonOrthogonalCorrectors·SIMPLE + pressure relaxation). After add_boundary_layers → run_solver (MPI automatic) → build_report. [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "flow_velocity": {"type": "number", "description": "free-stream velocity U (m/s, default 20). Re=U·L/nu"},
         "nu": {"type": "number", "description": "kinematic viscosity (default 1.5e-5, air). Sets Re together with U"},
         "turbulence": {"type": "string", "enum": ["kOmegaSST", "SpalartAllmaras", "kEpsilon", "laminar"],
                        "description": "turbulence model (default kOmegaSST). Per-model fields·BCs·wall functions are automatic"},
         "end_time": {"type": "integer", "description": "**upper limit (cap)** on SIMPLE iterations, default 500. With residual_tol>0 it stops automatically before that on convergence. A good cap comes from find_similar_runs (converged_iters of past converged runs)·web_search"},
         "residual_tol": {"type": "number", "description": "convergence threshold (default 1e-4). residualControl stops automatically once the initial residuals of p·U·turbulence fields are all below it. 0 = iterate to a fixed end_time (the old way)"},
         "nnonortho": {"type": "integer", "description": "nNonOrthogonalCorrectors (default 3; for high non-ortho meshes)"}},
         ["case_dir"])},
    {"name": "run_solver",
     "description": "Run foamRun. For large meshes it parallelises with MPI automatically based on the cell count (1~14 ranks: decomposePar scotch→mpirun→reconstructPar). Uses the solver from controlDict. The results (mode, nprocs, last_time, maxCo, diverged) are recorded automatically. [approval required] Check endTime before a long run, and start short.",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "timeout": {"type": "integer", "description": "seconds (default 1800)"},
         "nprocs": {"type": "integer", "description": "force the parallel rank count (omit = automatic from the cell count)"}}, ["case_dir"])},
    {"name": "read_log",
     "description": "Return the tail of the case's log.<which> file (for debugging).",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "which": {"type": "string", "description": "e.g. foamRun, blockMesh, checkMesh"},
         "tail": {"type": "integer"}}, ["case_dir"])},
    {"name": "build_report",
     "description": "Post-processing/report: writes a residual convergence plot from the solver log + velocity field (tricontourf) + REPORT.md into case/report/. Call after run_solver.",
     "input_schema": _obj({"case_dir": {"type": "string"}}, ["case_dir"])},
    {"name": "write_file",
     "description": "Create/overwrite an arbitrary text file. Use it to write new OF dicts (snappyHexMeshDict, fvModels, 0/ fields etc), notes, or custom scripts. The path is only allowed inside the case folder (under run_id) or reports. [approval required]",
     "input_schema": _obj({
         "path": {"type": "string", "description": "path of the file to write (inside the case/reports)"},
         "content": {"type": "string"}}, ["path", "content"])},
    {"name": "build_excel",
     "description": "Build an Excel (.xlsx) file — put tables and images on each sheet. E.g. a Reynolds-sweep comparison table + the velocity.png of each run embedded. The path and the image paths must be inside the case/reports folders.",
     "input_schema": _obj({
         "path": {"type": "string", "description": ".xlsx path (inside reports recommended)"},
         "sheets": {"type": "array", "description": "list of sheets", "items": {
             "type": "object",
             "properties": {
                 "name": {"type": "string"},
                 "tables": {"type": "array", "items": {"type": "object", "properties": {
                     "title": {"type": "string"},
                     "columns": {"type": "array", "items": {"type": "string"}},
                     "rows": {"type": "array", "items": {"type": "array"},
                              "description": "list of rows; each row is an array of cell values"}}}},
                 "images": {"type": "array", "items": {"type": "string"},
                            "description": "png/jpg paths to embed (inside the case/reports)"}}}}},
                          ["path", "sheets"])},
    {"name": "list_runs",
     "description": "Return the list of recent runs (settings·results).",
     "input_schema": _obj({"limit": {"type": "integer"}, "case_kind": {"type": "string"}})},
    {"name": "get_run",
     "description": "Look up the stored settings·results of a single run by run_id.",
     "input_schema": _obj({"run_id": {"type": "string"}}, ["run_id"])},
    {"name": "find_similar_runs",
     "description": "Search past runs by case_kind/solver/turb_model (converged ones only by default). Use it as the basis for proposing new settings.",
     "input_schema": _obj({
         "case_kind": {"type": "string"},
         "solver": {"type": "string"},
         "turb_model": {"type": "string"},
         "converged_only": {"type": "boolean"},
         "limit": {"type": "integer"}})},
    {"name": "delete_path",
     "description": "Delete a file or folder (for cleaning up failed meshes·stale time/processor dirs·abandoned cases). For safety only paths inside RUNS_ROOT·REPORTS_ROOT are allowed; the data roots themselves·system·project sources are refused. Folders need recursive=True to delete their contents too. Irreversible. [approval required]",
     "input_schema": _obj({
         "path": {"type": "string", "description": "file/folder to delete (inside RUNS_ROOT·REPORTS_ROOT)"},
         "recursive": {"type": "boolean", "description": "whether to delete the folder's contents too (required for non-empty folders)"}},
         ["path"])},
    {"name": "generate_propeller",
     "description": "CAD-free propeller geometry generation: lofts wrapped sections from an offset table and writes propeller.stl + rotatingZone.stl (MRF cellZone) into case/constant/geometry. preset=**DTMB4119** (validation standard, Jessup 1989) or **B-series** (Wageningen parametric — generate a propeller family via n_blades=Z·area_ratio=AE/A0·pitch_ratio=P/D). Then snappy(cellZone)→scaffold_mrf→run_solver→rotor_performance. handed=±1 sets the thrust direction (verify with rotor_report). [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "preset": {"type": "string", "description": "DTMB4119 (default) or B-series (Wageningen)"},
         "diameter": {"type": "number", "description": "diameter in m (default 0.3048)"},
         "n_blades": {"type": "integer", "description": "blade count Z (default 3; DTMB is 3, B-series 2~7)"},
         "hub_ratio": {"type": "number", "description": "hub/radius ratio (default 0.2)"},
         "handed": {"type": "integer", "description": "handedness ±1 (thrust direction)"},
         "area_ratio": {"type": "number", "description": "B-series expanded area ratio AE/A0 (default 0.70, 0.3~1.05)"},
         "pitch_ratio": {"type": "number", "description": "B-series pitch ratio P/D (default 1.0, 0.5~1.4)"}},
         ["case_dir"])},
    {"name": "scaffold_mrf",
     "description": "rotating machinery (MRF) setup: give the rotating cellZone a frame rotation via MRFProperties and solve thrust·torque with steady SIMPLE (instead of a rotating mesh — cheap and fast). The mesh must contain a rotating cellZone (created via snappy refinementSurfaces faceZone/cellZone). The forces functionObject records axial thrust·torque·power, and residualControl stops automatically on convergence. After meshing → run_solver → rotor_performance. [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "cellzone": {"type": "string", "description": "name of the rotating-region cellZone (default innerCylinder)"},
         "rpm": {"type": "number", "description": "rotation rate in RPM (default 1500). omega=rpm·2π/60"},
         "axis": {"type": "string", "description": "rotation·thrust axis vector, e.g. '0 1 0' (default)"},
         "origin": {"type": "string", "description": "centre of rotation, e.g. '0 0 0' (default)"},
         "rotor_patches": {"type": "string", "description": "regex for the rotating wall patches (default 'propeller.*')"},
         "turbulence": {"type": "string", "enum": ["kEpsilon", "kOmegaSST", "SpalartAllmaras", "laminar"],
                        "description": "turbulence model (default kEpsilon)"},
         "flow_velocity": {"type": "number", "description": "inflow velocity (for the record, default 5)"},
         "nu": {"type": "number", "description": "kinematic viscosity (default 1e-6 = water; air is 1.5e-5)"},
         "residual_tol": {"type": "number", "description": "convergence threshold (default 1e-4)"},
         "end_time": {"type": "integer", "description": "upper limit (cap) on SIMPLE iterations (default 3000)"}},
         ["case_dir"])},
    {"name": "rotor_performance",
     "description": "Read MRF rotating-machinery performance from postProcessing/forces1 → axial thrust·torque·power(=τ·ω). If axis·rpm are omitted, the values scaffold_mrf recorded in the DB are used. Call after run_solver; the basis for comparison against the literature.",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "axis": {"type": "string", "description": "optional: axis vector (DB value if omitted)"},
         "rpm": {"type": "number", "description": "optional: RPM (DB value if omitted)"}}, ["case_dir"])},
    {"name": "rotor_report",
     "description": "A **visual diagnostic report for human judgement** (PNG+md) of a rotating-machinery (MRF) case: geometry 3D · blade cross-section (airfoil shape) · flow direction vs axis · rotation/thrust/torque directions · performance/convergence · human-judgment checklist. Rotating machinery is hard to judge from numbers alone (thrust = a small net force of lift−drag), so a human has to look at the geometry and directions. Use it to show a human the result after mesh/solve.",
     "input_schema": _obj({"case_dir": {"type": "string"}}, ["case_dir"])},
]

# ===========================================================================
# Self-evolution tools (selfevolve.py engine). The agent can extend/modify its
# own code with the same discipline used across my agentic systems: snapshot -> validate
# (canary) -> diff -> apply. Plugin edits hot-reload; kernel edits need restart.
# ===========================================================================
import sys as _sys

import selfevolve


def list_plugins() -> dict:
    """List the currently loaded hot-reload plugin tools (tools_plugins/tool_*.py)."""
    return {"ok": True, "plugins": sorted(selfevolve._PLUGIN_NAMES),
            "files": [p.name for p in selfevolve.discover_plugins()]}


def reload_plugins() -> dict:
    """Rescan tools_plugins/ and hot-reload the tools (no core-loop restart needed)."""
    return selfevolve.reload_plugins()


def run_self_tests(level: str = "fast") -> dict:
    """canary regression tests (syntax · tool-registry consistency · physics-validation propeller maths). level='fast' (~1s) | 'full'."""
    return selfevolve.canary(level)


def propose_self_edit(filename: str, old: str = "", new: str = "", create: bool = False) -> dict:
    """Propose a self-edit: apply it in the incubator (an isolated full copy) → canary → return diff/validation (not actually applied). [approval required]"""
    return selfevolve.propose_edit(filename, old, new, create=create)


def apply_self_edit(edit_id: str, force: bool = False) -> dict:
    """Promote a proposed self-edit: snapshot (rollback point) → apply → hot-reload (plugin) / restart signal (kernel). [approval required]"""
    return selfevolve.apply_edit(edit_id, force=force)


def rollback_self(snapshot_id: str) -> dict:
    """Roll the source back to a snapshot (kernel files need a restart). [approval required]"""
    return selfevolve.rollback(snapshot_id)


def evolve_log(n: int = 20) -> dict:
    """Self-evolution history (snapshot/proposal/apply/rollback/canary results)."""
    return {"ok": True, "log": selfevolve.evolve_log(n), "snapshots": selfevolve.list_snapshots()[-10:]}


def set_auto_evolve(on: bool, budget: int = 8, goal: str = "") -> dict:
    """Turn autonomous evolution mode ON/OFF (budget = number of unapproved plugin edits allowed). Turning it on is itself [approval required]."""
    return selfevolve.set_auto(on, budget, goal or None)


def auto_evolve(filename: str = "", old: str = "", new: str = "", reason: str = "",
                create: bool = False) -> dict:
    """Autonomous self-edit (no human approval): **plugins only** · must pass canary · charged against the budget ·
    auto-discarded on failure · kernel refused. Requires set_auto_evolve(True) first. Works only in autonomous
    mode, and the engine enforces the safety fences."""
    return selfevolve.auto_evolve(filename, old, new, reason, create=create)


DISPATCH.update({
    "list_plugins": list_plugins, "reload_plugins": reload_plugins,
    "run_self_tests": run_self_tests, "propose_self_edit": propose_self_edit,
    "apply_self_edit": apply_self_edit, "rollback_self": rollback_self,
    "evolve_log": evolve_log, "set_auto_evolve": set_auto_evolve, "auto_evolve": auto_evolve,
})
# Human-gated: propose/apply/rollback and *enabling* autonomy. auto_evolve itself is
# ungated (that's the point of autonomy) but engine-fenced (plugins only, canary, budget).
GATED |= {"propose_self_edit", "apply_self_edit", "rollback_self", "set_auto_evolve"}
TOOL_SPECS.extend([
    {"name": "list_plugins",
     "description": "List the hot-reload plugin tools (tools_plugins/tool_*.py).",
     "input_schema": _obj({})},
    {"name": "reload_plugins",
     "description": "Rescan tools_plugins/ and hot-reload the plugin tools (available from the next turn, without restarting the core loop).",
     "input_schema": _obj({})},
    {"name": "run_self_tests",
     "description": "Self-validating canary regression tests: syntax (py_compile over everything) · tool-registry consistency · physics-validation propeller maths (B-series polynomials, DTMB table, watertight geometry, convergence parser). The oracle for checking safety before and after a self-edit. level='fast'|'full'.",
     "input_schema": _obj({"level": {"type": "string", "description": "fast (default) | full"}})},
    {"name": "propose_self_edit",
     "description": "**Propose a self-edit** — apply old→new in filename inside an isolated incubator (a full copy of the project), run canary there to validate, and return only the diff + results (the real code is not touched). create=True for a new file. If it passes, promote it with apply_self_edit. [approval required]",
     "input_schema": _obj({
         "filename": {"type": "string", "description": "e.g. tool_x.py (plugin) or propgen.py (module)"},
         "old": {"type": "string", "description": "the existing string to replace (unique within the file)"},
         "new": {"type": "string", "description": "the new string"},
         "create": {"type": "boolean", "description": "True when creating a new file (new is the whole content)"}}, ["filename"])},
    {"name": "apply_self_edit",
     "description": "**Promote (apply)** a staged change made by propose_self_edit: snapshot the current state (rollback point) → replace the file → hot-reload if it is a plugin / restart signal if it is a module. The incubator canary is re-checked (refused on failure; force=True to override). [approval required]",
     "input_schema": _obj({
         "edit_id": {"type": "string"},
         "force": {"type": "boolean", "description": "apply even if canary fails"}}, ["edit_id"])},
    {"name": "rollback_self",
     "description": "Revert the source code to a snapshot ID (kernel files take effect only after a restart). Check snapshots with evolve_log. [approval required]",
     "input_schema": _obj({"snapshot_id": {"type": "string"}}, ["snapshot_id"])},
    {"name": "evolve_log",
     "description": "Self-evolution history (snapshot·proposal·apply·rollback·canary) and the list of recent snapshots.",
     "input_schema": _obj({"n": {"type": "integer"}})},
    {"name": "set_auto_evolve",
     "description": "**Toggle autonomous evolution mode**. With on=True the agent may fix plugins by itself up to `budget` times without human approval (only edits that pass canary). Turning it on is itself dangerous, hence [approval required].",
     "input_schema": _obj({
         "on": {"type": "boolean"},
         "budget": {"type": "integer", "description": "number of unapproved edits allowed (default 8)"},
         "goal": {"type": "string", "description": "note on the evolution goal"}}, ["on"])},
    {"name": "auto_evolve",
     "description": "**Autonomous self-edit** (without the human approval loop) — but with fences the engine enforces: ① plugins (tool_*.py) only ② applied only if canary passes (auto-discarded on failure) ③ charged against the session budget ④ kernel/PROTECTED refused ⑤ everything logged. Requires set_auto_evolve(True) first. For self-healing/improvement.",
     "input_schema": _obj({
         "filename": {"type": "string", "description": "a tool_*.py in tools_plugins"},
         "old": {"type": "string"}, "new": {"type": "string"},
         "reason": {"type": "string", "description": "why it is being fixed (log/audit)"},
         "create": {"type": "boolean"}}, ["filename", "reason"])},
])

# Load any hot-reloadable plugins into the registry above (no-op if none yet).
selfevolve.attach(_sys.modules[__name__])


# ===========================================================================
# multiRegion CHT (conjugate heat transfer): region split + foamMultiRun. The OF12
# tutorials live under multiRegion/CHT/ — copy_case one as a starting point, then run_cht.
# ===========================================================================
import re as _re


def _cht_regions(case_dir) -> list:
    const = Path(case_dir) / "constant"
    if not const.is_dir():
        return []
    return sorted(d.name for d in const.iterdir() if d.is_dir() and (d / "polyMesh").is_dir())


def _region_field_stats(case_dir, time, region, field) -> dict | None:
    p = Path(case_dir) / str(time) / region / field
    if not p.is_file():
        return None
    t = p.read_text()
    m = _re.search(r"internalField\s+nonuniform[^(]*\((.*?)\)\s*;", t, _re.S)
    if m:
        vals = [float(x) for x in m.group(1).split()]
        if not vals:
            return None
        return {"min": round(min(vals), 2), "max": round(max(vals), 2),
                "mean": round(sum(vals) / len(vals), 2), "n": len(vals)}
    m = _re.search(r"internalField\s+uniform\s+([-\d.eE+]+)", t)
    if m:
        v = float(m.group(1))
        return {"min": v, "max": v, "mean": v, "n": 1}
    return None


def run_cht(case_dir: str, mesh_steps: list | None = None, setup_cht: bool = False,
            snappy: bool = False, split: bool = True, split_args: str = "-cellZones -overwrite",
            parallel: bool = False, nprocs: int | None = None,
            mesh_timeout: int = 3600, solve_timeout: int = 14400) -> dict:
    """Run a multiRegion **CHT (conjugate heat transfer)** case: mesh → region split → foamMultiRun. With
    setup_cht=True, foamSetupCHT (e.g. coolingSphere). With snappy=True, blockMesh+snappyHexMesh. With
    split=True, splitMeshRegions (split_args e.g. '-cellZones -overwrite' or '-cellZones -defaultRegionName
    fluid -overwrite'). With parallel=True, decomposePar -allRegions → mpirun foamMultiRun -parallel →
    reconstructPar -allRegions. Returns the region list · final time · completed/diverged · per-region T
    (min/max/mean). The standard way is to copy_case a multiRegion/CHT/* tutorial first. [approval required]"""
    cd = str(case_dir)

    def fail(stage, r):
        return {"ok": False, "stage": stage, "returncode": r.returncode, "log_tail": r.tail(900)}
    # mesh_steps gives full control as a list of [util, args] (e.g. coolingSphere needs
    # blockMesh -> topoSet -> splitMeshRegions -> foamSetupCHT). If omitted, build the common
    # pipeline from the flags. foamSetupCHT (if any) runs AFTER mesh+split, never instead.
    if mesh_steps is None:
        mesh_steps = [["blockMesh", ""]]
        if snappy:
            mesh_steps.append(["snappyHexMesh", "-overwrite"])
        if split:
            mesh_steps.append(["splitMeshRegions", split_args])
        if setup_cht:
            mesh_steps.append(["foamSetupCHT", ""])
    steps = []
    for util, uargs in mesh_steps:
        r = foam.run_foam(util, (uargs or "").split(), case=cd, timeout=mesh_timeout)
        steps.append([util, bool(r.ok)])
        if not r.ok:
            return fail(util, r)
    regions = _cht_regions(cd)
    if parallel:
        n = int(nprocs) if nprocs else foam.decide_nprocs(_cell_count(cd) or 100000)
        foam.run_foam("decomposePar", ["-allRegions", "-force"], case=cd, timeout=mesh_timeout)
        r = foam.run_foam("mpirun", ["-np", str(n), "foamMultiRun", "-parallel"], case=cd, timeout=solve_timeout)
        foam.run_foam("reconstructPar", ["-allRegions"], case=cd, timeout=mesh_timeout)
        mode = f"parallel({n})"
    else:
        r = foam.run_foam("foamMultiRun", case=cd, timeout=solve_timeout); mode = "serial"
    out = r.stdout or ""
    diverged = bool(_re.search(r"FOAM FATAL|::sigHandler|Floating point exception|Initial residual = (?:nan|inf)", out))
    completed = bool(_re.search(r"\n *End\b", out))
    times = foam.list_time_dirs(cd)
    final_t = times[-1] if times else None
    Tstats = {reg: _region_field_stats(cd, final_t, reg, "T") for reg in regions} if final_t else {}
    res = {"ok": bool(r.ok and not diverged), "mode": mode, "regions": regions, "steps": steps,
           "final_time": final_t, "completed": completed, "diverged": diverged,
           "T_per_region": Tstats, "duration_s": round(r.duration_s, 1), "log_tail": r.tail(900)}
    try:
        registry.update_run(_run_id_of(cd), status="cht_run",
                            converged=bool(completed and not diverged), wall_time=round(r.duration_s, 1))
        registry.merge_results(_run_id_of(cd), cht={"regions": regions, "final_time": final_t,
                               "completed": completed, "diverged": diverged, "mode": mode,
                               "T_per_region": Tstats})
    except Exception:
        pass
    return res


def run_foam_util(case_dir: str, util: str, args: str = "", timeout: int = 3600) -> dict:
    """Run a single OpenFOAM utility (only within ALLOWED_BINS). Use it to step through a multiRegion meshing
    chain (e.g. topoSet → createBaffles → extrudeToRegionMesh → splitMeshRegions). For circuitBoardCooling-style
    extrude CHT. [approval required]"""
    r = foam.run_foam(util, (args or "").split(), case=str(case_dir), timeout=int(timeout))
    return {"ok": bool(r.ok), "util": util, "returncode": r.returncode,
            "duration_s": round(r.duration_s, 1), "log_tail": r.tail(900)}


def cht_report(case_dir: str, out_png: str | None = None) -> dict:
    """**Visualise + quantify** multiRegion CHT results: per-region temperature field (2D tricontourf, cell-centre
    based) + T min/max/mean. Rendered after writeCellCentres (best for 2D cases; 3D is projected onto xy).
    Returns the PNG path and the per-region T statistics."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as _plt
    import numpy as _np
    cd = Path(case_dir)
    times = foam.list_time_dirs(str(cd))
    if not times:
        return {"ok": False, "error": "no result time directories — run_cht first"}
    t = times[-1]
    regions = _cht_regions(str(cd))

    def _vec(p):
        body = _re.search(r"internalField\s+nonuniform[^(]*\((.*?)\)\s*;", Path(p).read_text(), _re.S).group(1)
        return _np.array([[float(v) for v in s.split()] for s in _re.findall(r"\(([^()]*)\)", body)])

    def _sca(p):
        m = _re.search(r"internalField\s+nonuniform[^(]*\((.*?)\)\s*;", Path(p).read_text(), _re.S)
        return _np.array([float(x) for x in m.group(1).split()]) if m else None
    data, stats = {}, {}
    for reg in regions:
        cfile, tfile = cd / str(t) / reg / "C", cd / str(t) / reg / "T"
        if not tfile.is_file():
            continue
        if not cfile.is_file():
            foam.run_foam("postProcess", ["-region", reg, "-func", "writeCellCentres", "-latestTime"],
                          case=str(cd), timeout=600)
        if cfile.is_file() and tfile.is_file():
            C, T = _vec(cfile), _sca(tfile)
            if T is not None and len(C) == len(T):
                data[reg] = (C[:, 0], C[:, 1], T)
                stats[reg] = _region_field_stats(str(cd), t, reg, "T")
    if not data:
        return {"ok": False, "error": "failed to read the temperature field", "regions": regions}
    vmin = min(d[2].min() for d in data.values()); vmax = max(d[2].max() for d in data.values())
    fig, ax = _plt.subplots(figsize=(9, 6.5))
    lev = _np.linspace(vmin, vmax, 40)
    cf = None
    for reg, (x, y, T) in data.items():
        cf = ax.tricontourf(x, y, T, levels=lev, cmap="inferno", extend="both")
    ax.set_aspect("equal"); ax.set_xlabel("x [m]"); ax.set_ylabel("y [m]")
    ax.set_title(f"CHT temperature — {cd.name} @ t={t}\nregions: {', '.join(regions)}")
    fig.colorbar(cf, ax=ax, label="T [K]")
    out = Path(out_png) if out_png else (Path(config.REPORTS_ROOT) / f"{cd.name}_CHT_T.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    _plt.tight_layout(); _plt.savefig(out, dpi=110); _plt.close(fig)
    return {"ok": True, "png": str(out), "time": t, "regions": regions, "T_per_region": stats}


DISPATCH.update({"run_cht": run_cht, "run_foam_util": run_foam_util, "cht_report": cht_report})
GATED |= {"run_cht", "run_foam_util"}      # cht_report is read-only viz
TOOL_SPECS.extend([
    {"name": "run_cht",
     "description": "Run a multiRegion **CHT (conjugate heat transfer)** case: mesh→region split (splitMeshRegions)→**foamMultiRun**. Options: setup_cht (foamSetupCHT)·snappy·split·parallel (decomposePar/reconstructPar -allRegions). Returns regions·final time·completed/diverged·per-region T (min/max/mean). copy_case an OF12 multiRegion/CHT/* tutorial first, then run this. [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "mesh_steps": {"type": "array", "description": "full control of the meshing steps: [[util, args], ...] executed in order. Case-dependent. E.g. coolingSphere: [['blockMesh',''],['topoSet',''],['splitMeshRegions','-cellZones -defaultRegionName fluid -overwrite'],['foamSetupCHT','']]. If omitted, it is built from the flags below.", "items": {"type": "array"}},
         "setup_cht": {"type": "boolean", "description": "add foamSetupCHT after meshing (e.g. coolingSphere)"},
         "snappy": {"type": "boolean", "description": "blockMesh+snappyHexMesh (e.g. heatedDuct)"},
         "split": {"type": "boolean", "description": "run splitMeshRegions (default True)"},
         "split_args": {"type": "string", "description": "e.g. '-cellZones -overwrite' or '-cellZones -defaultRegionName fluid -overwrite'"},
         "parallel": {"type": "boolean", "description": "MPI parallel (beware over-decomposition on small meshes — setting nprocs explicitly is recommended)"},
         "nprocs": {"type": "integer"}}, ["case_dir"])},
    {"name": "run_foam_util",
     "description": "Run a single OF utility (within ALLOWED_BINS). Step through a multiRegion meshing chain (topoSet·createBaffles·extrudeToRegionMesh·splitMeshRegions) — for circuitBoardCooling-style extrude CHT. [approval required]",
     "input_schema": _obj({
         "case_dir": {"type": "string"}, "util": {"type": "string", "description": "e.g. topoSet, createBaffles, extrudeToRegionMesh"},
         "args": {"type": "string", "description": "utility arguments (space separated)"}}, ["case_dir", "util"])},
    {"name": "cht_report",
     "description": "Visualise multiRegion CHT results (per-region temperature field tricontourf) + quantify them (per-region T min/max/mean). Best for 2D cases. Use it to show a human the result after mesh/solve.",
     "input_schema": _obj({"case_dir": {"type": "string"}, "out_png": {"type": "string"}}, ["case_dir"])},
])


# ===========================================================================
# background (detached) solver jobs — run_solver defaults to background. The chat does not block, the job
# keeps running even after exit, and job_status/stop_job check/stop it. Job registry = RUNS_ROOT/jobs.json
# (survives an agent restart).
# ===========================================================================
import json as _json
import time as _time

_JOBS_FILE = Path(config.RUNS_ROOT) / "jobs.json"


def _jobs_load() -> dict:
    try:
        return _json.loads(_JOBS_FILE.read_text())
    except Exception:
        return {}


def _jobs_save(d: dict) -> None:
    try:
        _JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _JOBS_FILE.write_text(_json.dumps(d, ensure_ascii=False, indent=1, default=str))
    except Exception:
        pass


def _job_add(job_id: str, **info) -> str:
    d = _jobs_load(); d[job_id] = dict(info); _jobs_save(d)
    return job_id


def _job_state(j: dict) -> dict:
    pid = j.get("pid")
    alive = foam._running(pid) if pid else False
    txt = ""
    if j.get("log") and Path(j["log"]).is_file():
        try:
            txt = Path(j["log"]).read_text(errors="replace")
        except Exception:
            txt = ""
    sl = foam.parse_solver_log(txt) if txt else {}
    conv = {}
    try:
        import meshgen
        conv = meshgen.parse_convergence(j.get("case", ""), log_text=txt) or {}
    except Exception:
        conv = {}
    diverged = bool(sl.get("diverged"))
    completed = bool(_re.search(r"\n *End\b", txt))
    if alive:
        status = "running"
    elif j.get("status") == "stopped":
        status = "stopped"
    elif diverged:
        status = "failed(diverged)"
    elif completed:
        status = "done"
    else:
        status = "finished"        # process gone but no End — suspect kill/crash
    return {"status": status, "running": alive,
            "last_time": sl.get("last_time") if sl.get("last_time") is not None else conv.get("final_time"),
            "converged": bool(conv.get("converged")), "diverged": diverged, "completed": completed,
            "log_tail": txt[-800:] if txt else ""}


def job_status(job_id: str) -> dict:
    """Background job status·progress: running/done/failed(diverged)/stopped + last Time·convergence·log tail.
    Look it up by the job_id (= case name) returned by run_solver (background by default), or by case_dir."""
    jid = _run_id_of(job_id)
    d = _jobs_load(); j = d.get(jid)
    if not j:
        return {"ok": False, "error": f"no such job: {jid}", "jobs": list(d)}
    st = _job_state(j)
    if not st["running"] and d[jid].get("status") not in ("stopped", st["status"]):
        d[jid]["status"] = st["status"]; _jobs_save(d)
        try:
            registry.update_run(jid, status="bg_" + st["status"].split("(")[0])
        except Exception:
            pass
    return {"ok": True, "job_id": jid, "pid": j.get("pid"), "mode": j.get("mode"),
            "started": j.get("started"), **st}


def list_jobs() -> dict:
    """The stored background jobs + the status of each (persists across exit·restart)."""
    d = _jobs_load()
    jobs = [{"job_id": jid, "mode": j.get("mode"), "started": j.get("started"),
             "status": ("running" if (j.get("pid") and foam._running(j["pid"]))
                        else j.get("status", "finished"))}
            for jid, j in d.items()]
    return {"ok": True, "count": len(jobs), "jobs": jobs}


def stop_job(job_id: str) -> dict:
    """Stop a background solver job (terminates the process group). The last checkpoint (writeInterval) is
    preserved → you can resume from latestTime with run_solver. [approval required]"""
    jid = _run_id_of(job_id)
    d = _jobs_load(); j = d.get(jid)
    if not j:
        return {"ok": False, "error": f"no such job: {jid}"}
    pid = j.get("pid")
    if not pid or not foam._running(pid):
        d[jid]["status"] = "finished"; _jobs_save(d)
        return {"ok": True, "job_id": jid, "note": "already finished"}
    ok = foam.bg_stop(pid)
    d[jid]["status"] = "stopped"; _jobs_save(d)
    try:
        registry.update_run(jid, status="bg_stopped")
    except Exception:
        pass
    return {"ok": bool(ok), "job_id": jid,
            "note": "Stopped. The last checkpoint is preserved — you can resume with run_solver (startFrom latestTime)."}


DISPATCH.update({"job_status": job_status, "list_jobs": list_jobs, "stop_job": stop_job})
GATED |= {"stop_job"}        # job_status/list_jobs are read-only
TOOL_SPECS.extend([
    {"name": "job_status",
     "description": "Check a background solver job's status·progress — running/done/failed(diverged)/stopped + last Time·whether it converged·log tail. Look it up by the job_id (= case name) returned by run_solver (background by default).",
     "input_schema": _obj({"job_id": {"type": "string", "description": "job_id (= case name) or case_dir"}}, ["job_id"])},
    {"name": "list_jobs",
     "description": "The stored background jobs + the status of each (persists across agent exit·restart).",
     "input_schema": _obj({})},
    {"name": "stop_job",
     "description": "Stop a background solver job (terminates the process group). The last checkpoint is preserved → resume from latestTime with run_solver. [approval required]",
     "input_schema": _obj({"job_id": {"type": "string"}}, ["job_id"])},
])
