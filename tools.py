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
    """튜토리얼이 아닌(gmsh 등 from-scratch) 형상용 새 빈 케이스 폴더 + run_id 생성·등록.
    이후 generate_mesh(case_dir)로 메시·케이스를 채운다. copy_case의 from-scratch 짝."""
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
    """CAD 없이 인터넷에서 형상 다운로드(no-login 큐레이트 소스) → case/geometry/.
    kind: motorbike | airfoil(name 필요, 예 naca0012) | url(.stl/.obj/.dat 등). GATED."""
    dest_dir = _confined(str(Path(case_dir) / "geometry"))
    if dest_dir is None:
        return {"ok": False, "error": "case_dir가 허용 경로 밖(케이스 폴더여야 함)"}
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
    """gmsh로 메시 생성 → gmshToFoam → checkMesh (snappy 대신).
    geometry=cylinder_2d: 2D + caeloop validator-gated repair(휴리스틱→LLM), scaffold로 케이스까지.
    geometry=external_stl: fetch_geometry로 받은 STL/OBJ(예 motorBike)를 pymeshfix로 watertight
    repair→decimate→gmsh 외부 volume. improve=True면 품질 self-improve 루프(skew>=4→Frontal,
    nonortho/sicn 부족→faces↑)를 돌려 best를 찾고, gmsh tet의 non-ortho 한계를 못 넘으면
    결과에 snappy_suggested=True + snappy_reason을 실어 사용자에게 snappy 전환을 제안한다. GATED."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"gmsh/caeloop import 실패: {e}"}
    try:
        if geometry == "external_stl":
            if not stl:
                return {"ok": False, "error": "external_stl엔 stl 경로 필요(fetch_geometry로 받은 STL/OBJ)"}
            res = meshgen.generate_external(case_dir, stl, target_faces=20000, size_max=None,
                                            improve=bool(improve))
        elif geometry == "airfoil_2d":
            if not stl:
                return {"ok": False, "error": "airfoil_2d엔 stl=.dat 경로 필요(fetch_geometry kind=airfoil 결과)"}
            sm = 0.01 if abs(size_min - 0.3) < 1e-9 else float(size_min)   # airfoil-scale 기본
            sx = 0.5 if abs(size_max - 2.0) < 1e-9 else float(size_max)
            res = meshgen.generate_airfoil(case_dir, stl, aoa=float(aoa), chord=float(chord),
                                           size_min=sm, size_max=sx, scaffold=bool(scaffold))
        elif geometry == "external_netgen":
            if not stl:
                return {"ok": False, "error": "external_netgen엔 stl 경로 필요(clean watertight STL/OBJ)"}
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
    """gmsh 외부 메시(external_stl)에 snappyHexMesh addLayers-only로 벽면 prism boundary
    layer를 추가. generate_mesh(geometry=external_stl) 이후 실행. gmsh는 3D BL을 못 만들기에
    형상은 gmsh가 잡고 layer만 snappy가 넣는 하이브리드 — 3D 외부유동 RANS 벽면 정확도(drag·Cf)에
    필수. layer coverage는 벽 해상도가 좌우하므로 wall_faces(기본 30000)로 벽을 finer 재메싱해
    coverage를 올린다(20k→67%, 30k→78%). thin feature는 부분 coverage(결과 layer_coverage_pct). GATED."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import 실패: {e}"}
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
    """external_stl(+BL) 케이스에 외부유동 RANS 셋업(0/·constant/·system/ + forceCoeffs로 drag)을
    깐다. body bbox로 lRef/Aref/CofR 자동. 높은 non-ortho 안정화(limited schemes, nNonOrthogonalCorrectors,
    SIMPLE+압력완화) 포함. **residual_tol>0이면 residualControl로 수렴 시 end_time(=cap) 전에 자동
    정지**(OF12 drivaerFastback 방식); 0이면 고정 end_time 반복. cap은 find_similar_runs(과거 수렴 run의
    converged_iters)나 web_search로 정하면 좋다. add_boundary_layers 다음, run_solver 전. GATED."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import 실패: {e}"}
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
    """published offset table에서 프로펠러 형상을 생성(CAD-free) → case/constant/geometry에
    propeller.stl(블레이드+허브) + rotatingZone.stl(MRF cellZone 실린더). preset:
    **DTMB4119**(검증 표준, Jessup 1989) 또는 **B-series**(Wageningen parametric: n_blades=Z,
    area_ratio=AE/A0, pitch_ratio=P/D로 패밀리 생성, 예 B4-70=Z4·AE0.70). wrapped-section loft +
    hub. 이후 snappy(cellZone)+scaffold_mrf로 MRF. handed=±1로 회전손잡이(추력 방향). GATED."""
    try:
        import propgen
    except Exception as e:
        return {"ok": False, "error": f"propgen import 실패: {e}"}
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
    """rotating machinery(MRF): 회전 cellZone에 MRFProperties로 frame 회전을 주고 steady SIMPLE로
    푼다(회전메시 대신). forces functionObject로 축방향 thrust·torque 기록 → power=τ·ω. 메시에 회전
    cellZone이 있어야 함. run_solver 다음 rotor_performance로 성능을 읽는다. GATED."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import 실패: {e}"}

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
    """MRF 회전기계의 성능을 postProcessing/forces1에서 읽는다 → 축방향 thrust·torque·power(=τ·ω).
    axis·rpm 미지정 시 scaffold_mrf가 DB에 기록한 값을 쓴다. run_solver 다음에 호출."""
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import 실패: {e}"}
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
        return {"ok": False, "error": "postProcessing/forces1 없음 — forces functionObject가 안 돌았거나 run_solver 전"}
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
    """회전기계(MRF) 케이스의 **사람-판단용 시각 진단 report** 생성: ① geometry 3D, ② 블레이드 단면
    (airfoil 형상이 제대로인지), ③ flow 방향 vs 축 정렬, ④ 회전/thrust/torque 방향 schematic, ⑤ 성능·수렴,
    ⑥ human-judgment 체크리스트. PNG + md를 case/report/에 쓴다. 회전기계는 정량값만으론 판단이 어려워
    사람이 형상·방향을 눈으로 봐야 하므로(특히 thrust가 lift−drag의 작은 net force라 부호가 잘 틀림)."""
    try:
        import rotordiag
    except Exception as e:
        return {"ok": False, "error": f"rotordiag import 실패(matplotlib/trimesh 필요): {e}"}
    try:
        return rotordiag.make_rotor_report(case_dir)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def run_snappy_mesh(case_dir: str, refine_min: int = 2, refine_max: int = 4,
                    n_layers: int = 3, nprocs: int = 0, stl: str = "") -> dict:
    """임의 STL → full snappyHexMesh(castellate+snap+addLayers)로 hex-dominant + prism BL 메시
    생성(parallel). 정량 외부유동(drag)용 — gmsh-tet보다 품질 우수, dirty STL도 ray-cast라 견딤.
    stl 미지정 시 case/constant/triSurface 또는 case/geometry에서 자동 탐색(fetch_geometry 결과).
    이후 scaffold_external_ras → run_solver로 drag까지. GATED."""
    import glob
    try:
        import meshgen
    except Exception as e:
        return {"ok": False, "error": f"meshgen import 실패: {e}"}
    s = stl
    if not s:
        for pat in ("constant/triSurface/*", "geometry/*", "*.stl", "*.obj"):
            hits = [h for h in glob.glob(str(Path(case_dir) / pat))
                    if h.lower().endswith((".stl", ".obj"))]
            if hits:
                s = hits[0]
                break
    if not s:
        return {"ok": False, "error": "snappy엔 STL 필요(stl= 지정 또는 fetch_geometry 먼저)"}
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
    """Run foamRun, auto-parallelising via MPI when the mesh is large enough. **background=True(기본):
    솔버를 백그라운드로 던지고 job_id를 즉시 반환** — 채팅 안 멈추고, exit해도 계속 돌며, `job_status`로
    진행 확인·`stop_job`으로 중단(마지막 checkpoint 보존 → latestTime 이어받기). background=False면 끝까지
    blocking(작은 케이스/스크립트용). nprocs=None -> auto. GATED. Auto-records."""
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
                "note": f"백그라운드 실행 시작({mode}). job_status('{run_id}')로 진행 확인, "
                        f"stop_job('{run_id}')로 중단. exit해도 계속 돕니다."}
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
    # residualControl 인지 수렴: cap(endTime) 전에 멈췄거나 'solution converged' 메시지가 있으면 수렴.
    # 단순히 '돌았다'(ran_ok)와 구분 — converged_iters는 다음 run의 cap을 DB·web으로 정하는 핵심 수치.
    conv = {}
    try:
        if meshgen is not None:
            conv = meshgen.parse_convergence(case_dir, log_text=r.stdout) or {}
    except Exception:
        conv = {}
    # residualControl 수렴: OF 명시 메시지 + 깨끗한 종료(r.ok) + foam 발산판정(고-Courant/NaN 포함)으로
    # 교차검증. 이래야 timeout·kill·발산이 '수렴'으로 둔갑해 converged_iters를 DB에 오염시키지 않는다.
    residual_converged = bool(conv.get("converged")) and r.ok and not sl["diverged"]
    conv_iters = conv.get("converged_iters") if residual_converged else None
    cap = conv.get("end_time_cap")
    final_t = conv.get("final_time")
    hit_cap = bool(cap is not None and final_t is not None and final_t >= cap
                   and not residual_converged and not sl["diverged"])
    # cap 밑에서 멈췄는데 수렴 메시지도 발산도 아니면 → timeout·kill 의심(에이전트가 로그를 봐야 함)
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
    # column 'converged'은 '발산 없이 끝까지 돌았나'(ran_ok)의 거친 의미 유지(transient도 포함).
    # 정밀한 residualControl 수렴·converged_iters는 solve JSON에만 기록(가짜 없음 — 위 교차검증).
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
    """후처리/보고서: residual 수렴 그래프 + 속도장 + REPORT.md를 case/report/에 생성."""
    try:
        import postproc
    except Exception as e:
        return {"ok": False, "error": f"postproc import 실패(venv의 matplotlib 필요): {e}"}
    return postproc.build_report(case_dir)


# ----------------------------------------------------------------- files / excel
def write_file(path: str, content: str) -> dict:
    """임의 텍스트 파일 생성/덮어쓰기 (케이스/리포트 폴더 안으로 제한). GATED."""
    dest = _confined(path)
    if dest is None:
        return {"ok": False, "error": f"허용된 경로 밖입니다(케이스 폴더나 reports만 가능): {path}"}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        existed = dest.is_file()
        dest.write_text(content)
        return {"ok": True, "path": str(dest), "overwritten": existed,
                "bytes": len(content.encode("utf-8"))}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def build_excel(path: str, sheets: list) -> dict:
    """엑셀(.xlsx) 생성: 시트별 표(columns/rows) + 이미지(png 경로) 임베드.
    path와 임베드 이미지 경로는 모두 케이스/리포트 폴더 안으로 제한된다."""
    dest = _confined(path)
    if dest is None:
        return {"ok": False, "error": f"허용된 경로 밖입니다: {path}"}
    if dest.suffix.lower() not in (".xlsx", ".xlsm"):
        return {"ok": False, "error": "path는 .xlsx 여야 합니다"}
    try:
        import xlsx
    except Exception as e:
        return {"ok": False, "error": f"openpyxl 필요: {e}"}
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
    """파일/폴더를 지운다. 안전상 RUNS_ROOT·REPORTS_ROOT '안'의 경로만 허용하고(_confined가 '..'·
    심링크·바깥 경로를 거부), 데이터 루트 자체나 시스템/프로젝트 소스는 막는다. 폴더는 recursive=True여야
    내용까지 지운다. 실패한 메시·오래된 time/processor dir·버린 케이스 정리용. 비가역 — GATED(승인 필요)."""
    import shutil
    rp = _confined(path)
    if rp is None:
        return {"ok": False, "error": f"거부: '{path}'는 허용 루트(RUNS_ROOT·REPORTS_ROOT) 밖이거나 해석 불가"}
    roots = {Path(config.RUNS_ROOT).resolve(), Path(config.REPORTS_ROOT).resolve()}
    if rp in roots:
        return {"ok": False, "error": f"거부: 데이터 루트 자체는 못 지운다 — 그 '안'의 항목만({rp})"}
    if not rp.exists():
        return {"ok": False, "error": f"없는 경로: {rp}"}
    try:
        if rp.is_dir():
            files = [f for f in rp.rglob("*") if f.is_file()]
            nfiles = len(files)
            total = sum(f.stat().st_size for f in files if f.exists())
            if (nfiles > 0 or any(rp.iterdir())) and not recursive:
                return {"ok": False, "files": nfiles,
                        "error": f"폴더가 비어있지 않음({nfiles} files) — 내용까지 지우려면 recursive=True"}
            shutil.rmtree(rp)
            return {"ok": True, "deleted": str(rp), "type": "dir",
                    "files_removed": nfiles, "bytes_removed": total}
        size = rp.stat().st_size
        rp.unlink()
        return {"ok": True, "deleted": str(rp), "type": "file", "bytes_removed": size}
    except Exception as e:
        return {"ok": False, "error": f"삭제 실패: {type(e).__name__}: {e}"}


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
     "description": "설치된 OF12 튜토리얼 케이스 목록(system/controlDict 보유 디렉토리)을 substring으로 필터해 반환. 시작 케이스 선택과 OF12 정답 문법 확인의 근거.",
     "input_schema": _obj({
         "filter": {"type": "string", "description": "경로 substring (예: 'cavity', 'incompressibleFluid')"},
         "limit": {"type": "integer"}})},
    {"name": "copy_case",
     "description": "튜토리얼 케이스를 새 run 폴더(RUNS_ROOT/<run_id>)로 복사하고 registry에 등록. case_dir와 run_id 반환.",
     "input_schema": _obj({
         "tutorial": {"type": "string", "description": "$FOAM_TUTORIALS 기준 상대경로 (예: 'incompressibleFluid/cavity')"},
         "run_name": {"type": "string", "description": "선택: case_kind 라벨"}}, ["tutorial"])},
    {"name": "new_case",
     "description": "튜토리얼이 아닌 from-scratch 형상(gmsh 등)용 새 빈 케이스 폴더 + run_id 생성. 이후 generate_mesh(case_dir)로 채운다. copy_case의 from-scratch 짝.",
     "input_schema": _obj({"name": {"type": "string", "description": "case_kind 라벨(예: cylinder_2d)"}})},
    {"name": "fetch_geometry",
     "description": "CAD 없이 인터넷에서 형상을 다운로드(로그인 없는 큐레이트 소스)해 case/geometry/에 저장. kind=motorbike(OpenFOAM motorBike.obj) | airfoil(name 필요, 예 naca0012 — UIUC .dat) | url(.stl/.obj/.dat 직접 URL). [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "kind": {"type": "string", "enum": ["motorbike", "airfoil", "url"]},
         "name": {"type": "string", "description": "airfoil일 때 이름(예: naca0012)"},
         "url": {"type": "string", "description": "kind=url일 때 직접 URL(.stl/.obj/.dat 등)"}},
         ["case_dir", "kind"])},
    {"name": "read_dict",
     "description": "케이스 내 dict 파일(예: system/controlDict, constant/momentumTransport, 0/U)을 읽어 현재 설정 확인.",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "rel_path": {"type": "string", "description": "케이스 기준 상대경로"},
         "max_chars": {"type": "integer"}}, ["case_dir", "rel_path"])},
    {"name": "write_dict_entry",
     "description": "dict 최상위 항목 값 변경(예: controlDict endTime/deltaT/writeInterval, momentumTransport model). [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "rel_path": {"type": "string"},
         "key": {"type": "string"},
         "value": {"type": "string"}}, ["case_dir", "rel_path", "key", "value"])},
    {"name": "run_mesh",
     "description": "격자 생성 (blockMesh 또는 snappyHexMesh). [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "tool": {"type": "string", "enum": ["blockMesh", "snappyHexMesh"]}}, ["case_dir"])},
    {"name": "check_mesh",
     "description": "checkMesh 실행, 품질 지표(cells, max non-ortho, max skewness, Mesh OK) 파싱 반환.",
     "input_schema": _obj({"case_dir": {"type": "string"}}, ["case_dir"])},
    {"name": "generate_mesh",
     "description": "gmsh로 형상 메시 생성(snappy 대신 robust 경로). caeloop validator-gated repair: 품질/element수 validator가 결정, 실패하면 휴리스틱→LLM이 size를 고쳐 자동 재시도. 통과하면 gmshToFoam→OpenFOAM polyMesh→front/back empty(2D)→checkMesh. blockMesh로 안 되는 외부/곡선 형상에 사용. [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "geometry": {"type": "string", "enum": ["cylinder_2d", "airfoil_2d", "external_stl", "external_netgen"],
                      "description": "cylinder_2d · airfoil_2d(.dat) · external_stl(gmsh+snappy BL, dirty OK) · external_netgen(OCP+netgen prism BL, **clean watertight 형상**용)"},
         "target_cells": {"type": "integer", "description": "cylinder_2d 목표 cell 수(기본 8000)"},
         "size_min": {"type": "number", "description": "형상 근처 mesh size 초기값"},
         "size_max": {"type": "number", "description": "원거리 mesh size 초기값"},
         "scaffold": {"type": "boolean", "description": "풀 수 있는 케이스 템플릿까지 깔지(cylinder_2d, 기본 true)"},
         "stl": {"type": "string", "description": "external_stl일 때 STL/OBJ 경로(fetch_geometry 결과)"},
         "improve": {"type": "boolean", "description": "external_stl 품질 self-improve 루프 on/off(기본 true). 한계 도달 시 결과에 snappy_suggested"},
         "aoa": {"type": "number", "description": "airfoil_2d 받음각(deg, 기본 0)"},
         "chord": {"type": "number", "description": "airfoil_2d 익현 길이(기본 1.0)"}},
         ["case_dir"])},
    {"name": "add_boundary_layers",
     "description": "gmsh 외부 메시(external_stl)에 snappyHexMesh addLayers-only로 벽면 prism boundary layer 추가. 3D 외부유동 RANS 벽면 정확도(drag·Cf)에 필수(BL 없으면 무의미). gmsh가 형상, snappy가 layer만 넣는 하이브리드. generate_mesh(external_stl) 다음에 실행. thin feature는 부분 coverage(결과 layer_coverage_pct·avg_layers). [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "n_layers": {"type": "integer", "description": "목표 prism layer 수(기본 3)"},
         "first_layer": {"type": "number", "description": "첫 layer 두께(절대값; 0이면 body 대각선의 0.12%로 자동)"},
         "expansion": {"type": "number", "description": "layer 팽창비(기본 1.25)"},
         "wall_faces": {"type": "integer", "description": "벽면 재메시 면 수(기본 30000; coverage↑, 0이면 기존 메시 사용)"}},
         ["case_dir"])},
    {"name": "run_snappy_mesh",
     "description": "임의 STL → full snappyHexMesh(castellate+snap+addLayers)로 hex-dominant + prism BL 메시(parallel). 정량 외부유동 drag용 — gmsh-tet보다 품질 우수, dirty STL(motorBike)도 ray-cast라 견딤. fetch_geometry로 STL 받은 뒤 실행 → scaffold_external_ras → run_solver로 drag까지. 결과 패치 inlet/outlet/bottom/farfield/body(wall). [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "stl": {"type": "string", "description": "STL/OBJ 경로(미지정 시 case에서 자동 탐색)"},
         "refine_min": {"type": "integer", "description": "snappy 표면 최소 refinement level(기본 2)"},
         "refine_max": {"type": "integer", "description": "snappy 표면 최대 refinement level(기본 4)"},
         "n_layers": {"type": "integer", "description": "prism layer 수(기본 3)"},
         "nprocs": {"type": "integer", "description": "MPI 프로세스 수(0=자동)"}},
         ["case_dir"])},
    {"name": "scaffold_external_ras",
     "description": "external_stl(+BL) 케이스에 kOmegaSST 외부유동 RANS 셋업(0/·constant/·system/ + forceCoeffs로 drag 측정)을 깐다. body bbox로 lRef/Aref/CofR 자동, 높은 non-ortho 메시 안정화(limited schemes·nNonOrthogonalCorrectors·SIMPLE+압력완화) 포함. add_boundary_layers 다음 → run_solver(MPI 자동) → build_report. [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "flow_velocity": {"type": "number", "description": "자유류 속도 U (m/s, 기본 20). Re=U·L/nu"},
         "nu": {"type": "number", "description": "동점성계수 (기본 1.5e-5, 공기). U와 함께 Re 결정"},
         "turbulence": {"type": "string", "enum": ["kOmegaSST", "SpalartAllmaras", "kEpsilon", "laminar"],
                        "description": "난류 모델(기본 kOmegaSST). 모델별 필드·BC·wall function 자동"},
         "end_time": {"type": "integer", "description": "SIMPLE iteration **상한(cap)**, 기본 500. residual_tol>0이면 수렴 시 그 전에 자동 정지. cap은 find_similar_runs(과거 수렴 run의 converged_iters)·web_search로 정하면 좋다"},
         "residual_tol": {"type": "number", "description": "수렴 임계(기본 1e-4). p·U·난류필드 초기잔차가 모두 이 값 밑이면 residualControl이 자동 정지. 0이면 고정 end_time 반복(옛 방식)"},
         "nnonortho": {"type": "integer", "description": "nNonOrthogonalCorrectors (기본 3; 고 non-ortho 메시용)"}},
         ["case_dir"])},
    {"name": "run_solver",
     "description": "foamRun 실행. 격자가 크면 cell 수에 따라 자동 MPI 병렬(1~14 rank: decomposePar scotch→mpirun→reconstructPar). controlDict의 solver 사용. 결과(mode, nprocs, last_time, maxCo, diverged) 자동 저장. [승인 필요] 긴 실행 전 endTime 확인, 처음엔 짧게.",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "timeout": {"type": "integer", "description": "초 (기본 1800)"},
         "nprocs": {"type": "integer", "description": "병렬 rank 강제(생략=cell 수로 자동)"}}, ["case_dir"])},
    {"name": "read_log",
     "description": "case의 log.<which> 파일 끝부분 반환 (디버깅용).",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "which": {"type": "string", "description": "예: foamRun, blockMesh, checkMesh"},
         "tail": {"type": "integer"}}, ["case_dir"])},
    {"name": "build_report",
     "description": "후처리/보고서: 솔버 로그에서 residual 수렴 그래프 + 속도장(tricontourf) + REPORT.md를 case/report/에 생성. run_solver 이후 호출.",
     "input_schema": _obj({"case_dir": {"type": "string"}}, ["case_dir"])},
    {"name": "write_file",
     "description": "임의 텍스트 파일을 생성/덮어쓴다. 새 OF dict(snappyHexMeshDict, fvModels, 0/ 필드 등), 메모, 커스텀 스크립트 작성에 사용. 경로는 케이스 폴더(run_id 아래)나 reports 안만 허용. [승인 필요]",
     "input_schema": _obj({
         "path": {"type": "string", "description": "쓸 파일 경로(케이스/리포트 안)"},
         "content": {"type": "string"}}, ["path", "content"])},
    {"name": "build_excel",
     "description": "엑셀(.xlsx)을 만든다 — 시트별로 표와 이미지를 넣는다. 예: Reynolds 스윕 비교표 + 각 run의 velocity.png 임베드. path와 이미지 경로는 케이스/리포트 폴더 안.",
     "input_schema": _obj({
         "path": {"type": "string", "description": ".xlsx 경로 (reports 안 권장)"},
         "sheets": {"type": "array", "description": "시트 목록", "items": {
             "type": "object",
             "properties": {
                 "name": {"type": "string"},
                 "tables": {"type": "array", "items": {"type": "object", "properties": {
                     "title": {"type": "string"},
                     "columns": {"type": "array", "items": {"type": "string"}},
                     "rows": {"type": "array", "items": {"type": "array"},
                              "description": "행 목록; 각 행은 셀 값의 배열"}}}},
                 "images": {"type": "array", "items": {"type": "string"},
                            "description": "임베드할 png/jpg 경로(케이스/리포트 안)"}}}}},
                          ["path", "sheets"])},
    {"name": "list_runs",
     "description": "최근 run 목록(설정·결과) 반환.",
     "input_schema": _obj({"limit": {"type": "integer"}, "case_kind": {"type": "string"}})},
    {"name": "get_run",
     "description": "run_id로 단일 run의 저장된 설정·결과 조회.",
     "input_schema": _obj({"run_id": {"type": "string"}}, ["run_id"])},
    {"name": "find_similar_runs",
     "description": "과거 run을 case_kind/solver/turb_model로 검색(기본 수렴된 것만). 새 설정 제안의 근거로 사용.",
     "input_schema": _obj({
         "case_kind": {"type": "string"},
         "solver": {"type": "string"},
         "turb_model": {"type": "string"},
         "converged_only": {"type": "boolean"},
         "limit": {"type": "integer"}})},
    {"name": "delete_path",
     "description": "파일이나 폴더를 지운다(실패한 메시·오래된 time/processor dir·버린 케이스 정리용). 안전상 RUNS_ROOT·REPORTS_ROOT 안의 경로만 허용하고 데이터 루트 자체·시스템·프로젝트 소스는 거부. 폴더는 recursive=True여야 내용까지 지운다. 비가역. [승인 필요]",
     "input_schema": _obj({
         "path": {"type": "string", "description": "지울 파일/폴더 (RUNS_ROOT·REPORTS_ROOT 안)"},
         "recursive": {"type": "boolean", "description": "폴더의 내용까지 지울지(비어있지 않은 폴더엔 필수)"}},
         ["path"])},
    {"name": "generate_propeller",
     "description": "CAD-free 프로펠러 형상 생성: offset table에서 wrapped-section을 loft해 propeller.stl + rotatingZone.stl(MRF cellZone)을 case/constant/geometry에 쓴다. preset=**DTMB4119**(검증 표준, Jessup 1989) 또는 **B-series**(Wageningen parametric — n_blades=Z·area_ratio=AE/A0·pitch_ratio=P/D로 프로펠러 패밀리 생성). 이후 snappy(cellZone)→scaffold_mrf→run_solver→rotor_performance. handed=±1로 추력 방향(rotor_report로 확인). [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "preset": {"type": "string", "description": "DTMB4119(기본) 또는 B-series(Wageningen)"},
         "diameter": {"type": "number", "description": "직경 m(기본 0.3048)"},
         "n_blades": {"type": "integer", "description": "블레이드 수 Z(기본 3; DTMB는 3, B-series는 2~7)"},
         "hub_ratio": {"type": "number", "description": "허브/반경 비(기본 0.2)"},
         "handed": {"type": "integer", "description": "회전손잡이 ±1 (추력 방향)"},
         "area_ratio": {"type": "number", "description": "B-series 전개면적비 AE/A0(기본 0.70, 0.3~1.05)"},
         "pitch_ratio": {"type": "number", "description": "B-series 피치비 P/D(기본 1.0, 0.5~1.4)"}},
         ["case_dir"])},
    {"name": "scaffold_mrf",
     "description": "rotating machinery(MRF) 셋업: 회전 cellZone에 MRFProperties로 frame 회전을 주고 steady SIMPLE로 thrust·torque를 푼다(회전메시 대신, 싸고 빠름). 메시에 회전 cellZone이 있어야 함(snappy refinementSurfaces faceZone/cellZone으로 생성). forces functionObject로 축방향 thrust·torque·power 기록, residualControl로 수렴 자동정지. 메시 다음 → run_solver → rotor_performance. [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "cellzone": {"type": "string", "description": "회전영역 cellZone 이름(기본 innerCylinder)"},
         "rpm": {"type": "number", "description": "회전수 RPM(기본 1500). omega=rpm·2π/60"},
         "axis": {"type": "string", "description": "회전·추력 축 벡터, 예 '0 1 0'(기본)"},
         "origin": {"type": "string", "description": "회전 중심, 예 '0 0 0'(기본)"},
         "rotor_patches": {"type": "string", "description": "회전벽 patch 정규식(기본 'propeller.*')"},
         "turbulence": {"type": "string", "enum": ["kEpsilon", "kOmegaSST", "SpalartAllmaras", "laminar"],
                        "description": "난류 모델(기본 kEpsilon)"},
         "flow_velocity": {"type": "number", "description": "유입 속도(기록용, 기본 5)"},
         "nu": {"type": "number", "description": "동점성계수(기본 1e-6=물; 공기는 1.5e-5)"},
         "residual_tol": {"type": "number", "description": "수렴 임계(기본 1e-4)"},
         "end_time": {"type": "integer", "description": "SIMPLE iteration 상한 cap(기본 3000)"}},
         ["case_dir"])},
    {"name": "rotor_performance",
     "description": "MRF 회전기계 성능을 postProcessing/forces1에서 읽는다 → 축방향 thrust·torque·power(=τ·ω). axis·rpm 미지정 시 scaffold_mrf가 DB에 기록한 값 사용. run_solver 다음에 호출, 문헌비교의 근거.",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "axis": {"type": "string", "description": "선택: 축 벡터(미지정 시 DB값)"},
         "rpm": {"type": "number", "description": "선택: RPM(미지정 시 DB값)"}}, ["case_dir"])},
    {"name": "rotor_report",
     "description": "회전기계(MRF) 케이스의 **사람-판단용 시각 진단 report**(PNG+md): geometry 3D · 블레이드 단면(airfoil 형상) · flow 방향 vs 축 · 회전/thrust/torque 방향 · 성능/수렴 · human-judgment 체크리스트. 회전기계는 정량값만으론 판단이 어려워(thrust=lift−drag의 작은 net force) 사람이 형상·방향을 봐야 하므로. mesh/solve 후 사람에게 보여줄 때.",
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
    """현재 로드된 핫리로드 플러그인 도구 목록(tools_plugins/tool_*.py)."""
    return {"ok": True, "plugins": sorted(selfevolve._PLUGIN_NAMES),
            "files": [p.name for p in selfevolve.discover_plugins()]}


def reload_plugins() -> dict:
    """tools_plugins/를 다시 스캔해 도구를 핫리로드(코어 루프 재시작 불필요)."""
    return selfevolve.reload_plugins()


def run_self_tests(level: str = "fast") -> dict:
    """canary 회귀 테스트(문법·도구정합성·물리검증 프로펠러 수학). level='fast'(~1s) | 'full'."""
    return selfevolve.canary(level)


def propose_self_edit(filename: str, old: str = "", new: str = "", create: bool = False) -> dict:
    """자가수정 제안: incubator(격리된 전체 복사본)에 적용→canary→diff/검증 반환(실제 적용 X). [승인 필요]"""
    return selfevolve.propose_edit(filename, old, new, create=create)


def apply_self_edit(edit_id: str, force: bool = False) -> dict:
    """제안된 자가수정을 승격: 스냅샷(롤백점)→적용→핫리로드(plugin)/재시작신호(kernel). [승인 필요]"""
    return selfevolve.apply_edit(edit_id, force=force)


def rollback_self(snapshot_id: str) -> dict:
    """스냅샷으로 소스 롤백(커널은 재시작 필요). [승인 필요]"""
    return selfevolve.rollback(snapshot_id)


def evolve_log(n: int = 20) -> dict:
    """자가진화 이력(스냅샷/제안/적용/롤백/canary 결과)."""
    return {"ok": True, "log": selfevolve.evolve_log(n), "snapshots": selfevolve.list_snapshots()[-10:]}


def set_auto_evolve(on: bool, budget: int = 8, goal: str = "") -> dict:
    """자율 진화 모드 ON/OFF(budget=무승인 플러그인 편집 허용 횟수). 켜는 것 자체가 [승인 필요]."""
    return selfevolve.set_auto(on, budget, goal or None)


def auto_evolve(filename: str = "", old: str = "", new: str = "", reason: str = "",
                create: bool = False) -> dict:
    """자율 자가편집(사람 승인 없이): **플러그인만**·canary 통과 필수·예산 차감·실패 시 자동폐기·커널 거부.
    set_auto_evolve(True) 선행 필요. 자율모드에서만 동작하며 engine이 안전 펜스를 강제."""
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
     "description": "핫리로드 플러그인 도구(tools_plugins/tool_*.py) 목록.",
     "input_schema": _obj({})},
    {"name": "reload_plugins",
     "description": "tools_plugins/를 재스캔해 플러그인 도구를 핫리로드(코어 루프 재시작 없이 다음 턴부터 사용).",
     "input_schema": _obj({})},
    {"name": "run_self_tests",
     "description": "자가검증 canary 회귀 테스트: 문법(py_compile 전체)·도구 레지스트리 정합성·물리검증 프로펠러 수학(B-series 다항식, DTMB 표, watertight 형상, 수렴 파서). 자가수정 전후 안전 확인의 oracle. level='fast'|'full'.",
     "input_schema": _obj({"level": {"type": "string", "description": "fast(기본) | full"}})},
    {"name": "propose_self_edit",
     "description": "**자가수정 제안** — filename의 old→new를 격리된 incubator(전체 프로젝트 복사본)에 적용하고 그 안에서 canary를 돌려 검증한 뒤 diff+결과만 반환(실제 코드는 안 건드림). create=True면 새 파일. 통과하면 apply_self_edit로 승격. [승인 필요]",
     "input_schema": _obj({
         "filename": {"type": "string", "description": "예: tool_x.py(플러그인) 또는 propgen.py(모듈)"},
         "old": {"type": "string", "description": "교체할 기존 문자열(파일 내 고유)"},
         "new": {"type": "string", "description": "새 문자열"},
         "create": {"type": "boolean", "description": "새 파일 생성 시 True(new가 전체 내용)"}}, ["filename"])},
    {"name": "apply_self_edit",
     "description": "propose_self_edit가 만든 staged 변경을 **승격(적용)**: 현재 상태 스냅샷(롤백점)→파일 교체→플러그인이면 핫리로드/모듈이면 재시작신호. incubator canary 재확인(실패 시 거부, force=True로 강제). [승인 필요]",
     "input_schema": _obj({
         "edit_id": {"type": "string"},
         "force": {"type": "boolean", "description": "canary 실패해도 강제 적용"}}, ["edit_id"])},
    {"name": "rollback_self",
     "description": "스냅샷 ID로 소스 코드를 되돌림(커널 파일은 재시작해야 반영). evolve_log로 스냅샷 확인. [승인 필요]",
     "input_schema": _obj({"snapshot_id": {"type": "string"}}, ["snapshot_id"])},
    {"name": "evolve_log",
     "description": "자가진화 이력(스냅샷·제안·적용·롤백·canary)과 최근 스냅샷 목록.",
     "input_schema": _obj({"n": {"type": "integer"}})},
    {"name": "set_auto_evolve",
     "description": "**자율 진화 모드 토글**. on=True면 budget 횟수만큼 사람 승인 없이 플러그인을 스스로 고칠 수 있음(canary 통과한 것만). 켜는 것 자체가 위험하므로 [승인 필요].",
     "input_schema": _obj({
         "on": {"type": "boolean"},
         "budget": {"type": "integer", "description": "무승인 편집 허용 횟수(기본 8)"},
         "goal": {"type": "string", "description": "진화 목표 메모"}}, ["on"])},
    {"name": "auto_evolve",
     "description": "**자율 자가편집**(사람 승인 루프 없이) — 단 engine이 강제하는 펜스: ① 플러그인(tool_*.py)만 ② canary 통과해야 적용(실패 시 자동 폐기) ③ 세션 예산 차감 ④ 커널/PROTECTED 거부 ⑤ 전부 로깅. set_auto_evolve(True) 선행 필요. 자가치유/개선용.",
     "input_schema": _obj({
         "filename": {"type": "string", "description": "tools_plugins의 tool_*.py"},
         "old": {"type": "string"}, "new": {"type": "string"},
         "reason": {"type": "string", "description": "왜 고치는지(로그/감사)"},
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
    """multiRegion **CHT(공액열전달)** 케이스 실행: 메시 → region 분리 → foamMultiRun. setup_cht=True면
    foamSetupCHT(예 coolingSphere). snappy=True면 blockMesh+snappyHexMesh. split=True면 splitMeshRegions
    (split_args 예 '-cellZones -overwrite' 또는 '-cellZones -defaultRegionName fluid -overwrite'). parallel=True면
    decomposePar -allRegions → mpirun foamMultiRun -parallel → reconstructPar -allRegions. region 목록·최종시간·
    완료/발산·region별 T(min/max/mean) 반환. 튜토리얼 multiRegion/CHT/* 를 copy_case 후 쓰는 게 정석. [승인 필요]"""
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
    """단일 OpenFOAM 유틸리티 실행(ALLOWED_BINS 내에서만). multiRegion 메싱 체인(예: topoSet → createBaffles
    → extrudeToRegionMesh → splitMeshRegions)을 단계별로 돌릴 때 쓴다. circuitBoardCooling류 extrude CHT용. [승인 필요]"""
    r = foam.run_foam(util, (args or "").split(), case=str(case_dir), timeout=int(timeout))
    return {"ok": bool(r.ok), "util": util, "returncode": r.returncode,
            "duration_s": round(r.duration_s, 1), "log_tail": r.tail(900)}


def cht_report(case_dir: str, out_png: str | None = None) -> dict:
    """multiRegion CHT 결과 **시각화 + 정량**: region별 온도장(2D tricontourf, cell-centre 기반) + T min/max/mean.
    writeCellCentres 후 렌더(2D 케이스 최적; 3D는 xy 투영). PNG 경로·region별 T 통계 반환."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as _plt
    import numpy as _np
    cd = Path(case_dir)
    times = foam.list_time_dirs(str(cd))
    if not times:
        return {"ok": False, "error": "결과 시간 디렉토리 없음 — run_cht 먼저"}
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
        return {"ok": False, "error": "온도장 읽기 실패", "regions": regions}
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
     "description": "multiRegion **CHT(공액열전달)** 케이스 실행: 메시→region 분리(splitMeshRegions)→**foamMultiRun**. setup_cht(foamSetupCHT)·snappy·split·parallel(decomposePar/reconstructPar -allRegions) 옵션. region·최종시간·완료/발산·region별 T(min/max/mean) 반환. OF12 multiRegion/CHT/* 튜토리얼을 copy_case 후 실행. [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"},
         "mesh_steps": {"type": "array", "description": "메시 단계 전체 제어: [[util, args], ...] 순서대로 실행. 케이스별 다름. 예 coolingSphere: [['blockMesh',''],['topoSet',''],['splitMeshRegions','-cellZones -defaultRegionName fluid -overwrite'],['foamSetupCHT','']]. 생략하면 아래 플래그로 구성.", "items": {"type": "array"}},
         "setup_cht": {"type": "boolean", "description": "메시 후 foamSetupCHT 추가(예 coolingSphere)"},
         "snappy": {"type": "boolean", "description": "blockMesh+snappyHexMesh(예 heatedDuct)"},
         "split": {"type": "boolean", "description": "splitMeshRegions 실행(기본 True)"},
         "split_args": {"type": "string", "description": "예 '-cellZones -overwrite' 또는 '-cellZones -defaultRegionName fluid -overwrite'"},
         "parallel": {"type": "boolean", "description": "MPI 병렬(작은 메시는 과분해 주의 — nprocs 직접 지정 권장)"},
         "nprocs": {"type": "integer"}}, ["case_dir"])},
    {"name": "run_foam_util",
     "description": "단일 OF 유틸리티 실행(ALLOWED_BINS 내). multiRegion 메싱 체인(topoSet·createBaffles·extrudeToRegionMesh·splitMeshRegions)을 단계별로 — circuitBoardCooling류 extrude CHT용. [승인 필요]",
     "input_schema": _obj({
         "case_dir": {"type": "string"}, "util": {"type": "string", "description": "예 topoSet, createBaffles, extrudeToRegionMesh"},
         "args": {"type": "string", "description": "유틸 인자(공백구분)"}}, ["case_dir", "util"])},
    {"name": "cht_report",
     "description": "multiRegion CHT 결과 시각화(region별 온도장 tricontourf)+정량(region별 T min/max/mean). 2D 케이스 최적. mesh/solve 후 사람에게 보여줄 때.",
     "input_schema": _obj({"case_dir": {"type": "string"}, "out_png": {"type": "string"}}, ["case_dir"])},
])


# ===========================================================================
# background (detached) solver jobs — run_solver는 background 기본. 채팅 안 멈추고, exit해도 계속 돌며,
# job_status/stop_job로 확인·중단. 잡 레지스트리 = RUNS_ROOT/jobs.json (에이전트 재시작해도 유지).
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
        status = "finished"        # 프로세스 없어졌는데 End 없음 — kill/crash 의심
    return {"status": status, "running": alive,
            "last_time": sl.get("last_time") if sl.get("last_time") is not None else conv.get("final_time"),
            "converged": bool(conv.get("converged")), "diverged": diverged, "completed": completed,
            "log_tail": txt[-800:] if txt else ""}


def job_status(job_id: str) -> dict:
    """백그라운드 잡 상태·진행: running/done/failed(diverged)/stopped + 마지막 Time·수렴·로그 tail.
    run_solver(background 기본)가 준 job_id(=케이스명) 또는 case_dir로 조회."""
    jid = _run_id_of(job_id)
    d = _jobs_load(); j = d.get(jid)
    if not j:
        return {"ok": False, "error": f"job 없음: {jid}", "jobs": list(d)}
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
    """저장된 백그라운드 잡 목록 + 각 상태 (exit·재시작 후에도 유지)."""
    d = _jobs_load()
    jobs = [{"job_id": jid, "mode": j.get("mode"), "started": j.get("started"),
             "status": ("running" if (j.get("pid") and foam._running(j["pid"]))
                        else j.get("status", "finished"))}
            for jid, j in d.items()]
    return {"ok": True, "count": len(jobs), "jobs": jobs}


def stop_job(job_id: str) -> dict:
    """백그라운드 솔버 잡 중단(프로세스 그룹 종료). 마지막 checkpoint(writeInterval) 보존 → run_solver로
    latestTime 이어받기 가능. [승인 필요]"""
    jid = _run_id_of(job_id)
    d = _jobs_load(); j = d.get(jid)
    if not j:
        return {"ok": False, "error": f"job 없음: {jid}"}
    pid = j.get("pid")
    if not pid or not foam._running(pid):
        d[jid]["status"] = "finished"; _jobs_save(d)
        return {"ok": True, "job_id": jid, "note": "이미 종료됨"}
    ok = foam.bg_stop(pid)
    d[jid]["status"] = "stopped"; _jobs_save(d)
    try:
        registry.update_run(jid, status="bg_stopped")
    except Exception:
        pass
    return {"ok": bool(ok), "job_id": jid,
            "note": "중단됨. 마지막 checkpoint 보존 — run_solver로 이어받기 가능(startFrom latestTime)."}


DISPATCH.update({"job_status": job_status, "list_jobs": list_jobs, "stop_job": stop_job})
GATED |= {"stop_job"}        # job_status/list_jobs는 read-only
TOOL_SPECS.extend([
    {"name": "job_status",
     "description": "백그라운드 솔버 잡 상태·진행 확인 — running/done/failed(diverged)/stopped + 마지막 Time·수렴여부·로그 tail. run_solver(background 기본)가 반환한 job_id(=케이스명)로 조회.",
     "input_schema": _obj({"job_id": {"type": "string", "description": "job_id(=케이스명) 또는 case_dir"}}, ["job_id"])},
    {"name": "list_jobs",
     "description": "저장된 백그라운드 잡 목록 + 각 상태(에이전트 exit·재시작 후에도 유지).",
     "input_schema": _obj({})},
    {"name": "stop_job",
     "description": "백그라운드 솔버 잡 중단(프로세스 그룹 종료). 마지막 checkpoint 보존 → run_solver로 latestTime 이어받기. [승인 필요]",
     "input_schema": _obj({"job_id": {"type": "string"}}, ["job_id"])},
])
