#!/usr/bin/env bash
# myOpenFoamAgent 런처 — 전용 venv(~/of_agent_venv)의 python으로 agent.py 실행.
#   ./run.sh                                  # 대화형
#   ./run.sh --task "cavity 셋업하고 돌려줘"    # 한 번에
#   ./run.sh --task "..." --yes               # 승인 자동
#
# venv가 없으면 anthropic이 깔린 system python(/usr/bin/python3)으로 폴백한다.
set -euo pipefail

# UTF-8 강제 — 한글 입출력 안전 (콘솔/로케일 무관)
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
: "${LANG:=C.UTF-8}"; export LANG

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$HOME/of_agent_venv/bin/python"
PY="$VENV_PY"
[ -x "$PY" ] || PY="/usr/bin/python3"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ANTHROPIC_API_KEY 가 설정되지 않았습니다." >&2
    echo '  export ANTHROPIC_API_KEY=sk-ant-...' >&2
    exit 1
fi

exec "$PY" "$HERE/agent.py" "$@"
