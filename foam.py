"""
foam — the OpenFOAM execution layer (proven approach reused from projectOpenFoam).

Every OpenFOAM command runs through `run_foam()`, which:
  * sources the OF12 environment (so blockMesh/foamRun/... are on PATH),
  * cd's into the case directory,
  * runs exactly ONE allowlisted executable with arguments passed as an argv
    list (shell-quoted — model output is untrusted, never interpolated raw),
  * captures stdout/stderr, tees a per-command log into the case, and enforces a
    hard timeout (a runaway solver becomes a structured result, not a hung agent).

This module has NO LLM dependency — it is pure plumbing the tools call.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import config


# ----------------------------------------------------------------- result type
@dataclass
class CmdResult:
    cmd: list
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    log_path: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, n: int = 800) -> str:
        body = self.stdout or self.stderr or ""
        return body[-n:]


# --------------------------------------------------------------------- runner
def _source_prefix() -> str:
    # POSIX 'source'; silence the banner the OF bashrc prints.
    return ". " + shlex.quote(config.OPENFOAM_BASHRC) + " >/dev/null 2>&1"


def run_foam(
    executable: str,
    args: list[str] | None = None,
    *,
    case: str | Path,
    timeout: float = config.DEFAULT_TIMEOUT,
    log: bool = True,
    log_name: str | None = None,
) -> CmdResult:
    """Run one allowlisted OpenFOAM executable inside `case`, env sourced."""
    args = [str(a) for a in (args or [])]
    if executable not in config.ALLOWED_BINS:
        return CmdResult([executable, *args], 126, "",
                         f"executable not allowlisted: {executable}", False, 0.0)
    case = Path(case)

    parts = [
        _source_prefix(),
        "cd " + shlex.quote(str(case)),
        " ".join([shlex.quote(executable)] + [shlex.quote(a) for a in args]),
    ]
    inner = " && ".join(parts)
    log_path = None
    if log:
        log_path = str(case / f"log.{log_name or executable}")
        # tee the combined output; exit with the EXECUTABLE's code, not tee's.
        inner += " 2>&1 | tee " + shlex.quote(log_path) + "; exit ${PIPESTATUS[0]}"

    t0 = time.time()
    try:
        p = subprocess.run(["bash", "-c", inner], capture_output=True,
                           text=True, timeout=timeout)
        rc, out, err, to = p.returncode, p.stdout, p.stderr, False
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        err = (e.stderr if isinstance(e.stderr, str) else "") + "\n[TIMEOUT]"
        rc, to = -9, True
    return CmdResult([executable, *args], rc, out, err, to, time.time() - t0, log_path)


# ------------------------------------------------------- background (detached) jobs
def _running(pid: int) -> bool:
    """True if the process is ALIVE (not a zombie). os.kill(pid,0) would report a killed-but-
    unreaped child (zombie 'Z') as alive, so read /proc/<pid>/stat's state field instead."""
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            after = f.read().rsplit(") ", 1)[1]     # skip 'comm' (may contain spaces/parens)
        state = after.split(" ", 1)[0]
        return state not in ("Z", "X", "x")         # Z=zombie, X/x=dead
    except FileNotFoundError:
        return False
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def run_foam_bg(commands, *, case, log_name: str = "foamRun") -> dict:
    """Launch a CHAIN of allowlisted OF commands DETACHED — non-blocking, survives the agent
    process (own session). `commands`: list of (executable, args_list), joined with '&&'. Output
    goes to log.<name>. Returns {ok, pid, log}. The child is its own process-group leader
    (pgid == pid), so bg_stop(pid) kills the whole chain."""
    case = Path(case)
    quoted = []
    for exe, a in commands:
        if exe not in config.ALLOWED_BINS:
            return {"ok": False, "error": f"executable not allowlisted: {exe}"}
        quoted.append(" ".join([shlex.quote(exe)] + [shlex.quote(str(x)) for x in (a or [])]))
    log_path = str(case / f"log.{log_name}")
    inner = " && ".join([_source_prefix(), "cd " + shlex.quote(str(case)), *quoted])
    try:
        f = open(log_path, "w")
        p = subprocess.Popen(["bash", "-c", inner], stdout=f, stderr=subprocess.STDOUT,
                             start_new_session=True)
        f.close()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "pid": p.pid, "log": log_path}


def bg_stop(pid: int) -> bool:
    """Stop a background job: SIGTERM the process group, then SIGKILL if still alive after a grace
    period, then reap. OpenFOAM's last checkpoint(writeInterval) is preserved → resume via
    startFrom latestTime. Returns True if the process is dead afterwards."""
    pid = int(pid)
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        pgid = pid

    def _sig(s):
        try:
            os.killpg(pgid, s)
        except OSError:
            try:
                os.kill(pid, s)
            except OSError:
                pass
    _sig(signal.SIGTERM)
    time.sleep(1.5)
    if _running(pid):
        _sig(signal.SIGKILL)
        time.sleep(0.5)
    try:
        os.waitpid(pid, os.WNOHANG)       # reap if it's our direct child (avoid lingering zombie)
    except (ChildProcessError, OSError):
        pass
    return not _running(pid)


# ------------------------------------------------------------- case utilities
def copy_tutorial(name: str, dest: str | Path) -> Path:
    """Copy a bundled OF12 tutorial case into `dest`.

    `name` is relative to $FOAM_TUTORIALS, e.g. 'incompressibleFluid/cavity'.
    """
    src = Path(config.FOAM_TUTORIALS) / name
    if not src.is_dir():
        raise FileNotFoundError(f"tutorial not found: {src}")
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    return dest


def set_dict_entry(case: str | Path, dict_rel: str, key: str, value: str) -> bool:
    """Crude top-level dictionary edit: replace `key  <something>;` with the new
    value. Good enough for simple controls (endTime, deltaT, writeInterval).
    Returns True if a substitution was made.
    """
    p = Path(case) / dict_rel
    src = p.read_text()
    # [^;\n]* so dimensioned values like `nu  1e-05 [m^2/s];` are fully replaced
    # (caller supplies the new value incl. any [..] dimensions).
    new, n = re.subn(rf"(^\s*{re.escape(key)}\s+)[^;\n]*;", rf"\g<1>{value};",
                     src, flags=re.M)
    if n:
        p.write_text(new)
    return n > 0


def get_dict_entry(case: str | Path, dict_rel: str, key: str) -> str | None:
    """Read a simple top-level dictionary value, e.g. controlDict 'solver' or a
    nested 'model' line. Returns the token before ';' or None."""
    p = Path(case) / dict_rel
    if not p.is_file():
        return None
    # (.+?) so dimensioned values like `nu  1e-05 [m^2/s];` read fully, not just
    # the first token; single-token values (solver, model, endTime) still work.
    m = re.search(rf"^\s*{re.escape(key)}\s+(.+?);", p.read_text(), flags=re.M)
    return m.group(1).strip() if m else None


def list_time_dirs(case: str | Path) -> list[str]:
    """Numeric time directories written by the solver (excludes '0', constant, system)."""
    case = Path(case)
    out = []
    for child in case.iterdir():
        if not child.is_dir():
            continue
        nm = child.name
        if nm == "0":
            continue
        if nm.replace(".", "", 1).isdigit():
            out.append(nm)
    return sorted(out, key=float)


# --------------------------------------------------------------- parallel / MPI
def decide_nprocs(cells, *, max_ranks=None, cells_per_rank=None,
                  min_parallel=None) -> int:
    """Choose MPI rank count from mesh size: serial for small meshes, scaling up
    to MAX_RANKS for big ones. Returns 1 when decomposition is not worth it."""
    max_ranks = config.MAX_RANKS if max_ranks is None else max_ranks
    cells_per_rank = config.CELLS_PER_RANK if cells_per_rank is None else cells_per_rank
    min_parallel = config.MIN_PARALLEL_CELLS if min_parallel is None else min_parallel
    if not cells or cells < min_parallel:
        return 1
    return max(2, min(max_ranks, cells // cells_per_rank))


def write_decompose_par_dict(case: str | Path, n: int, method: str = "scotch") -> Path:
    """Write a minimal system/decomposeParDict (scotch = automatic graph
    decomposition; needs no geometry input)."""
    content = (
        "FoamFile\n{\n"
        "    format      ascii;\n"
        "    class       dictionary;\n"
        "    object      decomposeParDict;\n"
        "}\n\n"
        f"numberOfSubdomains {n};\n\n"
        f"method          {method};\n"
    )
    p = Path(case) / "system" / "decomposeParDict"
    p.write_text(content)
    return p


def run_parallel(case: str | Path, n: int, timeout: float) -> CmdResult:
    """decomposePar -> mpirun -np n foamRun -parallel -> reconstructPar.
    Returns the solver result; its log is written to log.foamRun so the serial
    parsers/report work unchanged. reconstructPar runs even on solver failure so
    whatever was written is merged back to top-level time directories."""
    rd = run_foam("decomposePar", ["-force"], case=case, timeout=600)
    if not rd.ok:
        return rd
    rs = run_foam("mpirun", ["-np", str(n), "foamRun", "-parallel"],
                  case=case, timeout=timeout, log=True, log_name="foamRun")
    run_foam("reconstructPar", [], case=case, timeout=600)
    return rs


# ------------------------------------------------------------------- parsers
def parse_checkmesh(text: str) -> dict:
    """Pull the headline mesh-quality numbers from a checkMesh log (OF Foundation)."""
    out: dict = {"cells": None, "max_non_ortho": None, "max_skewness": None,
                 "mesh_ok": False}
    m = re.search(r"cells:\s+(\d+)", text)
    if m:
        out["cells"] = int(m.group(1))
    m = re.search(r"non-orthogonality Max:\s*([0-9.eE+-]+)", text)
    if m:
        out["max_non_ortho"] = float(m.group(1))
    m = re.search(r"Max skewness\s*=\s*([0-9.eE+-]+)", text)
    if m:
        out["max_skewness"] = float(m.group(1))
    out["mesh_ok"] = "Mesh OK" in text
    return out


def parse_solver_log(text: str) -> dict:
    """Lightweight solver-log read: last time, max Courant, and divergence flags."""
    out: dict = {"last_time": None, "max_courant": None, "diverged": False}
    times = re.findall(r"^Time = ([0-9.eE+-]+)", text, flags=re.M)
    if times:
        out["last_time"] = float(times[-1])
    co = re.findall(r"Courant Number mean: [0-9.eE+-]+ max: ([0-9.eE+-]+)", text)
    if co:
        out["max_courant"] = max(float(x) for x in co)
    # Real divergence signatures only. NB: "Enabling floating point exception
    # trapping" is a benign startup banner, so we never substring-match that;
    # an actual SIGFPE shows up as a sigFpe handler / printStack trace instead.
    crashed = ("FOAM FATAL" in text
               or "sigFpe::sigHandler" in text
               or "error::printStack" in text)
    high_co = out["max_courant"] is not None and out["max_courant"] > 100
    nan_inf = re.search(r"[Ii]nitial residual = (nan|inf)", text) is not None
    out["diverged"] = bool(crashed or high_co or nan_inf)
    return out
