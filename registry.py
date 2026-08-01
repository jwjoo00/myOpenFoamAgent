"""
registry — cross-run store so the agent can REUSE prior runs (the "save settings,
let the LLM suggest them later" requirement).

One append-only-ish SQLite row per run: the settings used + the results obtained.
Queryable columns for fast filtering (case_kind / solver / turbulence / converged)
plus full JSON blobs for the rest. The OpenFOAM case files themselves stay on disk
as plain text; this DB is the index over them.

No LLM dependency — the agent exposes get_run / list_runs / find_similar_runs as
tools so the model can pull prior runs into context.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import uuid
from pathlib import Path

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    ts           TEXT NOT NULL,
    case_kind    TEXT,
    geometry_src TEXT,
    solver       TEXT,
    turb_model   TEXT,
    mesh_cells   INTEGER,
    end_time     REAL,
    converged    INTEGER,
    wall_time    REAL,
    cd           REAL,
    cl           REAL,
    status       TEXT,
    case_dir     TEXT,
    settings     TEXT,   -- JSON
    results      TEXT,   -- JSON
    notes        TEXT
);
"""

COLUMNS = ["run_id", "ts", "case_kind", "geometry_src", "solver", "turb_model",
           "mesh_cells", "end_time", "converged", "wall_time", "cd", "cl",
           "status", "case_dir", "settings", "results", "notes"]


def db_path() -> Path:
    # Resolved lazily so tests can point config.RUNS_ROOT at a temp dir.
    return config.RUNS_ROOT / "registry.db"


def _conn() -> sqlite3.Connection:
    config.RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def new_run_id(case_kind: str = "case") -> str:
    # Timestamp for readability + a short uuid so two runs in the same second
    # never collide (which would let INSERT OR REPLACE clobber a prior row).
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{case_kind}_{ts}_{uuid.uuid4().hex[:4]}"


def record_run(rec: dict) -> str:
    """Insert/replace one run record. settings/results may be dict/list (JSON-encoded)."""
    init_db()
    row = dict(rec)
    row.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
    for k in ("settings", "results"):
        if isinstance(row.get(k), (dict, list)):
            row[k] = json.dumps(row[k], ensure_ascii=False)
    if isinstance(row.get("converged"), bool):
        row["converged"] = int(row["converged"])
    vals = [row.get(c) for c in COLUMNS]
    placeholders = ",".join("?" * len(COLUMNS))
    with _conn() as c:
        c.execute(f"INSERT OR REPLACE INTO runs ({','.join(COLUMNS)}) "
                  f"VALUES ({placeholders})", vals)
    return row["run_id"]


def update_run(run_id: str, **fields) -> None:
    """Merge-update only the given columns of an existing run (does NOT clobber
    the rest of the row, unlike record_run's INSERT OR REPLACE)."""
    init_db()
    for k in ("settings", "results"):
        if isinstance(fields.get(k), (dict, list)):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    if isinstance(fields.get("converged"), bool):
        fields["converged"] = int(fields["converged"])
    cols = [k for k in fields if k in COLUMNS and k != "run_id"]
    if not cols:
        return
    sets = ",".join(f"{c}=?" for c in cols)
    vals = [fields[c] for c in cols] + [run_id]
    with _conn() as c:
        c.execute(f"UPDATE runs SET {sets} WHERE run_id=?", vals)


def merge_results(run_id: str, **fields) -> None:
    """Read-merge-write the results JSON so successive stages (mesh -> bl -> ras ->
    solve) ACCUMULATE their recipe+metrics into one row instead of overwriting.
    Each stage passes a namespaced dict, e.g. merge_results(rid, mesh={...})."""
    cur = (get_run(run_id) or {}).get("results")
    if not isinstance(cur, dict):
        cur = {}
    cur.update(fields)
    update_run(run_id, results=cur)


def _row_to_dict(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("settings", "results"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


def get_run(run_id: str) -> dict | None:
    init_db()
    with _conn() as c:
        r = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return _row_to_dict(r)


def list_runs(limit: int = 20, case_kind: str | None = None) -> list[dict]:
    init_db()
    q, args = "SELECT * FROM runs", []
    if case_kind:
        q += " WHERE case_kind=?"
        args.append(case_kind)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_similar_runs(case_kind: str | None = None, solver: str | None = None,
                      turb_model: str | None = None, converged_only: bool = True,
                      limit: int = 10) -> list[dict]:
    """Prior runs matching the given facets — what the LLM uses to suggest settings."""
    init_db()
    clauses, args = [], []
    if case_kind:
        clauses.append("case_kind=?"); args.append(case_kind)
    if solver:
        clauses.append("solver=?"); args.append(solver)
    if turb_model:
        clauses.append("turb_model=?"); args.append(turb_model)
    if converged_only:
        clauses.append("converged=1")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    q = f"SELECT * FROM runs{where} ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [_row_to_dict(r) for r in rows]
