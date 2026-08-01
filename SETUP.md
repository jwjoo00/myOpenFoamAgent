# SETUP — 새 머신 환경 구성

> 목표: 다른 컴퓨터에서 `python bootstrap.py`가 돌게 만들기. 핵심은 **WSL2/Linux + OpenFOAM 12 + Python venv + API 키**.

---

## 0. 전제 — 실행 환경

OpenFOAM은 Linux 바이너리라 **WSL2(Windows) 또는 네이티브 Linux**가 필요합니다. Windows 단독 불가.
- Windows라면: WSL2 설치(`wsl --install`) → Ubuntu 22.04 권장
- 소스 코드는 어디 둬도 되지만, 무거운 RUNS 데이터는 ext4(리눅스 홈)에 두는 게 빠름

---

## 1. OpenFOAM 12 (Foundation) 설치

openfoam.org(ESI 아님 주의) 버전 12. Ubuntu 기준:
```bash
sudo sh -c "wget -O - https://dl.openfoam.org/gpg.key | gpg --dearmor | tee /etc/apt/trusted.gpg.d/openfoam.gpg > /dev/null"
sudo add-apt-repository http://dl.openfoam.org/ubuntu
sudo apt update && sudo apt install openfoam12
# 확인
source /opt/openfoam12/etc/bashrc && blockMesh -help | head -1
```
설치 경로가 `/opt/openfoam12`가 아니면 `config.py`의 `OPENFOAM_BASHRC` 또는 환경변수로 지정.
> CHT는 `/opt/openfoam12/tutorials/multiRegion/CHT/*`를 시작점으로 사용 — 설치본에 포함됨.

---

## 2. Python venv + 의존성

```bash
# 전용 venv (다른 anaconda/system env와 섞지 말 것 — 격리 원칙)
python3 -m venv ~/of_agent_venv
~/of_agent_venv/bin/pip install --upgrade pip
~/of_agent_venv/bin/pip install -r requirements.txt
```
venv 경로가 `~/of_agent_venv`가 아니면 `export OF_AGENT_PYTHON=/path/to/venv/bin/python` (bootstrap이 이걸로 자동 전환).

---

## 3. bubblewrap (선택 — 자가진화 샌드박스용)

플러그인 샌드박스/자율진화를 쓰려면 필요. 없으면 fail-closed(자율 OFF, 플러그인 실행 거부).
```bash
sudo apt install bubblewrap   # bwrap
bwrap --version               # 확인
```
챗-드리븐(사람 승인) CFD 작업만 할 거면 bwrap 없어도 됨.

---

## 4. 환경변수

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # 필수 (에이전트가 Claude API 호출)
# 선택 (기본값 있음):
export OF_AGENT_RUNS=~/of_agent_runs     # 케이스/결과 데이터 위치 (ext4 권장, Dropbox 밖)
export OF_AGENT_PYTHON=~/of_agent_venv/bin/python   # 전용 venv
# export OF_AGENT_WEB=1                   # 웹 검색·다운로드 켜기 (Console에서 web search 활성화 필요)
```
`.bashrc`에 넣어두면 편함.

---

## 5. 동작 확인

```bash
cd <project>
python bootstrap.py --canary     # 13 checks 통과해야 정상 (실패 시 아래 트러블슈팅)
python bootstrap.py              # 에이전트 실행 → 🧑 프롬프트 뜨면 성공
```

빠른 CFD 동작 테스트(대화창에서):
- `"cavity 튜토리얼 셋업하고 짧게 돌려줘"` (외부유동 기본)
- `"multiRegion/CHT/coolingCylinder2D 복사해서 run_cht로 돌리고 cht_report 보여줘"` (CHT)

---

## 6. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `preflight canary 실패: geometry_watertight` + `No module named 'trimesh'` | `python`이 venv가 아님 → venv로 실행하거나 `pip install -r requirements.txt`. bootstrap이 자동 전환하지만, OF_AGENT_PYTHON 경로 확인 |
| `blockMesh: command not found` | OpenFOAM bashrc 미source / 미설치. `config.py OPENFOAM_BASHRC` 확인 |
| `ANTHROPIC_API_KEY 가 설정되지 않았습니다` | `export ANTHROPIC_API_KEY=...` |
| 자율진화/플러그인 실행 거부 | bwrap 미설치(정상 fail-closed). `apt install bubblewrap` 또는 챗-드리븐만 사용 |
| canary `security_*` 실패 | 보안 불변식 깨짐 — 최근 손편집(config/agent/tools) 점검, `backups/`에서 복구 |
| 병렬 CHT `MPI_ABORT` | 작은 메시 과분해. `run_cht(..., parallel=False)` 또는 `nprocs` 작게 |

---

## 7. 파일 권한/경로 메모

- `config.py`가 모든 머신-의존 경로를 환경변수로 흡수 → 코드 수정 없이 새 머신 적응 가능
- `RUNS_ROOT`(케이스 데이터)는 **Dropbox 밖 ext4** 권장 (동기화 부담·충돌 방지)
- 소스(`*.py`)만 git/Dropbox로 옮기면 됨. RUNS 데이터는 재생성 가능 (검증 케이스는 HANDOFF.md 절차로 재현)

자세한 프로젝트 개요·교훈·로드맵 → **HANDOFF.md**.
