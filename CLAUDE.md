# CLAUDE.md — myOpenFoamAgent project working rules

> **This file is the set of rules Claude Code follows when working in this folder.**
> Project overview · lessons · roadmap in `HANDOFF.md`, environment setup in `SETUP.md`, usage in `README.md`.

## What this is

A conversational CAE agent in which Claude **drives OpenFOAM 12 directly** through tool-use + a **self-evolving framework**.
It automates external-flow RANS · turbomachinery (MRF) · conjugate heat transfer (CHT), and **validates results quantitatively against the literature**.

## Running it (always this way)

```bash
python bootstrap.py --canary      # health check: all 13 checks must pass
python bootstrap.py               # run the agent (auto-switches to the dedicated venv)
python bootstrap.py --list-sessions          # list saved conversations
python bootstrap.py --resume sess_<id>       # resume a previous conversation
```
- **Python must always be the dedicated venv** `~/of_agent_venv/bin/python` (never the system/anaconda env — isolation principle).
  bootstrap switches automatically, but when running a script directly, spell out the venv path.
- WSL commands use the absolute path `<repo>`.

## Code modification rules (must be followed)

1. **backup → verify → diff**, in that order. Backups go to `backups/<file>_backup_<YYYYMMDD>_<reason>.py`
2. After editing, **always** confirm `py_compile` + `python bootstrap.py --canary` (13 checks)
3. **No big changes at once** — small change → verify → next
4. Add new features **additively** (preserve existing behavior). Copy an existing sibling, then change only what you intend
5. **PROTECTED files** (`agent.py`·`config.py`·`foam.py`·`selfevolve.py`·`bootstrap.py`) are
   off-limits to the self-evolution engine → **hand-edited by a human (= Claude Code)**. Be especially careful with them.

## File map (essentials only)

| File | Role |
|---|---|
| `agent.py` | LLM loop · SYSTEM prompt (operating principles 1~11) · approval gate `_approve` · REPL |
| `tools.py` | **46 tools** — `DISPATCH` (name→function) / `TOOL_SPECS` (schemas) / `GATED` (approval required) |
| `config.py` | paths · `ALLOWED_BINS` (binaries allowed to run) · MPI · web/cache |
| `foam.py` | OF execution wrappers — `run_foam` (blocking) / `run_foam_bg` (detached) / `bg_stop` |
| `selfevolve.py` | self-evolution engine — plugin hot-reload · canary · incubator · snapshots · bwrap sandbox |
| `bootstrap.py` | immutable root of trust — venv auto-switch · preflight canary · supervision · auto-rollback |
| `propgen.py` | CAD-free propellers (DTMB tables · Wageningen B-series · open-water polynomials) |
| `meshgen.py` · `rotordiag.py` · `registry.py` · `session.py` | mesh / convergence parsing · turbomachinery diagnostics · run DB · conversation persistence |
| `tools_plugins/tool_*.py` | 6 hot-reloadable plugins (`SPEC`+`handler`; ungated if `READONLY=True`) |

## Data locations

| | |
|---|---|
| Source | this folder (Dropbox-synced) |
| **Run data** | `~/of_agent_runs` (ext4, outside Dropbox) — cases·`jobs.json`·`sessions/`·`registry.db`·`selfevolve/` |
| Outputs | `reports/` (HTML·PNG·xlsx — Dropbox-synced) |

## Frequently used commands

```bash
# tool consistency + count
~/of_agent_venv/bin/python -c "import tools; assert set(tools.DISPATCH)=={s['name'] for s in tools.TOOL_SPECS}; print(len(tools.TOOL_SPECS))"
# check background jobs (from outside the agent)
cat ~/of_agent_runs/jobs.json
# list sessions (conversations)
ls -lat ~/of_agent_runs/sessions/ | head
```

## Pitfalls worth remembering (learned the hard way)

- **MRF rotating walls must be `MRFnoSlip`** — with `noSlip` the blades do not rotate (thrust = drag only). If the thrust sign looks wrong, suspect the MRF setup before the geometry.
- **`run_solver` defaults to background** — returns a job_id immediately, check with `job_status`, stop with `stop_job` (checkpoint preserved).
- **Watch out for zombies when deciding if a process is alive** — `os.kill(pid,0)` misjudges a zombie as alive. Use the `/proc/<pid>/stat` state instead (`foam._running`).
- **The canary must never be bricked by an optional dependency** — security/code checks are fail-closed, missing dependencies (trimesh etc.) are skipped.
- **MSYS bug** — in the Bash tool, `$VAR`·for-loops·`$(...)` inside `wsl.exe -- bash -lc '...'` get eaten. Use **literal paths**.
- **The model sometimes calls tools as plain text** (`<invoke>` XML) — already handled: `agent.py` parses those and runs them as a recovery path.
- **Self-evolution is for unattended operation** — when you are at the keyboard, editing directly with Claude Code is safer and easier. Keep `set_auto_evolve` OFF by default.

## Response style

Mixed Korean + English, technical terms in English. **Concise, tables and key points first** (long-winded prose is not preferred).
