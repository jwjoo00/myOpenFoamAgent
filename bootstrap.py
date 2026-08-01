#!/usr/bin/env python3
"""
bootstrap.py — the immutable trusted root that supervises the self-evolving agent.

This file is PROTECTED: the agent must never modify it autonomously. It is deliberately
self-contained (depends only on config for paths) so it can still validate and roll back
even if selfevolve.py / tools.py / agent.py get broken by a bad self-edit.

Run the agent through this instead of `python agent.py`:
    python bootstrap.py            # supervise + auto-rollback
    python bootstrap.py --canary   # just run the canary and exit
    python bootstrap.py --rollback <snapshot_id>

Lifecycle each launch:
  1. preflight: run canary in a clean subprocess; if broken, auto-rollback to last
     known-good and re-check; refuse to launch unrecoverable code.
  2. run agent.py as a CHILD process (fresh code every launch).
  3. on a kernel self-edit (PROMOTE_SIGNAL) or restart request (child exit 42):
     re-run canary -> pass: record new known-good + relaunch ; fail: rollback + relaunch.
Because the agent runs as a child, a bad kernel edit can never permanently brick it.
"""
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
PLUGINS = PROJECT / "tools_plugins"
STATE = Path(config.RUNS_ROOT) / "selfevolve"
SNAP_DIR = STATE / "snapshots"
KNOWN_GOOD = STATE / "known_good.txt"
PROMOTE_SIGNAL = STATE / "promote.signal"
RESTART_CODE = 42
for d in (STATE, SNAP_DIR, PLUGINS):
    d.mkdir(parents=True, exist_ok=True)


# --- self-contained snapshot/rollback (does NOT import selfevolve) -----------
def snapshot(label: str = "known_good") -> str:
    sid = f"{time.strftime('%Y%m%d_%H%M%S')}_{label}"
    dest = SNAP_DIR / sid
    (dest / "tools_plugins").mkdir(parents=True, exist_ok=True)
    for p in PROJECT.glob("*.py"):
        if not p.name.startswith("_"):
            shutil.copy2(p, dest / p.name)
    for p in PLUGINS.glob("*.py"):
        shutil.copy2(p, dest / "tools_plugins" / p.name)
    return sid


def restore(sid: str) -> bool:
    src = SNAP_DIR / sid
    if not src.is_dir():
        return False
    for p in src.glob("*.py"):
        shutil.copy2(p, PROJECT / p.name)
    pdir = src / "tools_plugins"
    if pdir.is_dir():
        for existing in PLUGINS.glob("*.py"):
            existing.unlink()
        for p in pdir.glob("*.py"):
            shutil.copy2(p, PLUGINS / p.name)
    return True


# --- canary via a CLEAN subprocess (tests on-disk code, not this process) -----
def run_canary(level: str = "fast") -> dict:
    code = ("import json,selfevolve;"
            "print('CANARY:'+json.dumps(selfevolve.canary('%s')))" % level)
    tmp = tempfile.mkdtemp(prefix="ofboot_")
    try:
        # LOAD_PLUGINS=0 so preflight AST-validates plugins without executing them; throwaway
        # RUNS_ROOT so even a malicious installed module can't touch live data during validation.
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT),
                              capture_output=True, text=True, timeout=300,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                   "OF_AGENT_LOAD_PLUGINS": "0", "OF_AGENT_RUNS": tmp})
    except Exception as e:
        return {"ok": False, "failed": [{"check": "canary_launch", "detail": str(e)}]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for line in proc.stdout.splitlines():
        if line.startswith("CANARY:"):
            return json.loads(line[len("CANARY:"):])
    return {"ok": False, "failed": [{"check": "canary_import",
                                     "detail": (proc.stderr or proc.stdout)[-500:]}]}


def _restore_known_good() -> bool:
    if KNOWN_GOOD.is_file():
        return restore(KNOWN_GOOD.read_text().strip())
    return False


def _mark_known_good() -> str:
    sid = snapshot("known_good")
    KNOWN_GOOD.write_text(sid)
    return sid


def preflight() -> bool:
    c = run_canary()
    if c["ok"]:
        if not KNOWN_GOOD.is_file():
            print("   ✅ preflight canary passed — known-good recorded:", _mark_known_good())
        return True
    print("⚠️  preflight canary failed:")
    for f in c.get("failed", []):
        print(f"     - {f.get('check')}: {f.get('detail', '')[:160]}")
    if any("No module named" in (f.get("detail") or "") for f in c.get("failed", [])):
        print(f"   ↳ Looks like a missing dependency. Run it with the venv python:\n"
              f"       not {Path(sys.executable).name} → ~/of_agent_venv/bin/python bootstrap.py")
    if _restore_known_good():
        print("   → rolled back to known-good, re-checking...")
        return run_canary()["ok"]
    return False


def _ensure_venv() -> None:
    """ALWAYS run the OpenFOAM agent in its OWN dedicated venv (default ~/of_agent_venv, override
    with OF_AGENT_PYTHON), never the base/system/anaconda env — so it stays fully isolated from
    unrelated Python environments. Re-exec if we aren't already running that interpreter.
    Compared by PATH (abspath), not realpath — a venv python symlinks to the base python, so realpath
    would wrongly equate them. If the venv is absent, falls back to the current interpreter."""
    if "--no-reexec" in sys.argv or os.environ.get("OF_AGENT_NO_REEXEC"):
        return
    venv_py = os.environ.get("OF_AGENT_PYTHON", str(Path.home() / "of_agent_venv" / "bin" / "python"))
    if os.path.exists(venv_py) and os.path.abspath(venv_py) != os.path.abspath(sys.executable):
        print(f"   ↻ switching to the dedicated venv: {venv_py}", flush=True)   # flush BEFORE execv (replaces process)
        sys.stdout.flush()
        os.execv(venv_py, [venv_py, os.path.abspath(__file__), *sys.argv[1:], "--no-reexec"])


def main() -> int:
    _ensure_venv()
    args = [a for a in sys.argv[1:] if a != "--no-reexec"]    # don't leak the guard flag to agent.py
    if args and args[0] == "--canary":
        c = run_canary("full" if "full" in args else "fast")
        print(json.dumps(c, ensure_ascii=False, indent=2))
        return 0 if c["ok"] else 1
    if args and args[0] == "--rollback" and len(args) > 1:
        ok = restore(args[1])
        print("rollback:", "ok" if ok else "failed (no such snapshot)", args[1])
        return 0 if ok else 1

    print("🌱 bootstrap: supervising the self-evolving agent")
    if not preflight():
        print("❌ code is broken and known-good rollback is not possible — manual inspection needed")
        return 1
    while True:
        if PROMOTE_SIGNAL.is_file():
            PROMOTE_SIGNAL.unlink()
        proc = subprocess.run([sys.executable, str(PROJECT / "agent.py"), *args])
        promoted = PROMOTE_SIGNAL.is_file()
        if proc.returncode == RESTART_CODE or promoted:
            print("\n🔄 kernel self-edit detected — validating, then restarting")
            c = run_canary()
            if PROMOTE_SIGNAL.is_file():
                PROMOTE_SIGNAL.unlink()
            if c["ok"]:
                print("   ✅ canary passed — known-good updated:", _mark_known_good())
            else:
                print("   ❌ canary failed:", [f["check"] for f in c.get("failed", [])], "→ auto-rollback")
                _restore_known_good()
            continue
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
