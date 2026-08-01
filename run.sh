#!/usr/bin/env bash
# myOpenFoamAgent launcher — runs agent.py with the dedicated venv python (~/of_agent_venv).
#   ./run.sh                                  # interactive
#   ./run.sh --task "set up cavity and run it"   # one-shot
#   ./run.sh --task "..." --yes               # auto-approve gated tools
#
# Falls back to the system python (/usr/bin/python3) if the venv is missing but anthropic is installed.
set -euo pipefail

# Force UTF-8 — safe non-ASCII I/O regardless of console/locale
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
: "${LANG:=C.UTF-8}"; export LANG

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$HOME/of_agent_venv/bin/python"
PY="$VENV_PY"
[ -x "$PY" ] || PY="/usr/bin/python3"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Environment variable ANTHROPIC_API_KEY is not set." >&2
    echo '  export ANTHROPIC_API_KEY=sk-ant-...' >&2
    exit 1
fi

exec "$PY" "$HERE/agent.py" "$@"
