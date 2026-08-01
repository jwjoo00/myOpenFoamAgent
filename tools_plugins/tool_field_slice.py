"""Plugin: field_slice
Visualise OpenFOAM ASCII volume fields on a mid-plane (|y|<slab) slice by
parsing the cell-centre field C and the requested fields directly (no OF
utilities, so it is safe inside the confined plugin sandbox). Designed for
DDES/LES statistics: |UMean| and UPrime2Mean (Reynolds stresses).

Requires that the latest time directory holds ASCII fields and a 'C' field
(cell centres). Generate C by reconstructing after a run with writeFormat
ascii (foamRun writes C automatically, or use 'postProcess -func writeCellCentres').
"""
import os
import re
import glob


SPEC = {
    "name": "field_slice",
    "description": (
        "Mid-plane (|y|<slab) contour of OpenFOAM ASCII volume fields by "
        "parsing cell-centre field C + the field directly (no OF utils; "
        "sandbox-safe). For DDES/LES stats: |UMean| and UPrime2Mean (Reynolds "
        "stresses). Needs an ASCII time dir with a 'C' field. [approval required]"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "case_dir": {"type": "string"},
            "time": {"type": "string", "description": "time dir (default: latest numeric)"},
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "fields to plot (default UMean,UPrime2Mean,pMean)",
            },
            "slab": {"type": "number", "description": "half-thickness of y-slab (m, default 0.02)"},
            "out_png": {"type": "string"},
        },
        "required": ["case_dir"],
    },
}

GATED = {"field_slice"}


def _latest_time(case_dir):
    times = []
    for d in os.listdir(case_dir):
        full = os.path.join(case_dir, d)
        if os.path.isdir(full):
            try:
                times.append((float(d), d))
            except ValueError:
                pass
    if not times:
        return None
    times.sort()
    return times[-1][1]


def _read_internal(path):
    """Parse an OpenFOAM ASCII field's internalField nonuniform List.
    Returns list of scalars or list of tuples (for vector/tensor)."""
    with open(path) as f:
        txt = f.read()
    m = re.search(r"internalField\s+nonuniform\s+List<(\w+)>\s*\n?\s*(\d+)\s*\n\(", txt)
    if not m:
        # uniform field
        mu = re.search(r"internalField\s+uniform\s+([^;]+);", txt)
        if mu:
            return ("uniform", mu.group(1).strip())
        return None
    kind = m.group(1)
    n = int(m.group(2))
    start = m.end()  # just after '('
    # collect until matching ')'
    depth = 1
    i = start
    body_chars = []
    while i < len(txt) and depth > 0:
        c = txt[i]
        if c == "(":
            depth += 1
            body_chars.append(c)
        elif c == ")":
            depth -= 1
            if depth > 0:
                body_chars.append(c)
        else:
            body_chars.append(c)
        i += 1
    body = "".join(body_chars)
    vals = []
    if kind == "scalar":
        for tok in body.split():
            try:
                vals.append(float(tok))
            except ValueError:
                pass
    else:
        # vector '(a b c)' or symmTensor '(xx xy xz yy yz zz)'
        for grp in re.findall(r"\(([^()]*)\)", body):
            nums = [float(x) for x in grp.split()]
            vals.append(tuple(nums))
    return (kind, n, vals)


def handler(**args):
    case_dir = args["case_dir"]
    if not os.path.isdir(case_dir):
        return {"ok": False, "error": "case_dir not found"}

    t = args.get("time") or _latest_time(case_dir)
    if not t:
        return {"ok": False, "error": "no time directory found"}
    tdir = os.path.join(case_dir, t)

    cpath = os.path.join(tdir, "C")
    if not os.path.exists(cpath):
        return {"ok": False, "error": "cell-centre field C not found in %s (need ASCII C)" % tdir}
    C = _read_internal(cpath)
    if not C or C[0] == "uniform":
        return {"ok": False, "error": "could not parse C field"}
    _, nC, cc = C  # cc: list of (x,y,z)

    fields = args.get("fields") or ["UMean", "UPrime2Mean", "pMean"]
    slab = float(args.get("slab", 0.02))

    # cell-centre y for slab mask
    xs = [p[0] for p in cc]
    ys = [p[1] for p in cc]
    zs = [p[2] for p in cc]

    panels = []  # (title, scalar_values_full)
    for fn in fields:
        fp = os.path.join(tdir, fn)
        if not os.path.exists(fp):
            continue
        F = _read_internal(fp)
        if not F or F[0] == "uniform":
            continue
        kind = F[0]
        vals = F[2]
        if len(vals) != nC:
            continue
        if kind == "scalar":
            panels.append((fn, vals))
        elif fn == "UMean" or kind == "vector":
            mag = [(_v[0] ** 2 + _v[1] ** 2 + _v[2] ** 2) ** 0.5 for _v in vals]
            panels.append(("|%s|" % fn, mag))
        elif fn == "UPrime2Mean" or kind == "symmTensor":
            # xx = streamwise normal Reynolds stress; also TKE = 0.5(xx+yy+zz)
            xx = [_v[0] for _v in vals]
            tke = [0.5 * (_v[0] + _v[3] + _v[5]) for _v in vals]
            panels.append(("%s_xx (u'u')" % fn, xx))
            panels.append(("TKE_res 0.5(uu+vv+ww)", tke))

    if not panels:
        return {"ok": False, "error": "no plottable fields among %s in %s" % (fields, tdir)}

    # slab mask
    idx = [i for i in range(nC) if abs(ys[i]) <= slab]
    if len(idx) < 50:
        # widen if too thin
        slab2 = slab
        while len(idx) < 50 and slab2 < 0.2:
            slab2 *= 1.5
            idx = [i for i in range(nC) if abs(ys[i]) <= slab2]
        slab = slab2
    px = [xs[i] for i in idx]
    pz = [zs[i] for i in idx]

    out_png = args.get("out_png") or os.path.join(case_dir, "report", "stats_slice.png")
    try:
        os.makedirs(os.path.dirname(out_png), exist_ok=True)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.tri as mtri

        npan = len(panels)
        fig, axes = plt.subplots(npan, 1, figsize=(11, 3.0 * npan), squeeze=False)
        tri = mtri.Triangulation(px, pz)
        for k, (title, full) in enumerate(panels):
            ax = axes[k][0]
            v = [full[i] for i in idx]
            # robust colour limits
            sv = sorted(v)
            lo = sv[int(0.02 * len(sv))]
            hi = sv[int(0.98 * len(sv)) - 1]
            if hi <= lo:
                hi = lo + 1e-9
            tcf = ax.tricontourf(tri, v, levels=40, vmin=lo, vmax=hi, cmap="turbo")
            fig.colorbar(tcf, ax=ax, shrink=0.9)
            ax.set_title("%s  (mid-plane |y|<%.3g)" % (title, slab), fontsize=10)
            ax.set_xlabel("x [m]"); ax.set_ylabel("z [m]")
            ax.set_aspect("equal")
            # focus on body+near wake
            ax.set_xlim(-0.5, 3.5); ax.set_ylim(-0.05, 0.8)
        fig.suptitle("Ahmed body DDES — time-averaged statistics (t=%s)" % t, fontsize=12)
        fig.tight_layout()
        fig.savefig(out_png, dpi=130)
        plt.close(fig)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "panels": [p[0] for p in panels], "n_slab_cells": len(idx)}

    return {
        "ok": True,
        "time": t,
        "png": out_png,
        "panels": [p[0] for p in panels],
        "n_cells_total": nC,
        "n_slab_cells": len(idx),
        "slab": slab,
    }
