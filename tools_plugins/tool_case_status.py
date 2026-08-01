"""case_status — quick read-only status of an OpenFOAM case (agent self-added)."""
from pathlib import Path

import config
import meshgen

READONLY = True

SPEC = {
    "name": "case_status",
    "description": "Quick status summary of an OpenFOAM case (read-only): log present, last solver time, converged or not, suspected timeout, forces.dat present. One-shot check after mesh/solve.",
    "input_schema": {"type": "object", "properties": {
        "case_dir": {"type": "string", "description": "case path (absolute) or run_id"}}, "required": ["case_dir"]},
}


def handler(case_dir: str) -> dict:
    p = Path(case_dir)
    if not p.is_absolute():
        p = Path(config.RUNS_ROOT) / case_dir
    if not p.is_dir():
        return {"ok": False, "error": f"case not found: {p}"}
    log = p / "log.foamRun"
    conv = meshgen.parse_convergence(case_dir=str(p)) if log.is_file() else {}
    forces = list(p.glob("postProcessing/*/*/forces.dat"))
    return {"ok": True, "case": p.name, "has_log": log.is_file(),
            "converged": conv.get("converged"), "final_time": conv.get("final_time"),
            "stopped_early_suspect": conv.get("stopped_early"), "has_forces": bool(forces),
            "n_postproc": len(list((p / "postProcessing").glob("*"))) if (p / "postProcessing").is_dir() else 0}
