# CLAUDE.md — myOpenFoamAgent 프로젝트 작업 규칙

> **이 파일은 Claude Code가 이 폴더에서 작업할 때의 규칙입니다.**
> 프로젝트 개요·교훈·로드맵은 `HANDOFF.md`, 환경 구성은 `SETUP.md`, 사용법은 `README.md`.

## 무엇인가

Claude가 tool-use로 **OpenFOAM 12를 직접 운전**하는 대화형 CAE 에이전트 + **자가진화(self-evolving) 프레임워크**.
외부유동 RANS · 회전기계(MRF) · 공액열전달(CHT)을 자동화하고, 결과를 **문헌과 정량 검증**한다.

## 실행 (항상 이렇게)

```bash
python bootstrap.py --canary      # 건강검진: 13 checks 통과해야 정상
python bootstrap.py               # 에이전트 실행 (전용 venv로 자동 전환)
python bootstrap.py --list-sessions          # 저장된 대화 목록
python bootstrap.py --resume sess_<id>       # 이전 대화 이어받기
```
- **Python은 반드시 전용 venv** `~/of_agent_venv/bin/python` (시스템/anaconda env 사용 금지 — 격리 원칙).
  bootstrap이 자동 전환하지만, 직접 스크립트 돌릴 땐 venv 경로를 명시할 것.
- WSL 명령은 리포 절대경로를 사용.

## 코드 수정 규칙 (반드시 지킬 것)

1. **백업 → 검증 → diff** 순서. 백업은 `backups/<파일>_backup_<YYYYMMDD>_<이유>.py`
2. 수정 후 **반드시** `py_compile` + `python bootstrap.py --canary` (13 checks) 확인
3. **한 번에 큰 변경 금지** — 작은 변경 → 검증 → 다음
4. 새 기능은 **additive**로 (기존 동작 보존). 기존 sibling을 복사 후 의도한 것만 변경
5. **PROTECTED 파일**(`agent.py`·`config.py`·`foam.py`·`selfevolve.py`·`bootstrap.py`)은
   자가진화 엔진이 못 건드림 → **사람(=Claude Code)이 손편집**. 특히 신중하게.

## 파일 맵 (핵심만)

| 파일 | 역할 |
|---|---|
| `agent.py` | LLM 루프 · SYSTEM 프롬프트(운영원칙 1~11) · 승인 게이트 `_approve` · REPL |
| `tools.py` | **도구 46개** — `DISPATCH`(이름→함수)/`TOOL_SPECS`(스키마)/`GATED`(승인 필요) |
| `config.py` | 경로 · `ALLOWED_BINS`(실행 허용 바이너리) · MPI · 웹/캐시 |
| `foam.py` | OF 실행 래퍼 — `run_foam`(blocking) / `run_foam_bg`(detached) / `bg_stop` |
| `selfevolve.py` | 자가진화 엔진 — 플러그인 핫리로드 · canary · incubator · 스냅샷 · bwrap 샌드박스 |
| `bootstrap.py` | 불변 신뢰 루트 — venv 자동전환 · preflight canary · 감독 · 자동롤백 |
| `propgen.py` | CAD-free 프로펠러(DTMB 표 · Wageningen B-series · open-water 다항식) |
| `meshgen.py` · `rotordiag.py` · `registry.py` · `session.py` | 메시/수렴파싱 · 회전기계 진단 · run DB · 대화 영속 |
| `tools_plugins/tool_*.py` | 핫리로드 플러그인 6개(`SPEC`+`handler`, `READONLY=True`면 ungated) |

## 데이터 위치

| | |
|---|---|
| 소스 | 이 폴더 (Dropbox 동기화) |
| **실행 데이터** | `~/of_agent_runs` (ext4, Dropbox 밖) — 케이스·`jobs.json`·`sessions/`·`registry.db`·`selfevolve/` |
| 산출물 | `reports/` (HTML·PNG·xlsx — Dropbox 동기화) |

## 자주 쓰는 명령

```bash
# 도구 정합성 + 개수
~/of_agent_venv/bin/python -c "import tools; assert set(tools.DISPATCH)=={s['name'] for s in tools.TOOL_SPECS}; print(len(tools.TOOL_SPECS))"
# 백그라운드 잡 확인 (에이전트 밖에서)
cat ~/of_agent_runs/jobs.json
# 세션(대화) 목록
ls -lat ~/of_agent_runs/sessions/ | head
```

## 꼭 기억할 함정 (겪은 것들)

- **MRF 회전벽은 `MRFnoSlip`** — `noSlip`이면 블레이드가 안 돈다(추력=drag만). thrust 부호 이상하면 형상보다 MRF 설정을 먼저 의심.
- **`run_solver`는 background 기본** — job_id 즉시 반환, `job_status`로 확인, `stop_job`으로 중단(checkpoint 보존).
- **프로세스 생존 판정은 좀비 주의** — `os.kill(pid,0)`은 좀비를 살아있다고 오판. `/proc/<pid>/stat` state 사용(`foam._running`).
- **canary가 optional 의존성으로 brick되면 안 됨** — 보안/코드는 fail-closed, 누락 의존성(trimesh 등)은 skip.
- **MSYS 버그** — Bash 도구에서 `wsl.exe -- bash -lc '...'` 안의 `$VAR`·for-loop·`$(...)`가 먹힘. **literal path** 쓸 것.
- **모델이 도구를 텍스트로 호출**(`<invoke>` XML)하는 경우가 있음 — `agent.py`가 파싱해 복구 실행하도록 이미 처리됨.
- **자가진화는 무인운영용** — 키보드 앞이면 Claude Code 직접 편집이 더 안전·편함. `set_auto_evolve`는 기본 OFF 유지.

## 응답 스타일

한국어+영어 혼용, 기술 용어는 영어. **표·핵심 위주로 간결하게** (장황한 prose 비선호).
