"""
postproc.py — headless post-processing & report (Phase 2). No GUI, no pyvista.

Reuses the projectOpenFoam approach: parse the solver log for residual convergence
and read OpenFOAM ASCII fields for a velocity contour (matplotlib tricontourf). The
build_report() entry assembles residuals.png + velocity.png + REPORT.md for a run.

Needs matplotlib + numpy (the project venv has them). Import is lazy where heavy.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

import config
import foam
import registry


# --------------------------------------------------- OpenFOAM ASCII field reader
def read_field(path: str | Path) -> np.ndarray | float:
    """Read an OpenFOAM internalField -> numpy array ((N,) scalar or (N,3) vector),
    or a python float/array for a 'uniform' field."""
    txt = Path(path).read_text()
    m = re.search(r"internalField\s+(uniform|nonuniform)\b", txt)
    if not m:
        raise ValueError(f"no internalField in {path}")
    if m.group(1) == "uniform":
        mu = re.search(r"internalField\s+uniform\s+(\([^)]*\)|\S+?)\s*;", txt)
        v = mu.group(1)
        if v.startswith("("):
            return np.array([float(x) for x in v.strip("()").split()])
        return float(v)
    mt = re.search(r"nonuniform\s+List<(\w+)>\s*(\d+)\s*\((.*?)\)\s*;", txt, re.S)
    if not mt:
        raise ValueError(f"cannot parse nonuniform list in {path}")
    typ, body = mt.group(1), mt.group(3)
    if typ == "vector":
        rows = re.findall(r"\(([^)]*)\)", body)
        return np.array([[float(x) for x in r.split()] for r in rows])
    return np.array([float(x) for x in body.split()])


# ------------------------------------------------------------ residual parsing
def parse_residuals(log_text: str) -> dict:
    """{field: [(time, initial_residual), ...]} using the FIRST initial residual
    per time step (the outer-loop value), for Ux/Uy/p/k/epsilon/..."""
    cur = None
    seen: set = set()
    data: dict = {}
    for line in log_text.splitlines():
        mt = re.match(r"^Time = ([0-9.eE+-]+)", line)
        if mt:
            cur = float(mt.group(1))
            seen = set()
            continue
        mr = re.search(r"Solving for (\w+), Initial residual = ([0-9.eE+-]+)", line)
        if mr and cur is not None:
            fld = mr.group(1)
            if fld in seen:
                continue
            seen.add(fld)
            data.setdefault(fld, []).append((cur, float(mr.group(2))))
    return data


def plot_residuals(data: dict, out_png: str | Path,
                   title: str = "residual convergence") -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 5))
    for fld, pairs in data.items():
        t = [p[0] for p in pairs]
        r = [max(p[1], 1e-20) for p in pairs]
        ax.semilogy(t, r, lw=1.0, label=fld)
    ax.set_xlabel("time [s]")
    ax.set_ylabel("initial residual")
    ax.set_title(title)
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8, ncol=2)
    plt.tight_layout()
    plt.savefig(out_png, dpi=120)
    plt.close(fig)
    return Path(out_png)


# --------------------------------------------------------------- velocity field
def plot_velocity_field(case_dir: str | Path, out_png: str | Path,
                        title: str = "velocity field") -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.tri as mtri

    case = Path(case_dir)
    times = foam.list_time_dirs(case)
    if not times:
        raise FileNotFoundError("no solver time directories")
    tdir = times[-1]
    if not (case / tdir / "C").is_file():
        foam.run_foam("postProcess", ["-func", "writeCellCentres", "-latestTime"],
                      case=case, timeout=120, log=False)
    C = read_field(case / tdir / "C")
    U = read_field(case / tdir / "U")
    x, y = C[:, 0], C[:, 1]
    umag = np.hypot(U[:, 0], U[:, 1])

    tri = mtri.Triangulation(x, y)
    fig, ax = plt.subplots(figsize=(6.6, 6.0))
    cs = ax.tricontourf(tri, umag, levels=30, cmap="viridis")
    step = max(1, len(x) // 250)
    ax.quiver(x[::step], y[::step], U[::step, 0], U[::step, 1],
              color="white", alpha=0.8, width=0.003)
    ax.set_aspect("equal")
    ax.set_xlabel("x [m]")
    ax.set_ylabel("y [m]")
    ax.set_title(f"{title}  (t = {tdir} s)")
    fig.colorbar(cs, ax=ax, shrink=0.85, label="|U| [m/s]")
    plt.tight_layout()
    plt.savefig(out_png, dpi=120)
    plt.close(fig)
    return Path(out_png)


# ------------------------------------------------------------------- report
def _gather_meta(case: Path) -> dict:
    """Authoritative settings/results read straight from the case files + solver
    log, so the report is complete even when there is no registry record."""
    meta = {
        "solver": foam.get_dict_entry(case, "system/controlDict", "solver"),
        "turb_model": foam.get_dict_entry(case, "constant/momentumTransport", "model"),
        "end_time": foam.get_dict_entry(case, "system/controlDict", "endTime"),
        "nu": foam.get_dict_entry(case, "constant/physicalProperties", "nu"),
    }
    log = case / "log.foamRun"
    if log.is_file():
        sl = foam.parse_solver_log(log.read_text())
        meta["last_time"] = sl["last_time"]
        meta["max_courant"] = sl["max_courant"]
        meta["diverged"] = sl["diverged"]
    return meta


def _render_md(run_id: str, data: dict, mesh: dict, artifacts: dict) -> str:
    g = data.get
    lines = [f"# CFD run report — `{run_id}`", ""]
    lines += ["## 설정 (settings)", "",
              "| 항목 | 값 |", "|---|---|",
              f"| case_kind | {g('case_kind')} |",
              f"| solver | {g('solver')} |",
              f"| turbulence | {g('turb_model')} |",
              f"| viscosity nu | {g('nu')} |",
              f"| endTime | {g('end_time')} |",
              f"| geometry | {g('geometry_src')} |", ""]
    lines += ["## 격자 품질 (checkMesh)", "",
              "| 지표 | 값 |", "|---|---|",
              f"| cells | {mesh.get('cells') or g('mesh_cells')} |",
              f"| max non-orthogonality | {mesh.get('max_non_ortho')} |",
              f"| max skewness | {mesh.get('max_skewness')} |",
              f"| Mesh OK | {mesh.get('mesh_ok')} |", ""]
    lines += ["## 실행 결과 (run)", "",
              "| 항목 | 값 |", "|---|---|",
              f"| reached time | {g('last_time')} |",
              f"| max Courant | {g('max_courant')} |",
              f"| diverged | {g('diverged')} |",
              f"| converged | {g('converged')} |",
              f"| wall time [s] | {g('wall_time')} |", ""]
    if artifacts.get("residuals_png"):
        lines += ["## 수렴 (residuals)", "",
                  f"![residuals]({Path(artifacts['residuals_png']).name})", ""]
    if artifacts.get("velocity_png"):
        lines += ["## 속도장 (velocity)", "",
                  f"![velocity]({Path(artifacts['velocity_png']).name})", ""]
    elif artifacts.get("velocity_error"):
        lines += ["## 속도장", "", f"_(생략: {artifacts['velocity_error']})_", ""]
    return "\n".join(lines)


def build_report(case_dir: str | Path, out_dir: str | Path | None = None) -> dict:
    """Assemble residuals.png + velocity.png + REPORT.md for a finished run."""
    case = Path(case_dir)
    if not case.is_dir():
        return {"ok": False, "error": f"case not found: {case}"}
    run_id = case.name
    # reports go to the project's reports/<run_id>/ (Dropbox-synced), NOT next to
    # the heavy case data on ext4.
    out = Path(out_dir) if out_dir else config.REPORTS_ROOT / run_id
    out.mkdir(parents=True, exist_ok=True)
    artifacts: dict = {}

    log = case / "log.foamRun"
    if log.is_file():
        data = parse_residuals(log.read_text())
        if data:
            artifacts["residuals_png"] = str(
                plot_residuals(data, out / "residuals.png", title=f"{run_id} — residuals"))

    try:
        artifacts["velocity_png"] = str(
            plot_velocity_field(case, out / "velocity.png", title=f"{run_id} — velocity"))
    except Exception as e:
        artifacts["velocity_error"] = f"{type(e).__name__}: {e}"

    rec = registry.get_run(run_id) or {}
    res = rec.get("results") or {}
    meta = _gather_meta(case)
    mesh = {}
    cm = case / "log.checkMesh"
    if cm.is_file():
        mesh = foam.parse_checkmesh(cm.read_text())

    def _pref(a, b):
        return a if a is not None else b

    data = {
        "case_kind": rec.get("case_kind") or "(tutorial copy)",
        "geometry_src": rec.get("geometry_src"),
        "solver": _pref(meta.get("solver"), rec.get("solver")),
        "turb_model": _pref(meta.get("turb_model"), rec.get("turb_model")),
        "end_time": _pref(meta.get("end_time"), rec.get("end_time")),
        "nu": meta.get("nu"),
        "last_time": _pref(meta.get("last_time"), res.get("last_time")),
        "max_courant": _pref(meta.get("max_courant"), res.get("max_courant")),
        "diverged": _pref(meta.get("diverged"), res.get("diverged")),
        "converged": rec.get("converged"),
        "wall_time": rec.get("wall_time"),
        "mesh_cells": rec.get("mesh_cells"),
    }
    md = _render_md(run_id, data, mesh, artifacts)
    (out / "REPORT.md").write_text(md)
    artifacts["report_md"] = str(out / "REPORT.md")
    return {"ok": True, "run_id": run_id, "out_dir": str(out), "artifacts": artifacts}
