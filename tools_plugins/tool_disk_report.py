"""
Example hot-reloadable plugin. The contract:
  - SPEC: dict with name / description / input_schema (same shape as tools.TOOL_SPECS)
  - handler(**kwargs) -> JSON-able dict
  - READONLY = True   for read-only tools (ungated). Plugins are GATED by DEFAULT otherwise.
  - GATED = True      to force gating (redundant if not READONLY)

Module body is AST-validated before it is ever executed: NOTHING may run at import time —
only imports (not of the engine/loop), def (with call-free decorators & default args), simple
assignments, and a literal SPEC are allowed. This file can be added/edited/removed while the
agent runs; selfevolve.reload_plugins() splices it into the tool registry with no restart.
"""
from pathlib import Path

import config

READONLY = True   # read-only -> ungated. Plugins are GATED by default unless they declare this.

SPEC = {
    "name": "disk_report",
    "description": "RUNS_ROOT 아래 각 케이스 디렉토리의 디스크 사용량(MB)을 큰 순으로 반환. 디스크 위생용 read-only 플러그인.",
    "input_schema": {"type": "object", "properties": {
        "top": {"type": "integer", "description": "상위 N개 (기본 15)"}}, "required": []},
}


def _dir_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return round(total / 1e6, 1)


def handler(top: int = 15) -> dict:
    root = Path(config.RUNS_ROOT)
    if not root.is_dir():
        return {"ok": False, "error": f"RUNS_ROOT 없음: {root}"}
    cases = [(d.name, _dir_mb(d)) for d in root.iterdir() if d.is_dir()]
    cases.sort(key=lambda x: x[1], reverse=True)
    return {"ok": True, "total_mb": round(sum(c[1] for c in cases), 1),
            "cases": [{"name": n, "mb": m} for n, m in cases[:int(top)]]}
