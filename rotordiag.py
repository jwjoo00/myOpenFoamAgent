"""
rotordiag.py — human-judgment visual diagnostic report for rotating-machinery (MRF) cases.

Rotating machinery (propeller/fan/impeller) CFD needs a human in the loop: the thrust is a
small net force (lift - drag) that's easy to get wrong via geometry/mesh. This builds a
multi-panel PNG + markdown so a person can SEE and judge: blade airfoil cross-sections,
flow direction vs axis, rotation/torque direction, thrust sign, propeller-vs-windmill mode.
"""
from __future__ import annotations

import math
import re
from pathlib import Path


def _read_mrf(case):
    f = case / "constant" / "MRFProperties"
    out = {"axis": (0, 1, 0), "omega_rpm": None, "cellzone": None}
    if f.is_file():
        t = f.read_text()
        m = re.search(r"axis\s*\(([^)]+)\)", t)
        if m:
            out["axis"] = tuple(float(x) for x in m.group(1).split())
        m = re.search(r"omega\s+([-0-9.eE]+)\s*\[rpm\]", t)
        if m:
            out["omega_rpm"] = float(m.group(1))
        m = re.search(r"cellZone\s+(\w+)", t)
        if m:
            out["cellzone"] = m.group(1)
    return out


def _read_inlet_U(case):
    f = case / "0" / "U"
    if not f.is_file():
        return None
    m = re.search(r"inlet\s*\{[^}]*?uniform\s*\(([^)]+)\)", f.read_text(), re.S)
    return tuple(float(x) for x in m.group(1).split()) if m else None


def _find_prop_stl(case):
    for d in (case / "constant" / "geometry", case / "constant" / "triSurface"):
        if d.is_dir():
            for p in sorted(d.iterdir()):
                if p.suffix.lower() in (".stl", ".obj") and "zone" not in p.name.lower() \
                        and "cylinder" not in p.name.lower():
                    return p
    return None


def make_rotor_report(case_dir, out_png=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import trimesh

    case = Path(case_dir)
    mrf = _read_mrf(case)
    inU = _read_inlet_U(case)
    axis = np.array(mrf["axis"], float)
    axis = axis / (np.linalg.norm(axis) or 1)
    omega_rpm = mrf["omega_rpm"]
    omega = (omega_rpm or 0) * 2 * math.pi / 60.0

    # forces / convergence
    try:
        import meshgen
        fc = meshgen.parse_forces(str(case), axis=tuple(axis), omega_rad_s=omega)
        conv = meshgen.parse_convergence(str(case))
    except Exception:
        fc, conv = None, {}
    thrust = fc.get("thrust") if fc else None
    torque = fc.get("torque") if fc else None
    # mode: shaft power = -torque*omega  (>0 propeller absorbs, <0 windmill drives)
    mode = "?"
    if torque is not None and omega:
        mode = "PROPELLER (shaft drives flow)" if (-torque * omega) > 0 else "WINDMILL (flow drives rotor)"

    stl = _find_prop_stl(case)
    m = trimesh.load(str(stl), force="mesh") if stl else None

    fig = plt.figure(figsize=(15, 9))

    # ---- Panel 1: geometry 3D ----
    ax1 = fig.add_subplot(2, 3, 1, projection="3d")
    if m is not None:
        v, f = m.vertices, m.faces
        ax1.plot_trisurf(v[:, 0], v[:, 2], v[:, 1], triangles=f, color="#9fb2c8", edgecolor="none")
        try:
            ax1.set_box_aspect((1, 1, 0.6))
        except Exception:
            pass
    ax1.set_title("1. Geometry (rotor)")
    ax1.set_xlabel("x"); ax1.set_ylabel("z"); ax1.set_zlabel("axis")

    # ---- Panels 2-3: blade cross-sections (airfoil shape) at radial stations ----
    # unwrap: for vertices near radius r, plot (tangential arc, axial). axis assumed ~Y.
    if m is not None:
        v = m.vertices
        # radial coordinate perpendicular to axis
        proj = v - np.outer(v.dot(axis), axis)
        rad = np.linalg.norm(proj, axis=1)
        axco = v.dot(axis)
        Rmax = rad.max()
        # tangential angle in plane perpendicular to axis (use x,z if axis~y)
        ang = np.arctan2(v[:, 2], v[:, 0])
        for pi, rR in enumerate((0.5, 0.75)):
            axp = fig.add_subplot(2, 3, 2 + pi)
            r0 = rR * Rmax
            band = np.abs(rad - r0) < 0.02 * Rmax
            if band.sum() > 5:
                a = ang[band]
                # pick the densest blade (cluster near the modal angle)
                amod = np.median(a)
                sel = band & (np.abs(((ang - amod + np.pi) % (2 * np.pi)) - np.pi) < 0.6)
                tang = r0 * np.unwrap(np.sort(ang[sel])) if sel.sum() > 5 else r0 * a
                axp.scatter((ang[sel] - amod) * r0, axco[sel], s=6, c="#3a6ea5")
                axp.set_aspect("equal", adjustable="datalim")
            axp.set_title(f"{2+pi}. Blade section r/R={rR:.2f} (airfoil?)")
            axp.set_xlabel("tangential (m)"); axp.set_ylabel("axial (m)")
            axp.grid(alpha=0.3)

    # ---- Panel 4: flow + rotation schematic ----
    ax4 = fig.add_subplot(2, 3, 4)
    ax4.set_xlim(-1, 1); ax4.set_ylim(-1.2, 1.2); ax4.axis("off")
    ax4.set_title("4. Flow & rotation (judge directions)")
    ax4.plot([0, 0], [-1, 1], "k--", lw=1)                    # axis
    ax4.text(0.05, 1.02, f"axis {tuple(round(x,1) for x in axis)}", fontsize=8)
    # inflow arrow (sign of inlet U along axis)
    uax = float(np.dot(inU, axis)) if inU else 0.0
    yin = 0.9 if uax < 0 else -0.9
    ax4.annotate("", xy=(0, yin - 0.5 * np.sign(uax or 1)), xytext=(0, yin),
                 arrowprops=dict(arrowstyle="-|>", color="#1f77b4", lw=2.5))
    ax4.text(0.1, yin, f"inflow Va={abs(uax):.2f} m/s\n(axial: {'aligned' if abs(np.dot((np.array(inU) if inU else axis)/(np.linalg.norm(inU) or 1), axis))>0.95 else 'NOT aligned!'})",
             fontsize=8, color="#1f77b4", va="center")
    # rotation arrow (curved) + sign
    th = np.linspace(0.3, 2.6, 40) * (1 if (omega_rpm or 0) >= 0 else -1)
    ax4.plot(0.35 * np.cos(th), 0.35 * np.sin(th), color="#d62728", lw=2)
    ax4.annotate("", xy=(0.35 * np.cos(th[-1]), 0.35 * np.sin(th[-1])),
                 xytext=(0.35 * np.cos(th[-3]), 0.35 * np.sin(th[-3])),
                 arrowprops=dict(arrowstyle="-|>", color="#d62728", lw=2))
    ax4.text(-0.95, -0.05, f"omega={omega_rpm} rpm", fontsize=8, color="#d62728")
    # thrust + torque direction from forces
    if thrust is not None:
        ydir = np.sign(thrust)
        ax4.annotate("", xy=(0.0, 0.0 + 0.4 * ydir), xytext=(0, 0),
                     arrowprops=dict(arrowstyle="-|>", color="green", lw=2.5))
        ax4.text(0.05, 0.4 * ydir, f"thrust {'+axis(fwd)' if thrust>0 else '-axis(rev/drag)'}",
                 fontsize=8, color="green")

    # ---- Panel 5/6: performance + judgment summary ----
    ax5 = fig.add_subplot(2, 3, 5); ax5.axis("off"); ax5.set_title("5. Performance / convergence")
    flow_aligned = (inU is not None and abs(np.dot(np.array(inU) / (np.linalg.norm(inU) or 1), axis)) > 0.95)
    lines = [
        f"cellZone : {mrf['cellzone']}",
        f"omega    : {omega_rpm} rpm  ({omega:.1f} rad/s)",
        f"inflow U : {inU}   axial-aligned: {flow_aligned}",
        f"thrust(Fy): {thrust}  -> {'forward(+)' if (thrust or 0)>0 else 'reverse/drag(-)'}",
        f"torque(My): {torque}",
        f"mode     : {mode}",
        f"converged: {conv.get('converged')} @ {conv.get('converged_iters')} iters"
        f"  diverged: {conv.get('diverged')}",
    ]
    if fc and fc.get("Cd") is None:
        pass
    ax5.text(0.0, 0.95, "\n".join(lines), fontsize=10, family="monospace", va="top")

    ax6 = fig.add_subplot(2, 3, 6); ax6.axis("off"); ax6.set_title("6. Human-judgment checklist")
    checks = [
        ("Flow aligned with axis?", flow_aligned),
        ("Rotating (omega set)?", bool(omega_rpm)),
        ("Thrust forward (+axis)?", (thrust or 0) > 0),
        ("Propeller mode (shaft drives)?", mode.startswith("PROPELLER")),
        ("Converged?", bool(conv.get("converged"))),
    ]
    txt = "\n".join(f"  [{'OK' if ok else '!!'}]  {q}" for q, ok in checks)
    txt += ("\n\n  NOTE: thrust=small net (lift-drag).\n  If thrust is -ve/drag-dominated despite\n"
            "  correct dims+rotation -> mesh likely\n  under-resolves blade lift (need y+~1\n  boundary layers + fine surface).")
    ax6.text(0.0, 0.95, txt, fontsize=9, family="monospace", va="top")

    fig.suptitle(f"Rotating-machinery diagnostic — {case.name}", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    png = Path(out_png) if out_png else (case / "report" / "rotor_diagnostic.png")
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, dpi=95)
    plt.close(fig)

    # markdown summary
    md = png.with_suffix(".md")
    md.write_text(
        f"# Rotating-machinery diagnostic — {case.name}\n\n"
        f"![diagnostic]({png.name})\n\n"
        f"| item | value |\n|---|---|\n"
        f"| cellZone | {mrf['cellzone']} |\n| omega | {omega_rpm} rpm |\n"
        f"| inflow U | {inU} (axial-aligned: {flow_aligned}) |\n"
        f"| thrust Fy | {thrust} ({'forward' if (thrust or 0)>0 else 'reverse/drag'}) |\n"
        f"| torque My | {torque} |\n| mode | {mode} |\n"
        f"| converged | {conv.get('converged')} @ {conv.get('converged_iters')} |\n\n"
        "**Judgment notes:** rotating machinery needs human review. Check the blade cross-section "
        "panels look like proper airfoils, the flow is axial, rotation direction is intended, and "
        "thrust/torque signs are physical. A drag-dominated (reverse) thrust with correct geometry "
        "usually means the mesh under-resolves blade lift (add boundary layers + fine refinement).\n")
    return {"ok": True, "png": str(png), "md": str(md), "cellzone": mrf["cellzone"],
            "omega_rpm": omega_rpm, "inflow_U": list(inU) if inU else None,
            "flow_axial_aligned": bool(flow_aligned), "thrust": thrust, "torque": torque,
            "mode": mode, "converged": conv.get("converged")}
