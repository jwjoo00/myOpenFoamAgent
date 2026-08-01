"""Plugin: force_stats
Time-average aerodynamic force coefficients from an OpenFOAM forceCoeffs
functionObject output (postProcessing/<name>/<t0>/forceCoeffs.dat), over a
user window [t_start, t_end]. Reports mean/std/min/max of Cd, Cl, Cm and
(optionally) compares Cd/Cl against reference values. Also writes a Cd/Cl
time-history PNG. Pure file parsing + matplotlib (no OF utilities), so it is
safe to run inside the confined plugin sandbox.
"""
import os
import glob
import json

SPEC = {
    "name": "force_stats",
    "description": (
        "Time-average Cd/Cl/Cm from an OpenFOAM forceCoeffs.dat over a window "
        "[t_start,t_end] (mean/std/min/max), optional comparison to reference "
        "Cd/Cl, plus a Cd/Cl time-history PNG. Use after a transient run "
        "(DDES/LES) to extract statistics. [approval required]"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "case_dir": {"type": "string"},
            "dat": {
                "type": "string",
                "description": "path to forceCoeffs.dat (auto-find if omitted)",
            },
            "t_start": {"type": "number", "description": "averaging window start time"},
            "t_end": {"type": "number", "description": "window end (0=last)"},
            "cd_ref": {"type": "number", "description": "reference Cd (optional)"},
            "cl_ref": {"type": "number", "description": "reference Cl (optional)"},
            "out_png": {"type": "string", "description": "output PNG path (optional)"},
        },
        "required": ["case_dir"],
    },
}

GATED = {"force_stats"}


def _find_dats(case_dir):
    """Return ALL forceCoeffs.dat across restart start-time folders, sorted by
    their numeric start-time so they can be concatenated in time order."""
    pats = [
        os.path.join(case_dir, "postProcessing", "*", "*", "forceCoeffs.dat"),
        os.path.join(case_dir, "postProcessing", "*", "*", "coefficient.dat"),
    ]
    found = []
    for p in pats:
        found.extend(glob.glob(p))

    def _startt(path):
        # .../<name>/<startTime>/forceCoeffs.dat
        try:
            return float(os.path.basename(os.path.dirname(path)))
        except ValueError:
            return 0.0

    found = sorted(set(found), key=_startt)
    return found


def _find_dat(case_dir):
    d = _find_dats(case_dir)
    return d[0] if d else None


def _parse(dat):
    """Return (header_cols, rows) where rows are list of float lists."""
    cols = None
    rows = []
    with open(dat) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                # last comment line with multiple tokens is the column header
                toks = line.lstrip("#").split()
                if len(toks) >= 3 and "Time" in toks:
                    cols = toks
                continue
            parts = line.replace("\t", " ").split()
            try:
                vals = [float(x) for x in parts]
            except ValueError:
                continue
            rows.append(vals)
    return cols, rows


def _col_index(cols, name):
    if not cols:
        return None
    for i, c in enumerate(cols):
        if c == name:
            return i
    return None


def _stats(xs):
    n = len(xs)
    if n == 0:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n if n > 1 else 0.0
    return {
        "mean": m,
        "std": var ** 0.5,
        "min": min(xs),
        "max": max(xs),
        "n": n,
    }


def handler(**args):
    case_dir = args["case_dir"]
    if args.get("dat"):
        dats = [args["dat"]]
    else:
        dats = _find_dats(case_dir)
    dats = [d for d in dats if d and os.path.exists(d)]
    if not dats:
        return {"ok": False, "error": "forceCoeffs.dat not found under %s" % case_dir}

    t_start = float(args.get("t_start", 0.0))
    t_end = float(args.get("t_end", 0.0))  # 0 => last
    cd_ref = args.get("cd_ref")
    cl_ref = args.get("cl_ref")

    # merge all restart .dat files in time order; on restart overlap, a later
    # file's rows override earlier ones at the same time
    cols = None
    merged = {}  # time -> row
    for d in dats:
        c, rws = _parse(d)
        if c and not cols:
            cols = c
        for r in rws:
            if r:
                merged[r[0]] = r
    rows = [merged[k] for k in sorted(merged.keys())]
    dat = dats[0]
    if not rows:
        return {"ok": False, "error": "no numeric rows parsed from %s" % dats}

    # column layout: typically Time Cm Cd Cl Cl(f) Cl(r)
    it = _col_index(cols, "Time") or 0
    icd = _col_index(cols, "Cd")
    icl = _col_index(cols, "Cl")
    icm = _col_index(cols, "Cm")
    # fallback to canonical positions if header parse failed
    if icd is None:
        icd = 2
    if icl is None:
        icl = 3
    if icm is None:
        icm = 1

    tmax = rows[-1][it]
    if t_end <= 0:
        t_end = tmax

    times, cds, cls, cms = [], [], [], []
    for r in rows:
        if len(r) <= max(icd, icl, icm):
            continue
        t = r[it]
        if t < t_start or t > t_end:
            continue
        times.append(t)
        cds.append(r[icd])
        cls.append(r[icl])
        cms.append(r[icm])

    if not cds:
        return {
            "ok": False,
            "error": "no samples in window [%g,%g] (data ends at %g)"
            % (t_start, t_end, tmax),
        }

    res = {
        "ok": True,
        "dat": dat,
        "t_window": [t_start, t_end],
        "n_samples": len(cds),
        "Cd": _stats(cds),
        "Cl": _stats(cls),
        "Cm": _stats(cms),
    }
    if cd_ref is not None:
        res["Cd_ref"] = cd_ref
        res["Cd_error_pct"] = 100.0 * (res["Cd"]["mean"] - cd_ref) / cd_ref
    if cl_ref is not None:
        res["Cl_ref"] = cl_ref
        res["Cl_error_pct"] = (
            100.0 * (res["Cl"]["mean"] - cl_ref) / cl_ref if cl_ref != 0 else None
        )

    # time-history plot
    out_png = args.get("out_png")
    if not out_png:
        out_png = os.path.join(case_dir, "report", "force_history.png")
    try:
        os.makedirs(os.path.dirname(out_png), exist_ok=True)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # full series for context
        allt = [r[it] for r in rows if len(r) > max(icd, icl)]
        allcd = [r[icd] for r in rows if len(r) > max(icd, icl)]
        allcl = [r[icl] for r in rows if len(r) > max(icd, icl)]

        fig, ax = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
        ax[0].plot(allt, allcd, lw=0.6, color="tab:blue")
        ax[0].axvspan(t_start, t_end, color="orange", alpha=0.12, label="avg window")
        ax[0].axhline(res["Cd"]["mean"], color="tab:blue", ls="--",
                      label="Cd_mean=%.4f" % res["Cd"]["mean"])
        if cd_ref is not None:
            ax[0].axhline(cd_ref, color="k", ls=":", label="Cd_ref=%.3f" % cd_ref)
        ax[0].set_ylabel("Cd")
        ax[0].set_ylim(min(0, res["Cd"]["mean"] - 4 * res["Cd"]["std"]),
                       res["Cd"]["mean"] + 6 * res["Cd"]["std"])
        ax[0].legend(fontsize=8, loc="upper right")
        ax[0].grid(alpha=0.3)

        ax[1].plot(allt, allcl, lw=0.6, color="tab:green")
        ax[1].axvspan(t_start, t_end, color="orange", alpha=0.12)
        ax[1].axhline(res["Cl"]["mean"], color="tab:green", ls="--",
                      label="Cl_mean=%.4f" % res["Cl"]["mean"])
        if cl_ref is not None:
            ax[1].axhline(cl_ref, color="k", ls=":", label="Cl_ref=%.3f" % cl_ref)
        ax[1].set_ylabel("Cl")
        ax[1].set_xlabel("time [s]")
        ax[1].set_ylim(res["Cl"]["mean"] - 6 * res["Cl"]["std"],
                       res["Cl"]["mean"] + 6 * res["Cl"]["std"])
        ax[1].legend(fontsize=8, loc="upper right")
        ax[1].grid(alpha=0.3)

        fig.suptitle("Ahmed body force coefficients — DDES")
        fig.tight_layout()
        fig.savefig(out_png, dpi=130)
        plt.close(fig)
        res["png"] = out_png
    except Exception as e:
        res["png_error"] = "%s: %s" % (type(e).__name__, e)

    # also dump json next to png
    try:
        jpath = os.path.splitext(out_png)[0] + "_stats.json"
        with open(jpath, "w") as f:
            json.dump(res, f, indent=2)
        res["json"] = jpath
    except Exception:
        pass

    return res
