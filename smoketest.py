#!/usr/bin/env python3
"""
smoketest.py — Phase 0 verification. NO LLM, NO API key.

Proves the OpenFOAM plumbing works from our own code: copy the OF12 cavity
tutorial, run blockMesh -> checkMesh -> a short foamRun, and confirm a result
time directory is written. This is the bedrock the tool-use agent sits on.

Run inside WSL:
    python3 smoketest.py
"""
from __future__ import annotations

import os
import sys

# Make config/foam importable no matter the cwd (script lives next to them).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import foam


def main() -> int:
    print(f"OpenFOAM bashrc : {config.OPENFOAM_BASHRC}")
    print(f"runs root       : {config.RUNS_ROOT}")
    if not os.path.exists(config.OPENFOAM_BASHRC):
        print(f"FAIL: OpenFOAM env not found at {config.OPENFOAM_BASHRC}")
        return 1

    config.RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    case = config.RUNS_ROOT / "smoke_cavity"

    print(f"\n[1/5] copy cavity tutorial -> {case}")
    foam.copy_tutorial("incompressibleFluid/cavity", case)

    print("[2/5] shorten endTime 10 -> 0.5 (fast smoke run)")
    foam.set_dict_entry(case, "system/controlDict", "endTime", "0.5")

    print("[3/5] blockMesh")
    r_bm = foam.run_foam("blockMesh", case=case, timeout=120)
    print(f"      rc={r_bm.returncode}  {r_bm.duration_s:.1f}s  ok={r_bm.ok}")
    if not r_bm.ok:
        print(r_bm.tail())
        return 1

    print("[4/5] checkMesh")
    r_cm = foam.run_foam("checkMesh", case=case, timeout=120)
    mq = foam.parse_checkmesh(r_cm.stdout)
    print(f"      cells={mq['cells']}  maxNonOrtho={mq['max_non_ortho']}  "
          f"maxSkew={mq['max_skewness']}  meshOK={mq['mesh_ok']}")

    print("[5/5] foamRun (solver=incompressibleFluid, t -> 0.5)")
    r_run = foam.run_foam("foamRun", case=case, timeout=600)
    sl = foam.parse_solver_log(r_run.stdout)
    print(f"      rc={r_run.returncode}  {r_run.duration_s:.1f}s  ok={r_run.ok}  "
          f"lastTime={sl['last_time']}  maxCo={sl['max_courant']}  diverged={sl['diverged']}")
    if not r_run.ok:
        print(r_run.tail())
        return 1

    times = foam.list_time_dirs(case)
    print(f"      wrote time dirs: {times}")

    ok = r_bm.ok and r_cm.ok and r_run.ok and mq["mesh_ok"] and bool(times) \
        and not sl["diverged"]
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
