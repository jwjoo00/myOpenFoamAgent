# SETUP — setting up a new machine

> Goal: get `python bootstrap.py` running on another computer. The essentials are **WSL2/Linux + OpenFOAM 12 + Python venv + API key**.

---

## 0. Prerequisite — execution environment

OpenFOAM ships as Linux binaries, so you need **WSL2 (on Windows) or native Linux**. Windows alone will not work.
- On Windows: install WSL2 (`wsl --install`) → Ubuntu 22.04 recommended
- The source code can live anywhere, but heavy RUNS data is faster on ext4 (the Linux home directory)

---

## 1. Install OpenFOAM 12 (Foundation)

Version 12 from openfoam.org (note: **not** ESI). On Ubuntu:
```bash
sudo sh -c "wget -O - https://dl.openfoam.org/gpg.key | gpg --dearmor | tee /etc/apt/trusted.gpg.d/openfoam.gpg > /dev/null"
sudo add-apt-repository http://dl.openfoam.org/ubuntu
sudo apt update && sudo apt install openfoam12
# verify
source /opt/openfoam12/etc/bashrc && blockMesh -help | head -1
```
If the install path is not `/opt/openfoam12`, point `OPENFOAM_BASHRC` in `config.py` (or the environment variable) at it.
> CHT uses `/opt/openfoam12/tutorials/multiRegion/CHT/*` as its starting point — included in the installation.

---

## 2. Python venv + dependencies

```bash
# dedicated venv (never mix with other anaconda/system envs — isolation principle)
python3 -m venv ~/of_agent_venv
~/of_agent_venv/bin/pip install --upgrade pip
~/of_agent_venv/bin/pip install -r requirements.txt
```
If the venv path is not `~/of_agent_venv`, set `export OF_AGENT_PYTHON=/path/to/venv/bin/python` (bootstrap auto-switches to it).

---

## 3. bubblewrap (optional — for the self-evolution sandbox)

Required if you want the plugin sandbox / autonomous evolution. Without it the system is fail-closed (autonomy OFF, plugin execution refused).
```bash
sudo apt install bubblewrap   # bwrap
bwrap --version               # verify
```
If you only do chat-driven (human-approved) CFD work, you do not need bwrap.

---

## 4. Environment variables

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # required (the agent calls the Claude API)
# optional (defaults exist):
export OF_AGENT_RUNS=~/of_agent_runs     # location of case/result data (ext4 recommended, outside Dropbox)
export OF_AGENT_PYTHON=~/of_agent_venv/bin/python   # dedicated venv
# export OF_AGENT_WEB=1                   # enable web search/download (web search must be enabled in the Console)
```
Handy to put these in `.bashrc`.

---

## 5. Verify it works

```bash
cd <project>
python bootstrap.py --canary     # all 13 checks must pass (if not, see troubleshooting below)
python bootstrap.py              # run the agent → the 🧑 prompt appearing means success
```

Quick CFD smoke test (from the chat window):
- `"set up the cavity tutorial and run it briefly"` (external-flow basics)
- `"copy multiRegion/CHT/coolingCylinder2D, run it with run_cht, and show me cht_report"` (CHT)

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `preflight canary failed: geometry_watertight` + `No module named 'trimesh'` | `python` is not the venv → run via the venv or `pip install -r requirements.txt`. bootstrap auto-switches, but check the OF_AGENT_PYTHON path |
| `blockMesh: command not found` | OpenFOAM bashrc not sourced / not installed. Check `config.py OPENFOAM_BASHRC` |
| `ANTHROPIC_API_KEY is not set` | `export ANTHROPIC_API_KEY=...` |
| Autonomous evolution / plugin execution refused | bwrap not installed (normal fail-closed). `apt install bubblewrap`, or stick to chat-driven use |
| canary `security_*` failure | A security invariant is broken — review recent hand-edits (config/agent/tools), restore from `backups/` |
| `MPI_ABORT` in parallel CHT | Small mesh over-decomposed. Use `run_cht(..., parallel=False)` or a smaller `nprocs` |

---

## 7. Notes on file permissions / paths

- `config.py` absorbs every machine-dependent path into an environment variable → adapts to a new machine without code changes
- `RUNS_ROOT` (case data) is best kept on **ext4 outside Dropbox** (avoids sync load and conflicts)
- Only the source (`*.py`) needs to move via git/Dropbox. RUNS data can be regenerated (validation cases are reproduced by the procedure in HANDOFF.md)

Full project overview · lessons · roadmap → **HANDOFF.md**.
