"""
meshgen.py — LLM-guided, validator-gated mesh generation (Phase 3).

Runs a gmsh meshing job through the caeloop repair loop:
  validators (deterministic) decide pass/fail  ->  on failure, repair backend
  (heuristic rescale first, LLM fallback) rewrites the size knobs  ->  re-mesh,
bounded. Then gmshToFoam -> OpenFOAM polyMesh, fix front/back -> empty (2D), checkMesh.

Backs the generate_mesh tool. snappy is unaffected; this is the robust path for
geometry-driven meshes where failures are catchable as metrics.
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

import config
import foam

PROJECT_DIR = str(Path(os.path.abspath(__file__)).parent)
QMIN = 0.01      # min SICN: catch near-degenerate/inverted cells (checkMesh is the final gate)

_JOB_TEMPLATE = '''import sys, json
sys.path.insert(0, {project!r})
import mesher
SIZE_MIN = {size_min}
SIZE_MAX = {size_max}
q = mesher.{builder}(".", SIZE_MIN, SIZE_MAX)
json.dump(q, open("quality.json", "w"))
print("n_elements", q.get("n_elements"), "min", q.get("min"))
'''


def _rescale_rule(lo, hi):
    """Heuristic: element_count out of band -> rescale SIZE_* (2D: count ~ 1/size^2)."""
    import caeloop as cl
    target = math.sqrt(lo * hi)

    def matches(res, rep):
        return any(c.name == "element_count" and not c.ok for c in rep.checks)

    def transform(src, res, rep):
        n = res.metrics.get("n_elements")
        if not n:
            return src
        scale = max(0.15, min(4.0, math.sqrt(n / target)))
        repl = lambda m: f"{m.group(1)}{float(m.group(2)) * scale:.5f}"
        src = re.sub(r"(SIZE_MIN\s*=\s*)([0-9.]+)", repl, src)
        src = re.sub(r"(SIZE_MAX\s*=\s*)([0-9.]+)", repl, src)
        return src

    return cl.RepairRule("rescale_mesh_size", matches, transform)


class ChainBackend:
    """First backend that returns a CHANGED source wins (heuristic rules, then LLM)."""
    def __init__(self, backends):
        self.backends = backends
        self.name = "chain"

    def propose_fix(self, source, result, report, ctx):
        for b in self.backends:
            try:
                new = b.propose_fix(source, result, report, ctx)
            except Exception:
                continue
            if new and new != source:
                self.name = f"chain:{getattr(b, 'name', '?')}"
                return new
        return source


def _ensure_case_skeleton(case: Path) -> None:
    (case / "constant").mkdir(parents=True, exist_ok=True)
    sysd = case / "system"
    sysd.mkdir(parents=True, exist_ok=True)
    cd = sysd / "controlDict"
    if not cd.is_file():
        cd.write_text("FoamFile{ format ascii; class dictionary; object controlDict; }\n"
                      "application foamRun; startFrom startTime; startTime 0; stopAt endTime;\n"
                      "endTime 1; deltaT 1; writeControl timeStep; writeInterval 1;\n")


def _set_empty(boundary_text: str, patches) -> str:
    for p in patches:
        boundary_text = re.sub(rf"(\b{re.escape(p)}\b\s*\{{[^}}]*?type\s+)patch(\s*;)",
                               r"\1empty\2", boundary_text, flags=re.S)
    return boundary_text


def generate(case_dir, geometry="cylinder_2d", target_cells=8000,
             size_min=0.3, size_max=2.0, max_repairs=4, use_llm=True,
             scaffold=True, empty_patches=("front", "back")) -> dict:
    import caeloop as cl
    from caeloop import validators as V

    builder = {"cylinder_2d": "mesh_cylinder_2d"}.get(geometry)
    if builder is None:
        return {"ok": False, "error": f"unknown geometry: {geometry}"}

    case = Path(case_dir)
    work = case / "meshWork"
    work.mkdir(parents=True, exist_ok=True)
    lo = max(2000, int(target_cells * 0.35))
    hi = int(target_cells * 4)

    job = work / "job.py"
    job.write_text(_JOB_TEMPLATE.format(project=PROJECT_DIR, builder=builder,
                                        size_min=size_min, size_max=size_max))

    backends = [cl.NullBackend([_rescale_rule(lo, hi)])]
    if use_llm:
        backends.append(cl.AnthropicBackend(model=config.MODEL))
    runner = cl.Runner(ChainBackend(backends), log_path=str(work / "run_log.jsonl"),
                       verbose=False)
    step = cl.Step(
        name="mesh", script=str(job), metrics_file="quality.json",
        timeout_s=600.0, max_repairs=max_repairs,
        validators=[
            V.process_succeeded(),
            V.no_stderr_signature(["Traceback (most recent call last)"]),
            V.metric_present("n_elements"),
            V.element_count_between(lo, hi),
            V.min_quality(QMIN, key="min"),
        ])
    ctx = {"task": f"mesh a {geometry} for OpenFOAM: want {lo}..{hi} cells, "
                   f"min SICN >= {QMIN}. Only adjust SIZE_MIN/SIZE_MAX."}
    oc = runner.run_step(step, ctx)

    last = (oc.history[-1]["metrics"] if oc.history else {}) or {}
    res = {"ok": False, "geometry": geometry, "attempts": oc.attempts,
           "n_elements": last.get("n_elements"), "min_sicn": last.get("min"),
           "size_min": last.get("size_min"), "size_max": last.get("size_max"),
           "work": str(work)}
    if not oc.ok:
        res["error"] = "메시 validation 실패 (repair 후에도)"
        res["failed_checks"] = [c.name for c in oc.report.failed]
        return res

    # scaffold a solvable case (0/, constant, system) from a verified template,
    # so the case is runnable right after meshing; else just a minimal skeleton.
    tmpl = Path(PROJECT_DIR) / "templates" / geometry
    if scaffold and tmpl.is_dir():
        import shutil
        shutil.copytree(tmpl, case, dirs_exist_ok=True)
        res["scaffolded"] = True
    else:
        _ensure_case_skeleton(case)
        res["scaffolded"] = False

    r = foam.run_foam("gmshToFoam", [str(work / "mesh.msh")], case=case, timeout=300)
    if not r.ok:
        res["error"] = "gmshToFoam 실패"
        res["log_tail"] = r.tail(500)
        return res

    bfile = case / "constant" / "polyMesh" / "boundary"
    if bfile.is_file() and empty_patches:
        bfile.write_text(_set_empty(bfile.read_text(), empty_patches))

    rc = foam.run_foam("checkMesh", case=case, timeout=300)
    mq = foam.parse_checkmesh(rc.stdout)
    res.update(ok=bool(mq.get("mesh_ok")), mesh_cells=mq.get("cells"),
               max_non_ortho=mq.get("max_non_ortho"), max_skewness=mq.get("max_skewness"),
               mesh_ok=mq.get("mesh_ok"), patches=last.get("patches"),
               empty_patches=list(empty_patches))
    return res


def generate_airfoil(case_dir, dat_path, aoa=0.0, chord=1.0, size_min=0.01, size_max=0.5,
                     scaffold=True) -> dict:
    """fetch_geometry(airfoil)로 받은 UIUC .dat을 2D gmsh로 메싱(airfoil 패치 + front/back empty)
    → gmshToFoam → checkMesh. scaffold면 풀 수 있는 케이스(0/·물성·schemes)까지. cylinder_2d의
    외형 형상판."""
    import shutil

    import mesher

    case = Path(case_dir)
    work = case / "meshWork"
    work.mkdir(parents=True, exist_ok=True)
    try:
        mq = mesher.mesh_airfoil_2d(str(work), float(size_min), float(size_max),
                                    dat_path=dat_path, chord=float(chord), aoa=float(aoa))
    except Exception as e:
        return {"ok": False, "error": f"airfoil mesh 실패: {type(e).__name__}: {e}"}

    tmpl = Path(PROJECT_DIR) / "templates" / "airfoil_2d"
    scaffolded = False
    if scaffold and tmpl.is_dir():
        shutil.copytree(tmpl, case, dirs_exist_ok=True)
        scaffolded = True
        # forceCoeffs로 양력/항력(Cl/Cd): 흐름 +x, lift +y, span(z) 1-cell. lRef=chord,
        # Aref=chord×thickness, CofR=quarter-chord. (parse_force_coeffs로 읽힘)
        cdf = case / "system" / "controlDict"
        if cdf.is_file() and "forceCoeffs" not in cdf.read_text():
            forces = (
                "\nfunctions\n{\n    forceCoeffs1\n    {\n"
                '        type forceCoeffs; libs ("libforces.so");\n'
                "        writeControl timeStep; timeInterval 1; log yes;\n"
                "        patches (airfoil); rho rhoInf; rhoInf 1;\n"
                "        liftDir (0 1 0); dragDir (1 0 0); pitchAxis (0 0 1);\n"
                f"        CofR ({0.25 * float(chord):.4f} 0 0); magUInf 1; "
                f"lRef {float(chord):.4f}; Aref {float(chord) * 1.0:.5f};\n"
                "    }\n}\n")
            cdf.write_text(cdf.read_text() + forces)
    else:
        _ensure_case_skeleton(case)

    r = foam.run_foam("gmshToFoam", [str(work / "mesh.msh")], case=case, timeout=300)
    if not r.ok:
        return {"ok": False, "error": "gmshToFoam 실패", "log_tail": r.tail(500)}
    bfile = case / "constant" / "polyMesh" / "boundary"
    if bfile.is_file():
        bfile.write_text(_set_empty(bfile.read_text(), ("front", "back")))
    rc = foam.run_foam("checkMesh", case=case, timeout=300)
    cm = foam.parse_checkmesh(rc.stdout)
    return {"ok": bool(cm.get("mesh_ok")), "geometry": "airfoil_2d", "aoa": float(aoa),
            "chord": float(chord), "n_elements": mq.get("n_elements"), "min_sicn": mq.get("min"),
            "mesh_cells": cm.get("cells"), "max_non_ortho": cm.get("max_non_ortho"),
            "max_skewness": cm.get("max_skewness"), "mesh_ok": cm.get("mesh_ok"),
            "patches": mq.get("patches"), "scaffolded": scaffolded, "work": str(work)}


# --- external-STL quality self-improve loop ---------------------------------
# Escalation policy from the motorBike studies (qexp.py / QUALITY_STUDY.md):
#   - opt passes:   NO-OP (p2==p8)                          -> never escalate
#   - grade=True:   HARMFUL (skew 3.8->8.9)                 -> never use
#   - algo Frontal(4): lowest skewness + mesh_ok            -> use when skew high
#   - wall smoothing (humphrey + re-close): the ONE lever that breaks the ~89° tet
#       non-ortho floor -> 89°→81° at only ~1.4% surface deviation. Coarser
#       decimation + more smoothing helps most (a fine surface keeps sharp edges).
#   - target_faces up: lifts sicn but RAISES non-ortho once smoothed -> sicn-only.
# Even smoothed, max non-ortho ~81° > RANS-ideal(<70): usable with
# nNonOrthogonalCorrectors; snappy(hex+layers) for true wall accuracy.

def _better(m, b):
    # prefer a valid mesh, then the lowest non-orthogonality (the quality goal),
    # then the lowest skewness.
    ka = (1 if m.get("mesh_ok") else 0, -(m.get("max_non_ortho") or 999.0), -(m.get("max_skewness") or 99.0))
    kb = (1 if b.get("mesh_ok") else 0, -(b.get("max_non_ortho") or 999.0), -(b.get("max_skewness") or 99.0))
    return ka > kb


def _dev_max(m):
    d = m.get("deviation") or {}
    return d.get("max_pct") or 0.0


def _next_external_config(cfg, m, skew_max, nonortho_max, sicn_min, max_dev_pct=2.0, faces_cap=60000):
    """Data-validated escalation: Frontal(skew) -> wall smoothing(non-ortho, within a
    deviation budget) -> more faces(sicn only). passes/grade excluded as no-op/harmful."""
    sicn = m.get("min_sicn")
    sicn = sicn if sicn is not None else -1.0
    nonortho = m.get("max_non_ortho") or 99.0
    if cfg["algo"] != 4:                                   # Frontal: lowest skewness
        nc = dict(cfg); nc["algo"] = 4; return nc
    if nonortho >= nonortho_max and _dev_max(m) < max_dev_pct:   # smoothing: validated non-ortho lever
        if cfg.get("smooth", 0) == 0:
            nc = dict(cfg); nc["smooth"] = 15; return nc
        if cfg["smooth"] < 50:
            nc = dict(cfg); nc["smooth"] = 50; return nc
    if sicn <= sicn_min and cfg.get("smooth", 0) == 0:    # faces lift sicn (but hurt non-ortho if smoothed)
        nf = int(cfg["target_faces"] * 1.5)
        if nf <= faces_cap and nf != cfg["target_faces"]:
            nc = dict(cfg); nc["target_faces"] = nf; return nc
    return None                                            # plateau — no validated knob left


def generate_external(case_dir, stl_path, target_faces=20000, size_max=None,
                      improve=True, max_rounds=5, max_deviation_pct=2.0,
                      skew_max=4.0, nonortho_max=70.0, sicn_min=1e-4) -> dict:
    """외부 STL/OBJ(예: motorBike)를 snappy 없이 gmsh로 메싱하며 **품질을 self-improve**.
    repair(watertight)+decimate → gmsh 외부 volume → checkMesh 한 라운드, 미달이면 data로
    검증된 knob을 적용해 재메시: skew↑→Frontal, non-ortho↑→**벽면 smoothing**(STL을 펴서
    boundary tet 개선; max_deviation_pct 예산 안에서, 변형량 측정·보고), sicn↓→faces↑.
    best를 추적·복원하고, 한계를 못 넘으면 snappy_suggested로 사용자에게 제안한다."""
    import shutil
    import mesher

    case = Path(case_dir)
    work = case / "meshWork"
    work.mkdir(parents=True, exist_ok=True)
    _ensure_case_skeleton(case)
    fixed = work / "body_fixed.stl"
    (work / "source.txt").write_text(str(stl_path))   # add_boundary_layers가 벽 재메싱에 사용
    sm = float(size_max) if size_max else None

    def attempt(cfg):
        try:
            rep = mesher.repair_to_watertight(stl_path, str(fixed),
                                              target_faces=cfg["target_faces"],
                                              smooth=cfg.get("smooth", 0))
        except Exception as e:
            return {"ok": False, "error": f"surface repair 실패: {type(e).__name__}: {e}", **cfg}
        if not rep.get("watertight"):
            return {"ok": False, "error": "repair 후 watertight 아님", "repaired_faces": rep.get("n_faces"), **cfg}
        try:
            mq = mesher.mesh_external_stl_3d(str(work), str(fixed), size_max=sm,
                                             algo=cfg["algo"], opt_passes=2, grade=False)
        except Exception as e:
            return {"ok": False, "error": f"gmsh mesh 실패: {type(e).__name__}: {e}",
                    "repaired_faces": rep.get("n_faces"), "deviation": rep.get("deviation"), **cfg}
        shutil.rmtree(case / "constant" / "polyMesh", ignore_errors=True)
        r = foam.run_foam("gmshToFoam", [str(work / "mesh.msh")], case=case, timeout=600)
        if not r.ok:
            return {"ok": False, "error": "gmshToFoam 실패", "repaired_faces": rep.get("n_faces"),
                    "n_elements": mq.get("n_elements"), "deviation": rep.get("deviation"), **cfg}
        rc = foam.run_foam("checkMesh", case=case, timeout=600)
        cm = foam.parse_checkmesh(rc.stdout)
        return {"ok": bool(cm.get("mesh_ok")), "repaired_faces": rep.get("n_faces"),
                "n_elements": mq.get("n_elements"), "min_sicn": mq.get("min"),
                "mesh_cells": cm.get("cells"), "max_non_ortho": cm.get("max_non_ortho"),
                "max_skewness": cm.get("max_skewness"), "mesh_ok": cm.get("mesh_ok"),
                "deviation": rep.get("deviation"), "patches": mq.get("patches"), **cfg}

    cfg = {"target_faces": int(target_faces), "algo": 10, "smooth": 0}
    history, best = [], None
    bestmsh = work / "best.msh"
    for i in range(max_rounds if improve else 1):
        m = attempt(cfg)
        m["round"] = i + 1
        history.append({k: m.get(k) for k in
                        ("round", "target_faces", "algo", "smooth", "repaired_faces", "mesh_cells",
                         "max_non_ortho", "max_skewness", "min_sicn", "deviation", "mesh_ok", "error")})
        if best is None or _better(m, best):
            best = m
            if m.get("n_elements") and (work / "mesh.msh").is_file():
                try:
                    shutil.copy(work / "mesh.msh", bestmsh)   # keep the winning .msh
                except Exception:
                    pass
        sicn = m.get("min_sicn")
        met = (m.get("mesh_ok") and (m.get("max_skewness") or 99) < skew_max
               and (m.get("max_non_ortho") or 99) < nonortho_max
               and (sicn if sicn is not None else -1) > sicn_min)
        if met:
            break
        nxt = _next_external_config(cfg, m, skew_max, nonortho_max, sicn_min, max_deviation_pct)
        if nxt is None:
            break
        cfg = nxt

    # the loop may have ended on a worse attempt — restore the BEST mesh into the case
    if best and best.get("mesh_ok") and bestmsh.is_file():
        shutil.rmtree(case / "constant" / "polyMesh", ignore_errors=True)
        foam.run_foam("gmshToFoam", [str(bestmsh)], case=case, timeout=600)

    nonortho = best.get("max_non_ortho") if best else None
    dev = (best or {}).get("deviation")
    quality_met = bool(best and best.get("mesh_ok")
                       and (best.get("max_non_ortho") or 99) < nonortho_max
                       and (best.get("max_skewness") or 99) < skew_max)
    snappy = (not quality_met) and ((nonortho is None) or (nonortho >= nonortho_max))
    res = {"ok": bool(best and best.get("mesh_ok")), "geometry": "external_stl",
           "improved": bool(improve), "rounds": len(history), "history": history,
           "quality_targets": {"skew_max": skew_max, "nonortho_max": nonortho_max,
                               "sicn_min": sicn_min, "max_deviation_pct": max_deviation_pct},
           "quality_met": quality_met, "nonortho_floor": nonortho,
           "stl_deviation": dev, "snappy_suggested": bool(snappy), "work": str(work)}
    if snappy:
        res["solver_hint"] = "fvSolution: nNonOrthogonalCorrectors 2~3 (high max non-ortho 보정)"
        dtxt = (f"STL을 max {dev.get('max_pct')}%/mean {dev.get('mean_pct')}% 변형(smoothing)"
                if dev else "벽면 smoothing")
        res["snappy_reason"] = (
            f"{dtxt}해 max_non_ortho를 89°→{nonortho}°까지 낮췄으나 RANS-ideal(<70°)은 못 넘음 — "
            "복잡 body의 thin feature(거울·번호판·스포크)가 만드는 boundary tet은 tet 메시의 본질적 "
            "한계다(knobs·polyDual·pyacvd·tetgen 등 모두 확인, QUALITY_STUDY.md). 현재 메시도 average "
            "non-ortho는 낮아 nNonOrthogonalCorrectors 2~3으로 solve 가능(못 쓰는 메시 아님). 정량 벽면 "
            "정확도(drag·Cf)가 필요하면 snappyHexMesh(hex+boundary layers) 전환을 권한다.")
    if best:
        for k in ("repaired_faces", "n_elements", "min_sicn", "mesh_cells", "max_non_ortho",
                  "max_skewness", "mesh_ok", "patches", "target_faces", "algo", "smooth"):
            res[k] = best.get(k)
    return res


def generate_external_netgen(case_dir, stl_path, n_layers=3, first_layer=None,
                             target_faces=40000, smooth=0) -> dict:
    """**Clean watertight 형상**용 BL 메시: STL→OCC solid(OCP sew)→netgen box−body를 prism
    boundary layer까지 한 번에 메싱→gmshToFoam→checkMesh. snappy 없이 깨끗한 BL(sphere 검증:
    non-ortho 51, skew 0.5, Mesh OK). 바이크처럼 thin feature 많은 dirty 형상은 netgen BL이
    깨지니 generate_external + add_boundary_layers(snappy)를 쓸 것."""
    import shutil

    import mesher

    case = Path(case_dir)
    work = case / "meshWork"
    work.mkdir(parents=True, exist_ok=True)
    fixed = work / "body_fixed.stl"
    try:
        rep = mesher.repair_to_watertight(stl_path, str(fixed), target_faces=int(target_faces),
                                          smooth=int(smooth))
    except Exception as e:
        return {"ok": False, "error": f"surface repair 실패: {type(e).__name__}: {e}"}
    if not rep.get("watertight"):
        return {"ok": False, "error": "repair 후 watertight 아님"}
    try:
        mq = mesher.mesh_external_netgen_bl(str(case), str(fixed), n_layers=int(n_layers),
                                            first_layer=first_layer)
    except Exception as e:
        return {"ok": False, "error": f"netgen BL 실패(thin/복잡 형상이면 snappy 경로 사용): "
                f"{type(e).__name__}: {e}", "geometry": "external_netgen"}
    _ensure_case_skeleton(case)
    shutil.rmtree(case / "constant" / "polyMesh", ignore_errors=True)
    r = foam.run_foam("gmshToFoam", [mq["msh"]], case=case, timeout=600)
    if not r.ok:
        return {"ok": False, "error": "gmshToFoam 실패", "log_tail": r.tail(500)}
    cm = foam.parse_checkmesh(foam.run_foam("checkMesh", case=case, timeout=600).stdout)
    return {"ok": bool(cm.get("mesh_ok")), "geometry": "external_netgen", "method": "netgen+OCP",
            "n_elements": mq.get("n_elements"), "n_prisms": mq.get("n_prisms"),
            "n_layers": mq.get("n_layers"), "mesh_cells": cm.get("cells"),
            "max_non_ortho": cm.get("max_non_ortho"), "max_skewness": cm.get("max_skewness"),
            "mesh_ok": cm.get("mesh_ok"), "patches": mq.get("patches"), "work": str(work)}


# --- 3D boundary layers: gmsh tet mesh + snappyHexMesh addLayers-only --------
# gmsh는 3D prism BL을 못 만든다(BoundaryLayer field는 2D 전용). 그래서 gmsh가 잡은
# 형상(constant/polyMesh) 위에 snappy의 addLayers 단계만 돌려 벽면 prism 층을 넣는다 —
# 외부유동 RANS 벽면 정확도(drag·Cf)에 필수. tet base라 quality 제약을 완화해야 layer가
# 삽입되며, thin feature(거울·스포크)에는 부분적으로만 깔린다(coverage% 보고).

_SNAPPY_LAYERS_DICT = """FoamFile{{ format ascii; class dictionary; object snappyHexMeshDict; }}
castellatedMesh false;
snap            false;
addLayers       true;
geometry {{ {patch} {{ type triSurfaceMesh; file "{patch}.stl"; }} }}
castellatedMeshControls
{{
    maxLocalCells 2000000; maxGlobalCells 8000000; minRefinementCells 0;
    nCellsBetweenLevels 1; features (); refinementSurfaces {{}} resolveFeatureAngle 30;
    refinementRegions {{}} locationInMesh ({locx} {locy} {locz}); allowFreeStandingZoneFaces true;
}}
snapControls {{ nSmoothPatch 3; tolerance 2.0; nSolveIter 30; nRelaxIter 5; }}
addLayersControls
{{
    relativeSizes false;
    layers {{ {patch} {{ nSurfaceLayers {n}; }} }}
    expansionRatio {er};
    firstLayerThickness {flt};
    minThickness {mt};
    nGrow 0; featureAngle 130; slipFeatureAngle 50; nRelaxIter 10;
    nSmoothSurfaceNormals 2; nSmoothNormals 5; nSmoothThickness 10;
    maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio 0.6; minMedianAxisAngle 90;
    nBufferCellsNoExtrude 0; nLayerIter 50; nRelaxedIter 20;
}}
meshQualityControls
{{
    maxNonOrtho 89; maxBoundarySkewness 25; maxInternalSkewness 6; maxConcave 90;
    minVol 1e-20; minTetQuality -1e30; minArea -1; minTwist 0.001; minDeterminant 0.0001;
    minFaceWeight 0.001; minVolRatio 0.001; minTriangleTwist -1; nSmoothScale 4; errorReduction 0.75;
}}
mergeTolerance 1e-6;
"""


def _parse_snappy_layers(out, patch):
    """snappy 로그의 layer 요약(patch faces avgLayers thickness coverage%)에서 평균 layer 수와
    coverage%를 뽑는다. 마지막 매칭(=최종 결과)을 사용."""
    pat = re.compile(rf"^\s*{re.escape(patch)}\s+\d+\s+([\d.]+)\s+[\d.eE+-]+\s+([\d.]+)\s*$")
    avg = cov = None
    for line in out.splitlines():
        m = pat.match(line)
        if m:
            avg, cov = float(m.group(1)), float(m.group(2))
    return avg, cov


def add_boundary_layers(case_dir, body_patch="body", n_layers=3, first_layer=None,
                        expansion=1.25, min_thickness=None, wall_faces=30000, wall_smooth=20) -> dict:
    """gmsh 외부 메시에 snappyHexMesh addLayers-only로 벽면 prism BL을 넣는다. **layer coverage는
    벽면 메시 해상도가 좌우**한다(실측: 20k면→67%, 30k면→78%). 그래서 wall_faces>0이고 원본이
    있으면 벽을 그 해상도+wall_smooth로 다시 메싱한 뒤 layer를 넣는다(non-ortho는 조금 오르나
    nNonOrthogonalCorrectors로 보정, skew는 유효 범위). wall_faces=0이면 기존 polyMesh 그대로.
    first_layer 미지정 시 body 대각선의 0.12%. 반환: layer coverage%·평균 layer 수·전후 cells/품질."""
    import shutil

    import numpy as np
    import trimesh

    import mesher

    case = Path(case_dir)
    work = case / "meshWork"
    bstl = work / "body_fixed.stl"
    src_file = work / "source.txt"

    # BL coverage를 위해 벽을 finer로 재메싱 (generate_external가 저장한 원본이 있을 때만)
    remeshed = None
    if wall_faces and src_file.is_file():
        src = src_file.read_text().strip()
        if Path(src).exists():
            try:
                rep = mesher.repair_to_watertight(src, str(bstl), target_faces=int(wall_faces),
                                                  smooth=int(wall_smooth))
                mesher.mesh_external_stl_3d(str(work), str(bstl), algo=4)
                shutil.rmtree(case / "constant" / "polyMesh", ignore_errors=True)
                r = foam.run_foam("gmshToFoam", [str(work / "mesh.msh")], case=case, timeout=600)
                if r.ok:
                    remeshed = {"wall_faces": int(wall_faces), "wall_smooth": int(wall_smooth),
                                "repaired_faces": rep.get("n_faces")}
            except Exception as e:
                remeshed = {"error": f"벽 재메싱 실패(기존 메시로 진행): {type(e).__name__}: {e}"}

    if not (case / "constant" / "polyMesh" / "boundary").is_file():
        return {"ok": False, "error": "polyMesh 없음 — generate_mesh(geometry=external_stl) 먼저 실행"}
    if not bstl.is_file():
        return {"ok": False, "error": f"body STL 없음: {bstl} (generate_mesh external_stl 산출물 필요)"}

    cm0 = foam.parse_checkmesh(foam.run_foam("checkMesh", case=case, timeout=600).stdout)

    _ensure_case_skeleton(case)
    sysd = case / "system"
    if not (sysd / "fvSchemes").is_file():
        (sysd / "fvSchemes").write_text(
            "FoamFile{format ascii;class dictionary;object fvSchemes;}\n"
            "ddtSchemes{default steadyState;}\ngradSchemes{default Gauss linear;}\n"
            "divSchemes{default none;}\nlaplacianSchemes{default Gauss linear corrected;}\n"
            "interpolationSchemes{default linear;}\nsnGradSchemes{default corrected;}\n")
    if not (sysd / "fvSolution").is_file():
        (sysd / "fvSolution").write_text(
            "FoamFile{format ascii;class dictionary;object fvSolution;}\nsolvers{}\n")

    tri = case / "constant" / "triSurface"
    tri.mkdir(parents=True, exist_ok=True)
    shutil.copy(str(bstl), str(tri / f"{body_patch}.stl"))

    b = trimesh.load(str(bstl), force="mesh")
    mn = np.asarray(b.vertices).min(0)
    mx = np.asarray(b.vertices).max(0)
    diag = float(np.linalg.norm(mx - mn)) or 1.0
    flt = float(first_layer) if first_layer else round(diag * 0.0012, 6)
    mt = float(min_thickness) if min_thickness else round(flt * 0.15, 7)
    loc = (float(mn[0] - (mx[0] - mn[0])), float((mn[1] + mx[1]) / 2.0), float((mn[2] + mx[2]) / 2.0))

    (sysd / "snappyHexMeshDict").write_text(_SNAPPY_LAYERS_DICT.format(
        patch=body_patch, n=int(n_layers), er=expansion, flt=flt, mt=mt,
        locx=loc[0], locy=loc[1], locz=loc[2]))

    rs = foam.run_foam("snappyHexMesh", ["-overwrite"], case=case, timeout=1200)
    if not rs.ok:
        return {"ok": False, "error": "snappyHexMesh addLayers 실패", "log_tail": rs.tail(800)}
    avg, cov = _parse_snappy_layers(rs.stdout, body_patch)
    cm1 = foam.parse_checkmesh(foam.run_foam("checkMesh", case=case, timeout=600).stdout)
    return {"ok": bool(cm1.get("mesh_ok")), "body_patch": body_patch,
            "n_layers_wanted": int(n_layers), "avg_layers": avg, "layer_coverage_pct": cov,
            "first_layer": flt, "expansion": float(expansion), "remeshed": remeshed,
            "cells_before": cm0.get("cells"), "cells_after": cm1.get("cells"),
            "max_non_ortho": cm1.get("max_non_ortho"), "max_skewness": cm1.get("max_skewness"),
            "mesh_ok": cm1.get("mesh_ok")}


# --- external RANS scaffold (kOmegaSST, foamRun) + drag (forceCoeffs) --------
# OF12 motorBikeSteady 튜토리얼 기반: foamRun + incompressibleFluid + kOmegaSST + SIMPLE.
# 우리 patch(inlet/outlet/bottom/farfield/body)에 맞춰 쓰고, gmsh 메시의 높은 non-ortho(~88)
# 때문에 SIMPLE nNonOrthogonalCorrectors를 0→3으로 올린다(튜토리얼 hex 메시는 0). lRef/Aref/CofR는
# body bbox에서 자동 산출. forceCoeffs functionObject가 매 iter Cd/Cl과 항력을 기록한다.

def _ff(obj, cls="dictionary"):
    return (f"FoamFile{{ version 2.0; format ascii; class {cls}; object {obj}; }}\n")


def scaffold_external_ras(case_dir, flow_velocity=20.0, nu=1.5e-5, turb_intensity=0.05,
                          end_time=500, nnonortho=3, turbulence="kOmegaSST",
                          residual_tol=1e-4) -> dict:
    """external(+BL) 케이스에 외부유동 RANS 셋업(0/·constant/·system/)을 깐다. turbulence로 난류
    모델 선택(laminar / kOmegaSST / kEpsilon / SpalartAllmaras) → 그에 맞는 필드·BC·wall function·
    div schemes·solvers 자동 생성. Re는 flow_velocity·nu로 정한다. body bbox로 lRef·Aref·CofR 자동,
    forceCoeffs로 drag/lift. 높은 non-ortho면 nnonortho를 올린다.

    수렴 제어(OF12 drivaerFastback/airFoil2D 방식): residual_tol>0이면 SIMPLE residualControl을
    써서 p·U·난류필드 초기잔차가 모두 residual_tol 밑으로 떨어지면 **자동으로 멈춘다**(수렴). 이때
    end_time은 '수렴 못 하면 여기서 끊는다'는 **상한(cap)**일 뿐 고정 반복수가 아니다. residual_tol=0이면
    옛 방식(고정 end_time 반복). cap·미수렴 대응 결정은 런타임에서 find_similar_runs(과거 수렴 run) +
    web_search로 한다."""
    import numpy as np
    import trimesh

    case = Path(case_dir)
    bdy = case / "constant" / "polyMesh" / "boundary"
    if not bdy.is_file():
        return {"ok": False, "error": "polyMesh 없음 — generate_mesh(external_stl) 먼저"}
    # nut/k/omega wall functions require the body patch to be type 'wall'
    # (gmshToFoam writes it as 'patch') — fix it in the boundary file.
    bdy.write_text(re.sub(r"(\bbody\b\s*\{[^}]*?type\s+)patch(\s*;)",
                          r"\1wall\2", bdy.read_text(), flags=re.S))
    bstl = case / "meshWork" / "body_fixed.stl"
    if not bstl.is_file():   # snappy 경로는 body를 constant/triSurface에 둔다(.eMesh 등 제외)
        hits = [h for h in sorted((case / "constant" / "triSurface").glob("body.*"))
                if h.suffix.lower() in (".stl", ".stlb", ".obj")]
        if hits:
            bstl = hits[0]
    U = float(flow_velocity)
    if bstl.is_file():
        b = trimesh.load(str(bstl), force="mesh")
        mn = np.asarray(b.vertices).min(0)
        mx = np.asarray(b.vertices).max(0)
        lref = float(mx[0] - mn[0])
        aref = float((mx[1] - mn[1]) * (mx[2] - mn[2]))
        cofr = ((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, mn[2])
    else:
        lref, aref, cofr = 1.0, 1.0, (0.0, 0.0, 0.0)
    # 난류 모델별 필드 세트 + freestream 값(intensity·length scale 0.07*lref 기반)
    MODELS = {"laminar": [], "kOmegaSST": ["k", "omega"], "kEpsilon": ["k", "epsilon"],
              "SpalartAllmaras": ["nuTilda"]}
    if turbulence not in MODELS:
        turbulence = "kOmegaSST"
    tfields = MODELS[turbulence]
    Lt = 0.07 * lref
    k = 1.5 * (turb_intensity * U) ** 2
    omega = (k ** 0.5) / (0.09 ** 0.25 * Lt)
    epsilon = (0.09 ** 0.75) * (k ** 1.5) / Lt
    nuTilda = 4.0 * nu
    SPEC = {"k": ("[0 2 -2 0 0 0 0]", k, "kqRWallFunction", None),
            "omega": ("[0 0 -1 0 0 0 0]", omega, "omegaWallFunction", None),
            "epsilon": ("[0 2 -3 0 0 0 0]", epsilon, "epsilonWallFunction", None),
            "nuTilda": ("[0 2 -1 0 0 0 0]", nuTilda, "fixedValue", "0")}
    Uvec = f"({U} 0 0)"

    (case / "0").mkdir(exist_ok=True)
    (case / "0" / "U").write_text(_ff("U", "volVectorField") +
        f"dimensions [0 1 -1 0 0 0 0];\ninternalField uniform {Uvec};\nboundaryField{{\n"
        f"    inlet    {{ type fixedValue; value uniform {Uvec}; }}\n"
        f"    outlet   {{ type inletOutlet; inletValue uniform (0 0 0); value uniform {Uvec}; }}\n"
        "    body     { type noSlip; }\n    bottom   { type slip; }\n    farfield { type slip; }\n}\n")
    (case / "0" / "p").write_text(_ff("p", "volScalarField") +
        "dimensions [0 2 -2 0 0 0 0];\ninternalField uniform 0;\nboundaryField{\n"
        "    inlet    { type zeroGradient; }\n    outlet   { type fixedValue; value uniform 0; }\n"
        "    body     { type zeroGradient; }\n    bottom   { type slip; }\n    farfield { type slip; }\n}\n")
    for f in tfields:
        dim, val, bc, bval = SPEC[f]
        bv = bval if bval is not None else f"{val:.6g}"
        (case / "0" / f).write_text(_ff(f, "volScalarField") +
            f"dimensions {dim};\ninternalField uniform {val:.6g};\nboundaryField{{\n"
            f"    inlet    {{ type fixedValue; value uniform {val:.6g}; }}\n"
            f"    outlet   {{ type inletOutlet; inletValue uniform {val:.6g}; value uniform {val:.6g}; }}\n"
            f"    body     {{ type {bc}; value uniform {bv}; }}\n"
            "    bottom   { type slip; }\n    farfield { type slip; }\n}\n")
    if turbulence != "laminar":
        nut_wf = "nutUSpaldingWallFunction" if turbulence == "SpalartAllmaras" else "nutkWallFunction"
        (case / "0" / "nut").write_text(_ff("nut", "volScalarField") +
            "dimensions [0 2 -1 0 0 0 0];\ninternalField uniform 0;\nboundaryField{\n"
            "    inlet    { type calculated; value uniform 0; }\n    outlet   { type calculated; value uniform 0; }\n"
            f"    body     {{ type {nut_wf}; value uniform 0; }}\n"
            "    bottom   { type calculated; value uniform 0; }\n    farfield { type calculated; value uniform 0; }\n}\n")

    (case / "constant").mkdir(exist_ok=True)
    mt = ("simulationType laminar;\n" if turbulence == "laminar"
          else f"simulationType RAS;\nRAS{{ model {turbulence}; turbulence on; printCoeffs on; }}\n")
    (case / "constant" / "momentumTransport").write_text(_ff("momentumTransport") + mt)
    (case / "constant" / "physicalProperties").write_text(_ff("physicalProperties") +
        f"viscosityModel constant;\nnu [0 2 -1 0 0 0 0] {nu:g};\n")

    sysd = case / "system"
    sysd.mkdir(exist_ok=True)
    forces = (
        "functions\n{\n    forceCoeffs1\n    {\n        type forceCoeffs; libs (\"libforces.so\");\n"
        "        writeControl timeStep; timeInterval 1; log yes;\n        patches (body);\n"
        f"        rho rhoInf; rhoInf 1;\n        liftDir (0 0 1); dragDir (1 0 0); pitchAxis (0 1 0);\n"
        f"        CofR ({cofr[0]:.4f} {cofr[1]:.4f} {cofr[2]:.4f});\n"
        f"        magUInf {U}; lRef {lref:.4f}; Aref {aref:.5f};\n    }}\n}}\n")
    (sysd / "controlDict").write_text(_ff("controlDict") +
        "application foamRun;\nsolver incompressibleFluid;\nstartFrom startTime; startTime 0;\n"
        f"stopAt endTime; endTime {int(end_time)}; deltaT 1;\nwriteControl timeStep; writeInterval {int(end_time)};\n"
        "purgeWrite 2; writeFormat binary; writePrecision 6; writeCompression off;\n"
        "timeFormat general; timePrecision 6; runTimeModifiable true;\n" + forces)
    # high-non-ortho 메시: limited laplacian/snGrad이 non-ortho 보정을 cap, div(phi,U) upwind로 안정.
    div_turb = "".join(f"    div(phi,{f}) bounded Gauss upwind;\n" for f in tfields)
    (sysd / "fvSchemes").write_text(_ff("fvSchemes") +
        "ddtSchemes{ default steadyState; }\n"
        "gradSchemes{ default Gauss linear; grad(U) cellLimited Gauss linear 1; }\n"
        "divSchemes{ default none; div(phi,U) bounded Gauss upwind;\n" + div_turb +
        "    div((nuEff*dev2(T(grad(U))))) Gauss linear; }\n"
        "laplacianSchemes{ default Gauss linear limited 0.2; }\ninterpolationSchemes{ default linear; }\n"
        "snGradSchemes{ default limited 0.2; }\nwallDist{ method meshWave; }\n")
    turb_solvers = "".join(
        f"    {f} {{ solver smoothSolver; smoother GaussSeidel; tolerance 1e-8; relTol 0.1; nSweeps 1; }}\n"
        for f in tfields)
    relax_turb = "".join(f" {f} 0.5;" for f in tfields)
    # residualControl(OF12 drivaerFastback 방식): p·U·난류필드 초기잔차가 모두 residual_tol 밑이면
    # SIMPLE이 endTime 전에 '수렴'으로 멈춘다. residual_tol=0이면 블록 생략(고정 반복).
    rc = ""
    if residual_tol and residual_tol > 0:
        rc_fields = "".join(f"        {f} {residual_tol:g};\n" for f in (["p", "U"] + tfields))
        rc = f"    residualControl\n    {{\n{rc_fields}    }}\n"
    (sysd / "fvSolution").write_text(_ff("fvSolution") +
        "solvers{\n    p { solver GAMG; smoother GaussSeidel; tolerance 1e-7; relTol 0.01; }\n"
        "    Phi { $p; }\n"
        "    U { solver smoothSolver; smoother GaussSeidel; tolerance 1e-8; relTol 0.1; nSweeps 1; }\n"
        + turb_solvers + "}\n"
        "SIMPLE\n{\n"
        f"    nNonOrthogonalCorrectors {int(nnonortho)};\n    consistent no;\n"
        + rc + "}\n"
        "potentialFlow{ nNonOrthogonalCorrectors 10; }\n"
        f"relaxationFactors{{ fields{{ p 0.3; }} equations{{ U 0.5;{relax_turb} }} }}\ncache{{ grad(U); }}\n")

    return {"ok": True, "flow_velocity": U, "nu": nu, "Re_per_L": round(U / nu, 1),
            "turbulence": turbulence, "turb_fields": tfields, "k": round(k, 5),
            "omega": round(omega, 4), "epsilon": round(epsilon, 4), "nuTilda": round(nuTilda, 8),
            "lRef": round(lref, 4), "Aref": round(aref, 5), "CofR": [round(c, 4) for c in cofr],
            "nNonOrthogonalCorrectors": int(nnonortho),
            "residual_tol": residual_tol, "end_time_cap": int(end_time),
            "convergence": ("residualControl(auto-stop) cap=%d" % int(end_time)
                            if residual_tol and residual_tol > 0 else "fixed %d iters" % int(end_time)),
            "solver": "incompressibleFluid"}


def parse_force_coeffs(case_dir):
    """postProcessing/forceCoeffs1/<t>/coefficient.dat(또는 forceCoeffs.dat)에서 최신 Cd/Cl을
    읽는다. 헤더의 컬럼명으로 Cd/Cl 위치를 찾는다."""
    base = Path(case_dir) / "postProcessing" / "forceCoeffs1"
    if not base.is_dir():
        return None
    dat = None
    for t in sorted(base.iterdir(), key=lambda p: p.name):
        for name in ("coefficient.dat", "forceCoeffs.dat"):
            if (t / name).is_file():
                dat = t / name
    if not dat:
        return None
    cols, last = None, None
    for line in dat.read_text().splitlines():
        if line.startswith("#"):
            toks = line.lstrip("#").split()
            if "Cd" in toks or "Cm" in toks or "Cl" in toks:
                cols = toks
        elif line.strip():
            last = line.split()
    if not last:
        return None
    out = {"iters": int(float(last[0])) if last else None}
    if cols and len(cols) == len(last):
        idx = {c: i for i, c in enumerate(cols)}
        for key in ("Cd", "Cl", "Cm"):
            if key in idx:
                out[key] = float(last[idx[key]])
    else:  # fallback: OF order time Cd Cs Cl CmRoll CmPitch CmYaw Cd(f) ...
        try:
            out["Cd"] = float(last[1])
            out["Cl"] = float(last[3])
        except Exception:
            pass
    return out


def parse_convergence(case_dir, log_name="log.foamRun", log_text=None):
    """수렴 여부 판별. residualControl이 걸리면 foamRun이 endTime(cap) 전에 멈추므로:
    (1) 로그에서 'SIMPLE solution converged in N iterations'를 찾고, (2) 최신 time dir < cap이면
    조기 종료(=수렴)로 본다. log_text가 주어지면(run_solver의 stdout) 그걸 쓰고, 없으면 log 파일을
    읽는다. 반환: {converged, diverged, final_time, end_time_cap, converged_iters, last_residuals}.
    converged_iters는 다음 run의 cap을 DB·web으로 정할 때 쓰는 핵심 수치."""
    case = Path(case_dir)
    cap = None
    cd = case / "system" / "controlDict"
    if cd.is_file():
        m = re.search(r"\bendTime\s+([0-9.eE+\-]+)\s*;", cd.read_text())
        if m:
            try:
                cap = int(float(m.group(1)))
            except Exception:
                cap = None
    times = []
    for p in case.iterdir():
        try:
            times.append(float(p.name))
        except (ValueError, OSError):
            continue
    final_time = int(max(times)) if times else None
    text = log_text
    if text is None:
        for name in (log_name, "log.foamRun", "log.foamRun.txt"):
            if (case / name).is_file():
                text = (case / name).read_text(errors="replace")
                break
    converged_iters, diverged, last_res = None, False, {}
    if text is not None:
        m = re.search(r"solution converged in (\d+) iterations", text)
        if m:
            converged_iters = int(m.group(1))
        # 실제 발산 시그니처(시작 배너 'sigFpe : Enabling …'는 제외): handler 발화·FATAL·OS 신호·
        # stack dump·NaN/inf 잔차. 고-Courant 발산은 run_solver의 foam.parse_solver_log가 추가로 잡는다.
        if re.search(r"FOAM FATAL|::sigHandler|Floating point exception"
                     r"|error::printStack|Initial residual = (?:nan|inf)", text):
            diverged = True
        # 현재 run의 최종 시각은 로그의 마지막 'Time = N'에서 — 디렉토리 max는 이전 run의 stale dir을
        # 주울 수 있다('s' 접미 무시). 로그가 있으면 이게 우선.
        tmatches = re.findall(r"(?m)^Time = ([0-9.eE+\-]+)", text)
        if tmatches:
            try:
                final_time = int(float(tmatches[-1]))
            except Exception:
                pass
        for fld in ("p", "Ux", "Uy", "Uz", "k", "omega", "epsilon", "nuTilda"):
            mm = re.findall(r"Solving for " + re.escape(fld)
                            + r",\s*Initial residual = ([0-9.eE+\-]+)", text)
            if mm:
                last_res[fld] = float(mm[-1])
    # converged는 OF의 명시적 'solution converged in N iterations' 메시지로만 인정한다. cap 전에
    # 멈춘 것(final_time<cap)은 수렴의 충분조건이 아니다 — timeout·kill·발산도 cap 전에 멈추므로 이는
    # stopped_early 진단 플래그로만 보고하고, run_solver가 clean-exit·foam diverged와 교차검증한다.
    stopped_early = bool(cap is not None and final_time is not None and 0 < final_time < cap)
    converged = bool(converged_iters)
    return {"converged": converged, "diverged": diverged, "stopped_early": stopped_early,
            "final_time": final_time, "end_time_cap": cap,
            "converged_iters": converged_iters,
            "last_residuals": last_res or None}


# --- rotating machinery (MRF): steady Multiple Reference Frame on a rotor cellZone ----
# OF12 mixerVessel2DMRF(MRFProperties 문법) + propeller(forces·cellZone) 튜토리얼 기반. 회전메시
# (transient AMI) 대신 steady SIMPLE로 회전영역(cellZone)에 frame 회전을 줘 thrust·torque를 싸게 푼다.

def scaffold_mrf(case_dir, cellzone="innerCylinder", axis=(0, 1, 0), origin=(0, 0, 0),
                 rpm=1500.0, flow_velocity=5.0, nu=1e-6, turbulence="kEpsilon",
                 rotor_patches="propeller.*", residual_tol=1e-4, end_time=2000,
                 nnonortho=2) -> dict:
    """rotating machinery(MRF) 셋업: constant/MRFProperties로 회전 cellZone을 정의하고 회전메시 대신
    steady SIMPLE(foamRun+incompressibleFluid)로 푼다. dynamicMeshDict 제거, 회전벽 patch는 noSlip
    (MRF는 회전을 frame으로 처리). forces functionObject로 축방향 thrust·torque 기록 → power=τ·ω.
    residualControl로 수렴 시 자동 정지(end_time=cap). OF12 mixerVessel2DMRF + propeller 기반."""
    import math
    case = Path(case_dir)
    cz = case / "constant" / "polyMesh" / "cellZones"
    if not cz.is_file():
        return {"ok": False, "error": "cellZones 없음 — MRF는 회전 cellZone이 필요(snappy로 생성)"}
    if cellzone not in cz.read_text(errors="replace"):
        return {"ok": False, "error": f"cellZone '{cellzone}'가 메시에 없음(cellZones 파일 확인)"}
    ax = tuple(float(x) for x in axis)
    org = tuple(float(x) for x in origin)
    omega = float(rpm) * 2.0 * math.pi / 60.0  # rad/s
    (case / "constant" / "dynamicMeshDict").unlink(missing_ok=True)   # MRF ≠ moving mesh
    (case / "constant" / "MRFProperties").write_text(_ff("MRFProperties") +
        "MRF\n{\n"
        f"    cellZone    {cellzone};\n"
        f"    origin      ({org[0]} {org[1]} {org[2]});\n"
        f"    axis        ({ax[0]} {ax[1]} {ax[2]});\n"
        f"    omega       {float(rpm)} [rpm];\n"
        "}\n")
    # 회전벽은 **MRFnoSlip**(MRF 프레임과 함께 회전)이라야 한다 — 그냥 noSlip은 절대프레임 정지벽이라
    # 블레이드가 안 돌고 drag만 난다(실측 검증). rotor_patches 블록 type을 MRFnoSlip으로 강제,
    # moving-mesh 잔여 BC도 변환. (OF12 mixerVessel2DMRF: rotor=MRFnoSlip, stator=noSlip)
    u = case / "0" / "U"
    if u.is_file():
        txt = u.read_text().replace("movingWallVelocity", "MRFnoSlip") \
                           .replace("movingWallSlipVelocity", "slip")
        base = re.sub(r'["\.\*\s]+', "", str(rotor_patches)) or "rotor"
        txt = re.sub(r'("?' + re.escape(base) + r'[\w.*]*"?\s*\{[^{}]*?type\s+)(?:noSlip|MRFnoSlip)',
                     r"\1MRFnoSlip", txt, flags=re.S)
        u.write_text(txt)
    MODELS = {"laminar": [], "kOmegaSST": ["k", "omega"], "kEpsilon": ["k", "epsilon"],
              "SpalartAllmaras": ["nuTilda"]}
    if turbulence not in MODELS:
        turbulence = "kEpsilon"
    tfields = MODELS[turbulence]
    mt = ("simulationType laminar;\n" if turbulence == "laminar"
          else f"simulationType RAS;\nRAS{{ model {turbulence}; turbulence on; printCoeffs on; }}\n")
    (case / "constant" / "momentumTransport").write_text(_ff("momentumTransport") + mt)
    (case / "constant" / "physicalProperties").write_text(_ff("physicalProperties") +
        f"viscosityModel constant;\nnu [0 2 -1 0 0 0 0] {nu:g};\n")
    sysd = case / "system"
    sysd.mkdir(exist_ok=True)
    # OF12 foamRun은 system/functions를 자동 로드하되, 거기엔 #include 지시만 인식한다(직접 dict 정의는
    # 무시됨 — 실측). 그래서 검증된 튜토리얼 방식: forces 정의는 system/forces에 쓰고 system/functions가
    # 그걸 #include 한다. 튜토리얼 transient용 잔여(surfaces·Q)는 제거. regex patch는 반드시 따옴표.
    (sysd / "surfaces").unlink(missing_ok=True)
    (sysd / "Q").unlink(missing_ok=True)
    (sysd / "forces").write_text(
        "forces\n{\n    type forces; libs (\"libforces.so\");\n"
        "    writeControl timeStep; timeInterval 1; log yes;\n"
        f"    patches (\"{rotor_patches}\");\n    rho rhoInf; rhoInf 1;\n"
        f"    CofR ({org[0]} {org[1]} {org[2]}); pitchAxis ({ax[0]} {ax[1]} {ax[2]});\n}}\n")
    (sysd / "functions").write_text(_ff("functions") + "#include \"forces\"\n")
    (sysd / "controlDict").write_text(_ff("controlDict") +
        "application foamRun;\nsolver incompressibleFluid;\nstartFrom startTime; startTime 0;\n"
        f"stopAt endTime; endTime {int(end_time)}; deltaT 1;\nwriteControl timeStep; writeInterval {int(end_time)};\n"
        "purgeWrite 2; writeFormat binary; writePrecision 6; writeCompression off;\n"
        "timeFormat general; timePrecision 6; runTimeModifiable true;\n")
    # forces는 위에서 쓴 system/functions(OF12 자동로드)로 돈다 — controlDict엔 functions 불필요.
    div_turb = "".join(f"    div(phi,{f}) bounded Gauss upwind;\n" for f in tfields)
    (sysd / "fvSchemes").write_text(_ff("fvSchemes") +
        "ddtSchemes{ default steadyState; }\n"
        "gradSchemes{ default Gauss linear; grad(U) cellLimited Gauss linear 1; }\n"
        "divSchemes{ default none; div(phi,U) bounded Gauss upwind;\n" + div_turb +
        "    div((nuEff*dev2(T(grad(U))))) Gauss linear; }\n"
        "laplacianSchemes{ default Gauss linear limited 0.33; }\ninterpolationSchemes{ default linear; }\n"
        "snGradSchemes{ default limited 0.33; }\nwallDist{ method meshWave; }\n")
    turb_solvers = "".join(
        f"    {f} {{ solver smoothSolver; smoother GaussSeidel; tolerance 1e-8; relTol 0.1; nSweeps 1; }}\n"
        for f in tfields)
    relax_turb = "".join(f" {f} 0.5;" for f in tfields)
    rc = ""
    if residual_tol and residual_tol > 0:
        rc_fields = "".join(f"        {f} {residual_tol:g};\n" for f in (["p", "U"] + tfields))
        rc = f"    residualControl\n    {{\n{rc_fields}    }}\n"
    (sysd / "fvSolution").write_text(_ff("fvSolution") +
        "solvers{\n    p { solver GAMG; smoother GaussSeidel; tolerance 1e-7; relTol 0.01; }\n"
        "    U { solver smoothSolver; smoother GaussSeidel; tolerance 1e-8; relTol 0.1; nSweeps 1; }\n"
        + turb_solvers + "}\n"
        "SIMPLE\n{\n"
        f"    nNonOrthogonalCorrectors {int(nnonortho)};\n    consistent no;\n    pRefCell 0;\n    pRefValue 0;\n"
        + rc + "}\n"
        f"relaxationFactors{{ fields{{ p 0.3; }} equations{{ U 0.5;{relax_turb} }} }}\n")
    return {"ok": True, "method": "MRF", "cellZone": cellzone, "axis": list(ax), "origin": list(org),
            "rpm": float(rpm), "omega_rad_s": round(omega, 4), "turbulence": turbulence,
            "turb_fields": tfields, "nu": nu, "flow_velocity": float(flow_velocity),
            "rotor_patches": rotor_patches, "residual_tol": residual_tol,
            "end_time_cap": int(end_time), "solver": "incompressibleFluid"}


def _floats(line):
    return [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", line)]


def _sum_triplets(vals):
    """평탄화된 float 목록을 (x,y,z) triplet으로 묶어 elementwise 합. OF forces.dat의
    forces(pressure viscous [porous]) 분해를 합산해 총 force/moment 벡터를 만든다."""
    v = [0.0, 0.0, 0.0]
    for i in range(0, len(vals) - len(vals) % 3, 3):
        v[0] += vals[i]; v[1] += vals[i + 1]; v[2] += vals[i + 2]
    return v


def parse_forces(case_dir, axis=(0, 1, 0), omega_rad_s=None, func=None):
    """postProcessing의 forces functionObject 출력에서 최신 force·moment를 읽어 축방향 thrust·torque,
    power(=|torque·omega|)를 낸다. OF12 forces.dat 포맷: `Time forces((p)(v)) moments((p)(v))` —
    force/moment 각각 pressure+viscous(+porous)를 합산. func 미지정 시 postProcessing 아래에서
    forces.dat(또는 force.dat)를 자동 탐색. axis는 단위벡터로 정규화. omega_rad_s 주면 power 계산."""
    import math
    pp = Path(case_dir) / "postProcessing"
    if not pp.is_dir():
        return None
    ax = [float(x) for x in axis]
    nrm = math.sqrt(sum(a * a for a in ax)) or 1.0
    ax = [a / nrm for a in ax]
    # find latest forces.dat (combined) — auto-discover function dir
    dats = sorted(pp.glob(f"{func}/*/forces.dat") if func else pp.glob("*/*/forces.dat"))
    split = sorted(pp.glob(f"{func}/*/force.dat") if func else pp.glob("*/*/force.dat"))

    def last_data(path):
        last = None
        for ln in path.read_text().splitlines():
            if ln.strip() and not ln.lstrip().startswith("#"):
                last = ln
        return _floats(last) if last else None

    force = moment = it = None
    if dats:                                  # combined forces.dat: time + force-part + moment-part
        v = last_data(dats[-1])
        if v and len(v) >= 7:
            it = int(v[0]); rest = v[1:]; half = len(rest) // 2
            force = _sum_triplets(rest[:half]); moment = _sum_triplets(rest[half:])
    elif split:                               # newer split force.dat / moment.dat
        fv = last_data(split[-1]); md = split[-1].parent / "moment.dat"
        mv = last_data(md) if md.is_file() else None
        if fv and len(fv) >= 4:
            it = int(fv[0]); force = _sum_triplets(fv[1:])
        if mv and len(mv) >= 4:
            moment = _sum_triplets(mv[1:])
    if force is None or moment is None:
        return None
    thrust = sum(f * a for f, a in zip(force, ax))
    torque = sum(m * a for m, a in zip(moment, ax))
    out = {"iters": it, "axis": ax, "force": [round(f, 5) for f in force],
           "moment": [round(m, 6) for m in moment],
           "thrust": round(thrust, 5), "torque": round(torque, 6)}
    if omega_rad_s:
        out["power_W"] = round(abs(torque) * float(omega_rad_s), 4)
        out["omega_rad_s"] = round(float(omega_rad_s), 4)
        if abs(thrust) > 1e-9:
            out["fom_figureOfMerit_note"] = "static thrust; compare Ct/Cp to UIUC PDB"
    return out


# --- generic full snappyHexMesh: any STL -> hex-dominant + BL (quantitative aero) ----
# OF12 motorBikeSteady 튜토리얼의 검증된 control 값을 STL bbox로 일반화. blockMesh 배경 박스 +
# surfaceFeatures + snappy(castellate+snap+addLayers, parallel). 결과 패치 inlet/outlet/bottom/
# farfield/body(wall) → scaffold_external_ras로 바로 solve 가능. dirty STL도 ray-cast라 OK.

def _blockmesh_dict(mn, mx):
    L, W, H = (mx - mn)
    bx0, bx1 = mn[0] - 4 * L, mx[0] + 8 * L
    by0, by1 = mn[1] - 4 * W, mx[1] + 4 * W
    bz0, bz1 = mn[2] - 3 * H, mx[2] + 4 * H
    cs = max(L, W, H) / 5.0
    nx = max(6, round((bx1 - bx0) / cs))
    ny = max(6, round((by1 - by0) / cs))
    nz = max(6, round((bz1 - bz0) / cs))
    v = [(bx0, by0, bz0), (bx1, by0, bz0), (bx1, by1, bz0), (bx0, by1, bz0),
         (bx0, by0, bz1), (bx1, by0, bz1), (bx1, by1, bz1), (bx0, by1, bz1)]
    vstr = "\n".join(f"    ({x:.5f} {y:.5f} {z:.5f})" for x, y, z in v)
    txt = ("FoamFile{ format ascii; class dictionary; object blockMeshDict; }\n"
           "convertToMeters 1;\nvertices\n(\n" + vstr + "\n);\n"
           f"blocks ( hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1) );\nedges ();\n"
           "boundary\n(\n"
           "    inlet    { type patch; faces ((0 4 7 3)); }\n"
           "    outlet   { type patch; faces ((1 2 6 5)); }\n"
           "    bottom   { type patch; faces ((0 3 2 1)); }\n"
           "    farfield { type patch; faces ((4 5 6 7) (0 1 5 4) (3 7 6 2)); }\n"
           ");\nmergePatchPairs ();\n")
    return txt, (float(bx0 + 0.37 * L), float((by0 + by1) / 2), float((bz0 + bz1) / 2))


def run_snappy_external(case_dir, stl_path, refine=(2, 4), n_layers=3, nprocs=None) -> dict:
    """임의 STL → **full snappyHexMesh**(castellate+snap+addLayers) → hex-dominant + prism BL.
    blockMesh 배경박스(STL bbox) + surfaceFeatures + snappy(parallel) → reconstructParMesh →
    checkMesh. 정량 외부유동(drag)용 — gmsh-tet보다 품질 우수, dirty STL도 ray-cast라 견딤.
    결과 패치: inlet/outlet/bottom/farfield/body(wall). scaffold_external_ras로 바로 solve."""
    import shutil

    import numpy as np
    import trimesh

    case = Path(case_dir)
    sysd = case / "system"
    tri = case / "constant" / "triSurface"
    sysd.mkdir(parents=True, exist_ok=True)
    tri.mkdir(parents=True, exist_ok=True)
    _ensure_case_skeleton(case)
    # OBJ의 named region들은 snappy에서 패치를 region별로 쪼갠다(motorBike→70개). trimesh로
    # 단일 mesh로 병합해 STL로 내보내면 region이 사라져 snappy가 'body' 한 패치만 만든다.
    b = trimesh.load(str(stl_path), force="mesh")
    surf = "body.stl"
    b.export(str(tri / surf))
    mn = np.asarray(b.vertices).min(0)
    mx = np.asarray(b.vertices).max(0)
    bm, loc = _blockmesh_dict(mn, mx)
    (sysd / "blockMeshDict").write_text(bm)
    (sysd / "surfaceFeaturesDict").write_text(
        "FoamFile{ format ascii; class dictionary; object surfaceFeaturesDict; }\n"
        f'surfaces ( "{surf}" );\nincludedAngle 150;\n')
    rmin, rmax = int(refine[0]), int(refine[1])
    (sysd / "snappyHexMeshDict").write_text(
        "FoamFile{ format ascii; class dictionary; object snappyHexMeshDict; }\n"
        "castellatedMesh true; snap true; addLayers true;\n"
        f'geometry {{ body {{ type triSurfaceMesh; file "{surf}"; }} }}\n'
        "castellatedMeshControls\n{\n"
        "    maxLocalCells 2000000; maxGlobalCells 8000000; minRefinementCells 10;\n"
        "    maxLoadUnbalance 0.10; nCellsBetweenLevels 3;\n"
        '    features ( { file "body.eMesh"; level 1; } );\n'
        f"    refinementSurfaces {{ body {{ level ({rmin} {rmax}); patchInfo {{ type wall; }} }} }}\n"
        "    resolveFeatureAngle 30; refinementRegions {}\n"
        f"    locationInMesh ({loc[0]:.4f} {loc[1]:.4f} {loc[2]:.4f}); allowFreeStandingZoneFaces true;\n"
        "}\n"
        "snapControls\n{\n"
        "    nSmoothPatch 3; tolerance 2.0; nSolveIter 30; nRelaxIter 5;\n"
        "    nFeatureSnapIter 10; implicitFeatureSnap false; explicitFeatureSnap true;\n"
        "    multiRegionFeatureSnap false;\n}\n"
        "addLayersControls\n{\n"
        "    relativeSizes true;\n"
        f"    layers {{ body {{ nSurfaceLayers {int(n_layers)}; }} }}\n"
        "    expansionRatio 1.2; finalLayerThickness 0.4; minThickness 0.1;\n"
        "    nGrow 0; featureAngle 130; slipFeatureAngle 30; nRelaxIter 5;\n"
        "    nSmoothSurfaceNormals 1; nSmoothNormals 3; nSmoothThickness 10;\n"
        "    maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio 0.3; minMedianAxisAngle 90;\n"
        "    nBufferCellsNoExtrude 0; nLayerIter 50; nRelaxedIter 20;\n}\n"
        "meshQualityControls\n{\n"
        '    #includeEtc "caseDicts/mesh/generation/meshQualityDict"\n'
        "    nSmoothScale 4; errorReduction 0.75;\n}\n"
        "writeFlags ( scalarLevels layerSets layerFields );\nmergeTolerance 1e-6;\n")

    n = int(nprocs) if nprocs else min(6, config.MAX_RANKS)
    foam.write_decompose_par_dict(case, n)
    steps = []
    rf = foam.run_foam("surfaceFeatures", case=case, timeout=600)
    steps.append(("surfaceFeatures", rf.ok))
    rb = foam.run_foam("blockMesh", case=case, timeout=600)
    steps.append(("blockMesh", rb.ok))
    if not rb.ok:
        return {"ok": False, "error": "blockMesh 실패", "steps": steps, "log_tail": rb.tail(500)}
    foam.run_foam("decomposePar", ["-force"], case=case, timeout=600)
    rs = foam.run_foam("mpirun", ["-np", str(n), "snappyHexMesh", "-overwrite", "-parallel"],
                       case=case, timeout=2400, log=True, log_name="snappyHexMesh")
    steps.append((f"snappyHexMesh(parallel {n})", rs.ok))
    # OF12: reconstructParMesh는 reconstructPar로 통합됨(메시 자동 재구성)
    foam.run_foam("reconstructPar", ["-constant"], case=case, timeout=900)
    rc = foam.run_foam("checkMesh", case=case, timeout=900)
    cm = foam.parse_checkmesh(rc.stdout)
    return {"ok": bool(cm.get("mesh_ok")), "geometry": "snappy_external", "method": "snappyHexMesh_full",
            "nprocs": n, "refine": [rmin, rmax], "n_layers": int(n_layers), "steps": steps,
            "mesh_cells": cm.get("cells"), "max_non_ortho": cm.get("max_non_ortho"),
            "max_skewness": cm.get("max_skewness"), "mesh_ok": cm.get("mesh_ok"),
            "patches": ["inlet", "outlet", "bottom", "farfield", "body"], "work": str(case)}
