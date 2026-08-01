"""case_status — quick read-only status of an OpenFOAM case (agent self-added)."""
from pathlib import Path

import config
import meshgen

READONLY = True

SPEC = {
    "name": "case_status",
    "description": "OpenFOAM 케이스의 빠른 상태 요약(read-only): 로그 유무, 마지막 solver time, 수렴 여부, timeout 의심, forces.dat 존재. mesh/solve 후 한 번에 점검.",
    "input_schema": {"type": "object", "properties": {
        "case_dir": {"type": "string", "description": "케이스 경로(절대) 또는 run_id"}}, "required": ["case_dir"]},
}


def handler(case_dir: str) -> dict:
    p = Path(case_dir)
    if not p.is_absolute():
        p = Path(config.RUNS_ROOT) / case_dir
    if not p.is_dir():
        return {"ok": False, "error": f"케이스 없음: {p}"}
    log = p / "log.foamRun"
    conv = meshgen.parse_convergence(case_dir=str(p)) if log.is_file() else {}
    forces = list(p.glob("postProcessing/*/*/forces.dat"))
    return {"ok": True, "case": p.name, "has_log": log.is_file(),
            "converged": conv.get("converged"), "final_time": conv.get("final_time"),
            "stopped_early_suspect": conv.get("stopped_early"), "has_forces": bool(forces),
            "n_postproc": len(list((p / "postProcessing").glob("*"))) if (p / "postProcessing").is_dir() else 0}
