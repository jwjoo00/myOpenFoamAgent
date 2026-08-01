#!/usr/bin/env python3
"""
test_registry.py — verify the cross-run registry round-trips. NO LLM, NO OpenFOAM.

Run inside WSL:  python3 test_registry.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        config.RUNS_ROOT = Path(d)            # isolate DB in a temp dir
        import registry

        registry.init_db()
        rid = registry.new_run_id("cavity")

        registry.record_run({
            "run_id": rid, "case_kind": "cavity", "geometry_src": "tutorial",
            "solver": "incompressibleFluid", "turb_model": "kEpsilon",
            "mesh_cells": 400, "end_time": 0.5, "converged": True,
            "wall_time": 1.6, "cd": None, "cl": None, "status": "done",
            "case_dir": "/tmp/of_agent_runs/smoke_cavity",
            "settings": {"endTime": 0.5, "deltaT": 0.005},
            "results": {"maxCo": 0.166, "last_time": 0.5},
            "notes": "phase0 smoke",
        })
        # a second, non-converged run that should NOT show up in similar (converged_only)
        registry.record_run({
            "run_id": registry.new_run_id("cavity"), "case_kind": "cavity",
            "solver": "incompressibleFluid", "turb_model": "kEpsilon",
            "mesh_cells": 100, "converged": False, "status": "failed",
        })

        ok = True

        g = registry.get_run(rid)
        ok &= g is not None and g["mesh_cells"] == 400
        ok &= isinstance(g["settings"], dict) and g["settings"]["endTime"] == 0.5
        ok &= g["results"]["maxCo"] == 0.166
        print(f"[get_run] {rid}: cells={g['mesh_cells']} settings={g['settings']}")

        runs = registry.list_runs(case_kind="cavity")
        ok &= len(runs) == 2
        print(f"[list_runs] count={len(runs)}")

        sims = registry.find_similar_runs(case_kind="cavity",
                                          solver="incompressibleFluid",
                                          turb_model="kEpsilon")
        ok &= len(sims) == 1 and sims[0]["run_id"] == rid   # only the converged one
        print(f"[find_similar_runs converged_only] count={len(sims)} -> {sims[0]['run_id']}")

        print("\nRESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
