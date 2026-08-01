"""
selfevolve.py — the agent's self-evolution engine.

Lets the OpenFOAM agent EXTEND and (carefully) MODIFY its own code, with the same
same snapshot → validate → diff discipline used across my agentic systems: (1) backup, (2) syntax +
canary validation, (3) diff before apply. A running Python process can't hot-swap
its own loaded modules, so "self-evolution" here means: stage -> validate in an
isolated incubator -> atomic promote -> reload (plugins) or re-exec (kernel).

Three safety tiers by what a change touches:
  HOT      tools_plugins/*.py   -> live importlib reload, no restart (autonomous-eligible)
  RESTART  any other source     -> staged + validated, needs bootstrap re-exec to take effect
  PROTECTED selfevolve.py, bootstrap.py -> the trusted root + this engine; human-only, never autonomous
            (so self-evolution can never quietly disable its own safety gate)

The oracle that makes this trustworthy: a CANARY test suite grounded in the
literature-validated CFD (DTMB Kt, B-series polynomial). A change is promoted only
if the canary still passes; otherwise it is discarded / rolled back.
"""
from __future__ import annotations

import ast
import difflib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import config

PROJECT = Path(config.PROJECT_DIR)
PLUGINS_DIR = PROJECT / "tools_plugins"
STATE = Path(config.RUNS_ROOT) / "selfevolve"     # ext4, not Dropbox-churned
SNAP_DIR = STATE / "snapshots"
STAGING_DIR = STATE / "staging"
EVOLVE_LOG = STATE / "evolve_log.jsonl"
PROMOTE_SIGNAL = STATE / "promote.signal"          # bootstrap watches this

# Files the self-evolution engine must NEVER write to (apply_edit refuses these). The engine,
# the trusted root, the confinement config, the LLM loop / approval gate, and foam.py (the
# binary-execution gateway that enforces ALLOWED_BINS). Edit by hand only. Compared
# case-INSENSITIVELY (Windows FS is case-insensitive — "CONFIG.PY" would overwrite config.py).
PROTECTED = {"selfevolve.py", "bootstrap.py", "config.py", "agent.py", "foam.py"}
# Dangerous tools that MUST stay human-gated. The canary asserts REQUIRED_GATED ⊆ tools.GATED and
# agent._approve consults this frozenset directly, so a tampered tools.GATED can't silently un-gate
# destructive tools. Immutable (frozenset) — cannot be .clear()'d.
REQUIRED_GATED = frozenset({
    "run_solver", "run_mesh", "write_dict_entry", "write_file", "delete_path", "generate_mesh",
    "add_boundary_layers", "scaffold_external_ras", "run_snappy_mesh", "scaffold_mrf",
    "generate_propeller", "fetch_geometry", "propose_self_edit", "apply_self_edit",
    "rollback_self", "set_auto_evolve"})
# Minimum confinement baseline the canary asserts stays intact in config.py.
REQUIRED_BINS = frozenset({"foamRun", "blockMesh", "snappyHexMesh", "checkMesh"})
# Plugins are EXECUTED only in the live agent process (LOAD=1). Validation/incubator/bootstrap
# canary subprocesses set LOAD=0 -> plugins are AST-validated and their SPEC read statically, never
# executed. So untrusted staged code never runs during validation (no propose-time RCE).
LOAD_PLUGINS = os.environ.get("OF_AGENT_LOAD_PLUGINS", "1") != "0"
AUTO_BUDGET_DEFAULT = int(os.environ.get("OF_AGENT_EVOLVE_BUDGET", "8"))

_tools_mod = None          # set by attach(); the live tools module we extend in place
_PLUGIN_NAMES: set[str] = set()   # tool names currently contributed by plugins (for clean reload)

for d in (STATE, SNAP_DIR, STAGING_DIR, PLUGINS_DIR):
    d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- log
def _log(event: str, **kw):
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}
    with EVOLVE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def evolve_log(n: int = 20) -> list[dict]:
    if not EVOLVE_LOG.is_file():
        return []
    lines = EVOLVE_LOG.read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines[-n:] if x.strip()]


# ------------------------------------------------------------------- plugins (1)
def _import_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # raises on syntax/exec error -> caller handles
    return mod


# Modules a plugin may NOT import ANYWHERE (module body OR handler body) — importing the
# engine/loop or a dynamic-import helper is how a handler would neuter the guards at call time.
_BANNED_IMPORTS = {"tools", "selfevolve", "bootstrap", "agent", "importlib", "builtins"}
# Builtins a plugin may NOT call ANYWHERE — dynamic code / import escape hatches.
_BANNED_CALLS = {"eval", "exec", "compile", "__import__", "globals", "vars"}


def _has_call(node) -> bool:
    """True if anything that executes code (a call/await) appears anywhere under `node`."""
    return node is not None and any(isinstance(n, (ast.Call, ast.Await)) for n in ast.walk(node))


def _validate_plugin_ast(source: str):
    """Static safety gate for a plugin's MODULE BODY, run BEFORE any execution. The single rule:
    NOTHING may execute at import time. Allowed top-level: imports (not of the engine/loop),
    def (whose decorators AND default-arg values are call-free — those run at def time!), simple
    Name assignments with call-free values, docstrings. Rejected: bare calls, attribute assigns
    (tools.GATED=...), class defs, control flow, decorators/defaults containing calls, importing
    the engine/loop. So a plugin cannot run side effects or poison state at import; only its
    function BODIES run, and only on an explicit tool-call. Returns (ok, reason, spec_dict|None)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"SyntaxError: {e}", None
    # Layer 1 — whole file (handler bodies INCLUDED): no engine/dynamic-import, no eval/exec.
    # Blocks the call-time guard-neutering (rebind REQUIRED_GATED, monkeypatch _approve,
    # importlib.import_module the engine) the re-red-team found in handler bodies.
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in _BANNED_IMPORTS:
                    return False, f"금지된 import(어디서든): {a.name}", None
        elif isinstance(n, ast.ImportFrom):
            if (n.module or "").split(".")[0] in _BANNED_IMPORTS:
                return False, f"금지된 from-import(어디서든): {n.module}", None
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _BANNED_CALLS:
            return False, f"금지된 호출(어디서든): {n.func.id}()", None
    # Layer 2 — module level: nothing executes at import time.
    spec_val = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.decorator_list:               # ANY decorator runs at import (even bare @name)
                return False, "데코레이터 금지 (import-time 실행)", None
            defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
            if any(_has_call(d) for d in defaults):
                return False, "기본 인자값에 호출 금지 (import-time 실행)", None
            continue                              # body is deferred to call time
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue                              # banned ones already rejected in layer 1
        if isinstance(node, ast.Assign):
            if not all(isinstance(t, ast.Name) for t in node.targets):
                return False, "모듈-레벨 attribute 대입 금지 (예: tools.GATED=...)", None
            if any(t.id == "SPEC" for t in node.targets):
                try:
                    spec_val = ast.literal_eval(node.value)
                except Exception:
                    return False, "SPEC는 리터럴 dict여야 함 (동적 표현 금지)", None
            elif _has_call(node.value):
                return False, "모듈-레벨 대입값에 호출 금지 (import-time 실행)", None
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                              # docstring / bare literal
        return False, f"허용되지 않는 모듈-레벨 구문: {type(node).__name__} (call·class·제어문 등 금지)", None
    if not isinstance(spec_val, dict) or "name" not in spec_val:
        return False, "SPEC(name 포함 리터럴 dict) 필수", None
    return True, "ok", spec_val


def discover_plugins() -> list[Path]:
    return sorted(p for p in PLUGINS_DIR.glob("tool_*.py") if not p.name.startswith("_"))


# ---- plugin handler SANDBOX (closes the in-process-execution residual) -------
# Plugin handlers NEVER run in the live agent process. Each call is executed in a confined
# subprocess: separate memory (a handler cannot rebind REQUIRED_GATED / monkeypatch _approve /
# re-arm _auto in the live process), filesystem read-only EXCEPT RUNS_ROOT, no network (bwrap),
# CPU/mem rlimits + timeout. This is what makes autonomous self-evolution safe — even a malicious
# handler body (e.g. injected by auto_evolve) is contained.
# Without a real sandbox (bwrap/firejail) the agent runs FAIL-CLOSED: plugin execution and
# autonomy are refused. Set OF_AGENT_ALLOW_UNSANDBOXED=1 to override for a host you trust (chat-
# driven only; autonomy still requires a real sandbox regardless of this flag).
ALLOW_UNSANDBOXED = os.environ.get("OF_AGENT_ALLOW_UNSANDBOXED", "0") != "0"
_MAX_RESULT = 1_000_000          # cap plugin stdout (parent OOM / cost amplification)
_MAX_PLUGIN_BYTES = 262_144      # cap plugin file size (literal_eval DoS)


def _sandbox_cmd():
    """bwrap with a MINIMAL bind (not --ro-bind / /): only the python runtime, the project (ro)
    and RUNS_ROOT (rw) are visible — so ~/.ssh, ~/.aws, unrelated personal files,
    and the API key are NOT in the handler's view. Inside RUNS, the agent's INTERNAL STATE is
    masked with an empty tmpfs / /dev/null so a handler can neither read nor write it:
      - RUNS/selfevolve  (trust store: snapshots, known_good, promote.signal, evolve_log)
      - RUNS/sessions    (conversation transcripts — could hold pasted secrets/paths)
      - RUNS/registry.db (run DB)
    Case data under RUNS stays accessible for legit plugins. No network, own PID namespace.
    firejail is intentionally NOT used (its FS confinement is weaker/asymmetric); without bwrap
    the agent is fail-closed (process-only -> refused unless OF_AGENT_ALLOW_UNSANDBOXED)."""
    runs = str(Path(config.RUNS_ROOT))
    venv = str(Path(sys.executable).parent.parent)   # NO resolve() — keep the venv dir, not its symlink target
    if shutil.which("bwrap"):
        cmd = ["bwrap", "--unshare-net", "--unshare-pid", "--new-session", "--die-with-parent",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        seen = set()
        for d in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", sys.base_prefix, venv, str(PROJECT)):
            if d and d not in seen and os.path.exists(d):
                cmd += ["--ro-bind", d, d]
                seen.add(d)
        cmd += ["--bind", runs, runs]
        cmd += ["--tmpfs", os.path.join(runs, "selfevolve")]     # mask trust store (invisible)
        cmd += ["--tmpfs", os.path.join(runs, "sessions")]       # mask transcripts
        cmd += ["--ro-bind", "/dev/null", os.path.join(runs, "registry.db")]   # mask run DB
        return (cmd, "bwrap")
    return ([], "process-only")            # NOT confined — execution refused unless override


def sandbox_level() -> str:
    return _sandbox_cmd()[1]


def _real_sandbox() -> bool:
    return sandbox_level() == "bwrap"      # bwrap only — the one backend with verified masking


def _rlimits():                            # POSIX: bound CPU + address space of the handler
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (30, 35))
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024 ** 3, 3 * 1024 ** 3))


def _runner_src(plugin_file: str) -> str:
    return ("import json,sys,importlib.util\n"
            "sys.path.insert(0, " + repr(str(PROJECT)) + ")\n"
            "spec=importlib.util.spec_from_file_location('ofp', " + repr(plugin_file) + ")\n"
            "mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
            "args=json.loads(sys.stdin.read() or '{}')\n"
            "try:\n    r=mod.handler(**args)\n"
            "except Exception as e:\n    r={'ok':False,'error':type(e).__name__+': '+str(e)}\n"
            "sys.stdout.write('RESULT:'+json.dumps(r, default=str)[:900000])\n")


def run_plugin_sandboxed(plugin_file: str, args: dict, timeout: int = 30) -> dict:
    """Run a plugin handler in a confined subprocess. FAIL-CLOSED: refuses if no real sandbox
    (unless OF_AGENT_ALLOW_UNSANDBOXED). Output is size-capped; bwrap PID namespace + --die-with-
    parent kills grandchildren on timeout."""
    prefix, level = _sandbox_cmd()
    if level == "process-only" and not ALLOW_UNSANDBOXED:
        return {"ok": False, "sandbox": level,
                "error": "real sandbox(bwrap/firejail) 없음 → 플러그인 실행 거부(fail-closed). "
                         "신뢰 호스트면 OF_AGENT_ALLOW_UNSANDBOXED=1 (chat-driven 한정, 비권장)."}
    env = {"HOME": "/tmp", "MPLCONFIGDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
           "OF_AGENT_RUNS": str(Path(config.RUNS_ROOT)), "OF_AGENT_LOAD_PLUGINS": "0",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    cmd = prefix + [sys.executable, "-s", "-c", _runner_src(plugin_file)]
    try:
        proc = subprocess.run(cmd, input=json.dumps(args or {}), capture_output=True, text=True,
                              timeout=timeout, env=env, start_new_session=True,
                              preexec_fn=_rlimits if os.name == "posix" else None)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"플러그인 timeout({timeout}s) — 강제종료", "sandbox": level}
    except Exception as e:
        return {"ok": False, "error": f"샌드박스 실행 실패: {type(e).__name__}: {e}", "sandbox": level}
    out_s = proc.stdout or ""
    if len(out_s) > _MAX_RESULT:
        return {"ok": False, "error": f"플러그인 출력 과다(>{_MAX_RESULT}B) — 거부", "sandbox": level}
    for line in out_s.splitlines():
        if line.startswith("RESULT:"):
            out = json.loads(line[len("RESULT:"):])
            if isinstance(out, dict):
                out.setdefault("sandbox", level)
            return out
    return {"ok": False, "error": "플러그인 결과 없음", "sandbox": level, "stderr": (proc.stderr or "")[-400:]}


def _make_sandbox_handler(plugin_path: Path):
    pf = str(plugin_path)

    def _h(**args):
        return run_plugin_sandboxed(pf, args)
    _h._sandboxed = _real_sandbox()        # reflect ACTUAL confinement, not an unconditional True
    return _h


def _plugin_flags(src: str) -> dict:
    """Read READONLY / GATED literal flags from a plugin via AST (no execution)."""
    flags = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return flags
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in ("READONLY", "GATED")):
            try:
                flags[node.targets[0].id] = ast.literal_eval(node.value)
            except Exception:
                pass
    return flags


def load_plugins() -> dict:
    """AST-validate every tools_plugins/tool_*.py and read its SPEC + flags STATICALLY (the plugin
    is NEVER imported into the live process). In LOAD mode each plugin's handler is a SANDBOX wrapper
    that runs the real handler in a confined subprocess; in validation mode a stub is registered.
    Default-deny: a plugin is GATED unless it explicitly sets READONLY=True."""
    specs, dispatch, gated, errors = [], {}, set(), []
    for p in discover_plugins():
        try:
            if p.stat().st_size > _MAX_PLUGIN_BYTES:
                errors.append({"plugin": p.name, "error": f"파일 과대(>{_MAX_PLUGIN_BYTES}B) — 거부"})
                continue
            src = p.read_text(encoding="utf-8")
            ok, reason, spec = _validate_plugin_ast(src)
            if not ok:
                errors.append({"plugin": p.name, "error": reason})
                continue
            name = spec["name"]
            flags = _plugin_flags(src)
            is_gated = bool(flags.get("GATED")) or not bool(flags.get("READONLY"))
            handler = _make_sandbox_handler(p) if LOAD_PLUGINS else (
                lambda **k: {"ok": False, "error": "plugin validation stub"})
            specs.append(spec)
            dispatch[name] = handler
            if is_gated:
                gated.add(name)
        except Exception as e:
            errors.append({"plugin": p.name, "error": f"{type(e).__name__}: {e}"})
    return {"specs": specs, "dispatch": dispatch, "gated": gated, "errors": errors}


def attach(tools_module):
    """Called once by tools.py at import: remember the live tools module, then merge
    plugins into it. Safe to call before any plugins exist."""
    global _tools_mod
    _tools_mod = tools_module
    return reload_plugins()


def reload_plugins() -> dict:
    """Re-scan tools_plugins/ and merge into the live tools module's DISPATCH /
    TOOL_SPECS / GATED *in place*. agent.py reads those fresh each turn, so new/edited
    plugin tools become available WITHOUT restarting the core loop. Removes stale
    plugin tools first so deletions/renames take effect."""
    global _PLUGIN_NAMES
    if _tools_mod is None:
        return {"ok": False, "error": "tools module not attached"}
    loaded = load_plugins()
    # strip previously-merged plugin tools
    for n in _PLUGIN_NAMES:
        _tools_mod.DISPATCH.pop(n, None)
        _tools_mod.GATED.discard(n)
    _tools_mod.TOOL_SPECS[:] = [s for s in _tools_mod.TOOL_SPECS if s["name"] not in _PLUGIN_NAMES]
    # merge current plugins, then re-assert the immutable baseline so a reload can never
    # leave a REQUIRED_GATED tool un-gated.
    _tools_mod.DISPATCH.update(loaded["dispatch"])
    _tools_mod.GATED |= loaded["gated"]
    _tools_mod.GATED |= set(REQUIRED_GATED)
    _tools_mod.TOOL_SPECS.extend(loaded["specs"])
    _PLUGIN_NAMES = set(loaded["dispatch"].keys())
    _log("reload_plugins", count=len(_PLUGIN_NAMES), names=sorted(_PLUGIN_NAMES), errors=loaded["errors"])
    return {"ok": not loaded["errors"], "plugins": sorted(_PLUGIN_NAMES),
            "errors": loaded["errors"]}


# ----------------------------------------------------------------- snapshots (4)
def _core_files() -> list[Path]:
    files = [p for p in PROJECT.glob("*.py") if not p.name.startswith("_")]
    files += list(PLUGINS_DIR.glob("*.py"))
    return files


def snapshot(label: str = "auto") -> str:
    sid = f"{time.strftime('%Y%m%d_%H%M%S')}_{label}"
    dest = SNAP_DIR / sid
    (dest / "tools_plugins").mkdir(parents=True, exist_ok=True)
    for p in PROJECT.glob("*.py"):
        if not p.name.startswith("_"):
            shutil.copy2(p, dest / p.name)
    for p in PLUGINS_DIR.glob("*.py"):
        shutil.copy2(p, dest / "tools_plugins" / p.name)
    _log("snapshot", id=sid, label=label)
    return sid


def list_snapshots() -> list[str]:
    return sorted(d.name for d in SNAP_DIR.glob("*") if d.is_dir())


def rollback(snapshot_id: str) -> dict:
    src = SNAP_DIR / snapshot_id
    if not src.is_dir():
        return {"ok": False, "error": f"snapshot 없음: {snapshot_id}"}
    snapshot("pre_rollback")                      # safety: snapshot current first
    for p in src.glob("*.py"):
        shutil.copy2(p, PROJECT / p.name)
    pdir = src / "tools_plugins"
    if pdir.is_dir():
        for existing in PLUGINS_DIR.glob("*.py"):
            existing.unlink()
        for p in pdir.glob("*.py"):
            shutil.copy2(p, PLUGINS_DIR / p.name)
    _log("rollback", id=snapshot_id)
    reload_plugins()
    return {"ok": True, "rolled_back_to": snapshot_id,
            "note": "커널 파일(agent/tools 등)은 재시작해야 반영됨"}


# -------------------------------------------------------------------- canary (2)
def canary(level: str = "fast") -> dict:
    """Regression oracle. FAST (~1s): syntax+imports, tool-registry consistency, and
    the physics-validated propeller math (B-series polynomial, DTMB table, watertight
    geometry, convergence parser). FULL adds the real DTMB MRF solve (opt-in, ~25min;
    here we verify the validated case's recorded Kt instead). Returns {ok, passed, failed}."""
    passed, failed = [], []

    def check(nm, fn):
        try:
            ok, detail = fn()
            (passed if ok else failed).append({"check": nm, "detail": detail})
        except Exception as e:
            failed.append({"check": nm, "detail": f"{type(e).__name__}: {e}"})

    import propgen
    import meshgen
    import tools

    def _consistency():
        d, s = set(tools.DISPATCH), {x["name"] for x in tools.TOOL_SPECS}
        return d == s, f"DISPATCH={len(d)} SPECS={len(s)} diff={d ^ s}"

    def _bseries():
        kt, kq = propgen.bseries_KT_KQ(0.6, 1.0, 0.70, 4)
        ok = 0.21 < kt < 0.24 and 0.035 < kq < 0.040
        return ok, f"B4-70 J=0.6 KT={kt:.4f} 10KQ={10*kq:.4f} (expect ~0.226/0.373)"

    def _dtmb_table():
        t = propgen.DTMB_4119
        return len(t) >= 8 and abs(t[0][0] - 0.2) < 1e-6, f"rows={len(t)} first r/R={t[0][0]}"

    def _bgen():
        tbl = propgen.wageningen_b_series(4, 0.70, 1.0)
        c07 = [r[1] for r in tbl if abs(r[0] - 0.7) < 1e-6][0]
        return len(tbl) == 9 and 0.36 < c07 < 0.39, f"rows={len(tbl)} c/D@0.7R={c07:.3f}"

    def _geometry():
        try:
            import trimesh  # noqa: F401
        except ImportError:
            # optional geometry dep absent — NOT a code/security failure. Skip so a partial env
            # (e.g. system python without trimesh) doesn't brick the agent; propeller geometry is
            # simply unavailable until it runs under the venv that has trimesh.
            return True, "trimesh 미설치 — geometry 체크 skip (venv python으로 실행 시 활성)"
        import tempfile
        out = tempfile.mkdtemp(dir=str(STATE))
        try:
            r = propgen.generate_propeller(out, table=propgen.DTMB_4119, n_blades=3, diameter=0.3048)
            return bool(r.get("n_faces", 0) > 0), f"n_faces={r.get('n_faces')} watertight={r.get('watertight')}"
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def _convergence():
        log = "Time = 41\n...\nSIMPLE solution converged in 42 iterations\n"
        c = meshgen.parse_convergence(case_dir=str(STATE), log_text=log)
        return bool(c.get("converged")), f"converged={c.get('converged')}"

    # --- SECURITY invariants (the oracle must guard its OWN safety mechanisms, not just
    #     propeller physics). bootstrap runs this same canary, so a self-edit that guts any
    #     of these fails fail-closed and is never blessed as known-good. ---
    import re as _re

    import config as _cfg

    def _gated_baseline():
        missing = REQUIRED_GATED - set(tools.GATED)
        return not missing, (f"un-gated 위험도구: {sorted(missing)}" if missing else "REQUIRED_GATED ⊆ GATED")

    def _protected_invariant():
        bad = [f for f in PROTECTED if _classify(f) != "protected"]
        if _classify("CONFIG.PY") != "protected":      # case-variant must still be protected
            bad.append("CONFIG.PY(case) 우회")
        if _classify("tool_x.py") != "hot":
            bad.append("tool_x.py!=hot")
        return not bad, (f"PROTECTED/_classify 위반: {bad}" if bad else "engine/config/agent/foam protected (case-insensitive)")

    def _allowed_bins():
        missing = REQUIRED_BINS - set(getattr(_cfg, "ALLOWED_BINS", set()))
        return not missing, (f"confinement bins 제거: {sorted(missing)}" if missing else "ALLOWED_BINS 불변")

    def _approve_gate():
        import io
        import sys as _sys
        import agent
        old_in, old_out = _sys.stdin, _sys.stdout
        _sys.stdin = io.StringIO("")            # non-interactive -> input() EOFError -> deny
        _sys.stdout = io.StringIO()             # swallow the _approve prompt print
        try:
            denied = agent._approve("run_solver", {}, auto=False) is False
            opened = agent._approve("list_runs", {}, auto=False) is True
        finally:
            _sys.stdin, _sys.stdout = old_in, old_out
        return denied and opened, f"gated 거부={denied}, ungated 통과={opened}"

    def _spec_structural():
        seen, bad = set(), []
        rx = _re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
        for s in tools.TOOL_SPECS:
            n = s.get("name", "")
            if not rx.match(n):
                bad.append(f"name형식:{n!r}")
            if n in seen:
                bad.append(f"중복:{n}")
            seen.add(n)
            if not isinstance(s.get("input_schema"), dict):
                bad.append(f"schema:{n}")
        return not bad, (f"spec 위반: {bad[:5]}" if bad else f"{len(seen)} specs ok")

    def _plugins_sandboxed():
        if not LOAD_PLUGINS:
            return True, "validation 모드(plugins 미로드)"
        lvl = sandbox_level()
        if lvl == "bwrap":
            bad = [n for n in _PLUGIN_NAMES if not getattr(tools.DISPATCH.get(n), "_sandboxed", False)]
            return not bad, f"sandbox=bwrap(state masked), 비샌드박스={bad}"
        # no bwrap: real confinement absent → RED unless host explicitly opted out
        return bool(ALLOW_UNSANDBOXED), f"⚠ {lvl}(비격리), ALLOW_UNSANDBOXED={ALLOW_UNSANDBOXED}"

    check("syntax_all", lambda: compile_all())
    check("tool_registry_consistency", _consistency)
    check("security_gated_baseline", _gated_baseline)
    check("security_protected_invariant", _protected_invariant)
    check("security_allowed_bins", _allowed_bins)
    check("security_approve_gate", _approve_gate)
    check("security_spec_structural", _spec_structural)
    check("security_plugins_sandboxed", _plugins_sandboxed)
    check("bseries_polynomial", _bseries)
    check("dtmb_table_integrity", _dtmb_table)
    check("bseries_generator", _bgen)
    check("geometry_watertight", _geometry)
    check("convergence_parser", _convergence)

    if level == "full":
        import registry
        run = registry.get_run("propellerDTMB_validated") or {}
        v = (run.get("results") or {}).get("validation") or run.get("validation") or {}
        kt = v.get("KT") or v.get("kt_cfd")
        check("dtmb_validated_recorded", lambda: (bool(kt and abs(kt - 0.146) < 0.02),
                                                  f"recorded DTMB KT={kt}"))

    ok = not failed
    _log("canary", level=level, ok=ok, n_pass=len(passed), n_fail=len(failed),
         failed=[f["check"] for f in failed])
    return {"ok": ok, "level": level, "passed": passed, "failed": failed}


def compile_all() -> tuple[bool, str]:
    bad = []
    for p in _core_files():
        r = subprocess.run([sys.executable, "-m", "py_compile", str(p)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(f"{p.name}: {r.stderr.strip().splitlines()[-1] if r.stderr else 'fail'}")
    return (not bad), ("; ".join(bad) if bad else f"{len(_core_files())} files OK")


def _run_incubator_canary(inc: Path) -> dict:
    """Run the canary on STAGED code in an isolated subprocess. Two hard isolations: plugins
    are NOT executed (OF_AGENT_LOAD_PLUGINS=0 -> AST-validate + static SPEC only), and RUNS_ROOT
    is a throwaway tempdir — so untrusted staged code can neither run nor touch live data during
    validation (closes propose-time RCE + data-destruction)."""
    tmp = tempfile.mkdtemp(prefix="ofcanary_")
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import json,selfevolve; print('CANARY:'+json.dumps(selfevolve.canary('fast')))"],
            cwd=str(inc), capture_output=True, text=True, timeout=180,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                 "OF_AGENT_LOAD_PLUGINS": "0", "OF_AGENT_RUNS": tmp})
    except Exception as e:
        return {"ok": False, "failed": [{"check": "canary_run", "detail": str(e)}]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for line in proc.stdout.splitlines():
        if line.startswith("CANARY:"):
            return json.loads(line[len("CANARY:"):])
    return {"ok": False, "failed": [{"check": "canary_import",
                                     "detail": (proc.stderr or proc.stdout)[-600:]}]}


# ------------------------------------------------------- propose / incubate (3)
_PROTECTED_LC = {f.lower() for f in PROTECTED}


def _classify(filename: str) -> str:
    fl = Path(filename).name.lower()             # case-insensitive (Windows FS) + strip any dir
    if fl in _PROTECTED_LC:
        return "protected"
    if fl.startswith("tool_") or (PLUGINS_DIR / Path(filename).name).is_file():
        return "hot"
    return "restart"


def propose_edit(filename: str, old: str, new: str, *, create: bool = False) -> dict:
    """Stage a self-edit into an isolated INCUBATOR (full project copy), apply it there,
    and run the canary inside that copy in a subprocess — the live agent is never touched.
    Returns {ok, edit_id, kind, diff, canary}. Does NOT promote; call apply_edit to commit."""
    fname = Path(filename).name
    target = (PLUGINS_DIR / fname) if _classify(fname) == "hot" else (PROJECT / fname)
    kind = _classify(fname)
    if not create and not target.is_file():
        return {"ok": False, "error": f"파일 없음: {fname} (새 파일이면 create=True)"}
    original = target.read_text(encoding="utf-8") if target.is_file() else ""
    if create:
        updated = new
    else:
        if old not in original:
            return {"ok": False, "error": "old 문자열을 파일에서 못 찾음 (정확히 일치해야 함)"}
        if original.count(old) > 1:
            return {"ok": False, "error": f"old가 {original.count(old)}번 등장 — 더 고유하게"}
        updated = original.replace(old, new, 1)

    if kind == "hot":      # plugin: static-validate module body BEFORE staging (no import)
        okp, reason, _ = _validate_plugin_ast(updated)
        if not okp:
            return {"ok": False, "error": f"플러그인 정적 검증 실패: {reason}"}

    edit_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{fname.replace('.', '_')}"
    inc = STAGING_DIR / edit_id
    if inc.exists():
        shutil.rmtree(inc)
    (inc / "tools_plugins").mkdir(parents=True)
    for p in PROJECT.glob("*.py"):
        if not p.name.startswith("_"):
            shutil.copy2(p, inc / p.name)
    for p in PLUGINS_DIR.glob("*.py"):
        shutil.copy2(p, inc / "tools_plugins" / p.name)
    # write the edited version into the incubator
    inc_target = (inc / "tools_plugins" / fname) if kind == "hot" else (inc / fname)
    inc_target.write_text(updated, encoding="utf-8")
    (inc / "_edit_meta.json").write_text(json.dumps(
        {"filename": fname, "kind": kind, "create": create}), encoding="utf-8")

    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), updated.splitlines(keepends=True),
        fromfile=f"a/{fname}", tofile=f"b/{fname}"))
    can = _run_incubator_canary(inc)     # isolated: no plugin exec, throwaway RUNS_ROOT
    _log("propose_edit", edit_id=edit_id, file=fname, kind=kind, canary_ok=can.get("ok"))
    return {"ok": True, "edit_id": edit_id, "file": fname, "kind": kind,
            "diff": diff or "(빈 diff)", "canary": can,
            "promote_safe": bool(can.get("ok")) and kind != "protected"}


def apply_edit(edit_id: str, *, force: bool = False) -> dict:
    """Promote a staged edit: snapshot current (rollback point), copy the incubated file over
    the live one, then reload (hot) or signal restart. HARD refusals: PROTECTED files
    (selfevolve/bootstrap/config/agent — edit those by hand only); and `force` only relaxes the
    canary for HOT plugin edits (a kernel/module edit ALWAYS needs canary). After a hot reload,
    a live security re-check auto-rolls-back if the new plugin set somehow broke an invariant."""
    inc = STAGING_DIR / edit_id
    if not inc.is_dir():
        return {"ok": False, "error": f"staging 없음: {edit_id}"}
    meta = json.loads((inc / "_edit_meta.json").read_text())
    fname, kind = meta["filename"], meta["kind"]
    if kind == "protected":
        return {"ok": False, "error": f"PROTECTED 파일은 엔진이 절대 쓰지 않음: {fname} (수동 편집만)"}
    # canary gate. `force` is allowed to skip it ONLY for hot plugin edits.
    if not (force and kind == "hot"):
        if not _run_incubator_canary(inc).get("ok"):
            return {"ok": False, "error": "incubator canary 실패 — 승격 거부"
                    + (" (force는 hot 플러그인에만 허용)" if force else " (force=True는 hot에만)")}

    sid = snapshot(f"pre_{fname}")
    inc_src = (inc / "tools_plugins" / fname) if kind == "hot" else (inc / fname)
    dest = (PLUGINS_DIR / fname) if kind == "hot" else (PROJECT / fname)
    shutil.copy2(inc_src, dest)
    _log("apply_edit", edit_id=edit_id, file=fname, kind=kind, snapshot=sid)
    result = {"ok": True, "applied": fname, "kind": kind, "rollback_snapshot": sid}
    if kind == "hot":
        result["reload"] = reload_plugins()
        # live security re-check: if the freshly-loaded plugin set broke any invariant, undo.
        if _tools_mod is not None:
            chk = _live_security_check()
            if not chk["ok"]:
                rollback(sid)
                result.update(ok=False, error=f"적용 후 보안검사 실패 → 자동 롤백: {chk['failed']}")
                return result
        result["note"] = "플러그인 핫리로드 완료 — 다음 턴부터 사용 가능 (재시작 불필요)"
    else:
        PROMOTE_SIGNAL.write_text(json.dumps({"file": fname, "snapshot": sid, "t": time.time()}))
        result["note"] = "커널/모듈 변경 — bootstrap이 재시작(re-exec)해야 반영됨. PROMOTE_SIGNAL 기록함."
    return result


def _live_security_check() -> dict:
    """In-process security invariants (subset of canary, no subprocess) — used right after a
    hot plugin reload to ensure the live registry still honors REQUIRED_GATED and spec rules."""
    if _tools_mod is None:
        return {"ok": True, "failed": []}
    failed = []
    if REQUIRED_GATED - set(_tools_mod.GATED):
        failed.append(f"un-gated: {sorted(REQUIRED_GATED - set(_tools_mod.GATED))}")
    names = [s.get("name", "") for s in _tools_mod.TOOL_SPECS]
    if len(names) != len(set(names)):
        failed.append("중복 tool name")
    return {"ok": not failed, "failed": failed}


# ----------------------------------------------------- autonomous evolution (5)
_auto = {"on": False, "budget": 0, "goal": None}


def auto_status() -> dict:
    return dict(_auto)


def set_auto(on: bool, budget: int = AUTO_BUDGET_DEFAULT, goal: str | None = None) -> dict:
    if on and not _real_sandbox():            # autonomy requires a REAL sandbox, no override
        return {"ok": False, "error": f"real sandbox(bwrap/firejail) 없이는 자율모드 금지 "
                f"(현재 {sandbox_level()}) — handler 격리 불가", **_auto}
    _auto.update(on=bool(on), budget=int(budget) if on else 0, goal=goal)
    _log("set_auto", **_auto)
    return dict(_auto)


def auto_evolve(filename: str, old: str, new: str, reason: str, *, create: bool = False) -> dict:
    """Autonomous self-edit — NO human gate, but tightly fenced: EXISTING PLUGINS ONLY (cannot
    create a new tool autonomously), AST-validated + canary must pass (else discarded), per-session
    budget, kernel/PROTECTED refused, all logged. Self-healing the agent can do without bricking."""
    if not _auto["on"]:
        return {"ok": False, "error": "자율모드 꺼짐 — set_auto(True) 먼저"}
    if not _real_sandbox():
        return {"ok": False, "error": f"real sandbox 없이는 자율 진화 금지 (현재 {sandbox_level()})"}
    if _auto["budget"] <= 0:
        return {"ok": False, "error": "자율 편집 예산 소진 — 사람 검토 필요"}
    fname = Path(filename).name
    if create:
        return {"ok": False, "error": "자율 진화는 새 도구를 만들 수 없음 — 신규 플러그인은 사람 승인(propose/apply) 필요"}
    if _classify(fname) != "hot" or not (PLUGINS_DIR / fname).is_file():
        return {"ok": False, "error": "자율 진화는 **기존** 플러그인(tools_plugins/tool_*.py) 수정만 허용 — 커널·신규는 사람 승인"}
    prop = propose_edit(filename, old, new, create=False)
    if not prop.get("ok"):
        return {"ok": False, "stage": "propose", **prop}
    if not prop["canary"]["ok"]:
        _log("auto_evolve_rejected", file=filename, reason=reason,
             failed=[f["check"] for f in prop["canary"]["failed"]])
        return {"ok": False, "stage": "canary", "rejected": True,
                "failed": prop["canary"]["failed"], "note": "canary 실패 → 자동 폐기 (변경 안 됨)"}
    app = apply_edit(prop["edit_id"])
    _auto["budget"] -= 1
    _log("auto_evolve_applied", file=filename, reason=reason, budget_left=_auto["budget"])
    return {"ok": True, "reason": reason, "budget_left": _auto["budget"], **app}
