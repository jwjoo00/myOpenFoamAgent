# myOpenFoamAgent

대화형 **OpenFOAM CFD 에이전트** — Claude(claude-opus-4-8)가 **tool-use로 직접 운전**하며
케이스 정의 → 격자 → 솔버 설정 → 실행 → 후처리 → 보고서를 **단계별로 제안하고 당신 확인을
받아** 진행한다. 한 번 진행한 run의 설정·결과는 저장되어 **나중에 LLM이 참고·제시**한다.

- **실행 환경**: WSL2 / Ubuntu, **OpenFOAM 12 (Foundation, openfoam.org)** — `foamRun` +
  `incompressibleFluid`, `momentumTransport`, `physicalProperties`, `surfaceFeatures`.
- **case 데이터 위치**: `~/of_agent_runs` (네이티브 ext4 — 빠른 IO, Dropbox 비동기).
- **report 위치**: `myOpenFoamAgent/reports/<run_id>/` (REPORT.md + png만, Dropbox 동기화).

## 구조

| 파일 | 역할 | 단계 |
|------|------|------|
| `config.py` | 경로 · OF env · 모델 · 실행 allowlist | 0 |
| `foam.py` | OF env source 후 allowlist 실행 + 로그 파서 (LLM 무관) | 0 |
| `smoketest.py` | cavity 복사→blockMesh→checkMesh→foamRun (LLM 없이 배관 검증) | 0 |
| `registry.py` | run 설정·결과 SQLite 저장 + **단계별 성공 레시피 누적**(merge_results: mesh→bl→ras→solve) → find_similar_runs로 재사용 (LLM 무관) | 0 ✅ |
| `test_registry.py` | registry 라운드트립 검증 (LLM 없이) | 0 |
| `tools.py` | typed tool 20종 + Anthropic 스키마 (…·generate_mesh·add_boundary_layers·run_snappy_mesh·scaffold_external_ras) | 1 ✅ |
| `xlsx.py` | 엑셀 빌더 — 표 + 이미지 임베드 (build_excel가 사용, LLM 무관) | — ✅ |
| `mesher.py` | gmsh 메싱 엔진 (geometry → mesh.msh v2.2 + 품질지표, LLM 무관) | 3 ✅ |
| `meshgen.py` | caeloop repair 루프 + 외부 STL 품질 self-improve 루프 + **snappy addLayers BL**(add_boundary_layers) + gmshToFoam + checkMesh | 3 ✅ |
| `qexp.py` | gmsh 외부메시 품질 knob study 하니스 — self-improve 정책의 근거 데이터 도출(LLM 무관) | 3 ✅ |
| `caeloop/` | validator-gated repair FSM 패키지 (mesh_generator_example 복사 + `__init__`) | 3 ✅ |
| `geom.py` | CAD 없이 인터넷 형상 다운로드 (motorBike.obj·UIUC airfoil·url, urllib) | 3 ✅ |
| `templates/cylinder_2d/` · `templates/airfoil_2d/` | 풀 수 있는 2D 케이스 스캐폴드 (laminar, U·p; airfoil 패치) | 3 ✅ |
| `agent.py` | Claude tool-use 루프 + 단계별 승인 게이트 (진입점) | 1 ✅ |
| `postproc.py` | residual 수렴 + 속도장(tricontourf) + REPORT.md (LLM 무관) | 2 ✅ |
| `session.py` | 대화 영속(저장/resume) — prompt caching·context 관리는 `agent.py` 내장 | 4 ✅ |
| `run.sh` | 전용 venv(`~/of_agent_venv`)로 agent 실행하는 런처 | — |

## 진행 상태

- **Phase 0 (배관 + 영속) — 완료 · 검증됨** ✅
  - `smoketest.py` PASS: cavity 400 cells, foamRun t=0.5 정상.
  - `test_registry.py` PASS: 저장/조회/유사run 검색.
- **Phase 1 (tool-use 대화 루프) — 완료 · 라이브 검증** ✅ (cavity copy→blockMesh→checkMesh 실제 Claude API로 작동, registry 저장 확인).
- **Phase 2 (후처리/보고서) — 완료 · 검증** ✅ (`build_report`: residual 수렴 + 속도장 + REPORT.md).
- **Phase 4 (context 관리) — 부분 완료** ✅: 대화 영속(`--resume`) · prompt caching(rolling cache_control) · 긴 세션 trim/compaction/context-editing. MPI 자동 병렬도 포함.
- **Phase 3 (gmsh 메싱) — 핵심 완료·검증** ✅: `generate_mesh` = gmsh + **caeloop validator-gated repair**(휴리스틱→LLM) → `gmshToFoam` → front/back empty(2D) → `checkMesh`. 2D cylinder repair 루프 실증(too-coarse → 자동수정 3회 → 9409 cells, min SICN 0.146, Mesh OK).
- **`fetch_geometry` — 완료·실측 검증** ✅: motorBike.obj(10.7MB) · UIUC airfoil(naca0012→n0012.dat) · 임의 url(.stl/.obj/.dat) 다운로드. "CAD 없이 인터넷 형상" 다운로드 닫힘.
- **`generate_mesh(external_stl)` — 완료·검증** ✅: 더러운 외부 STL을 **snappy 없이 gmsh로** 메싱 + **품질 self-improve 루프**. motorBike(331k tris, non-watertight) → pymeshfix repair(watertight)+decimate → gmsh 외부 volume(차체 띄움) → gmshToFoam → checkMesh를 한 라운드로, 미달이면 data로 검증된 knob(skew↑→Frontal algo, nonortho/sicn↓→faces↑)을 적용해 재메시하고 **best를 케이스에 복원**. 라이브: round1 HXT(skew 3.78)→round2 **Frontal(skew 3.12, Mesh OK, 67k cells)** 로 자동 개선.
- **gmsh 품질 self-improve + 실측 한계** 🧱 ([`QUALITY_STUDY.md`](QUALITY_STUDY.md)): 표면보존 lever 9종(knobs·polyDual·pyacvd·manifold3d·**tetgen -q**)은 전부 non-ortho ~89°에 막힘. 하지만 **벽면 smoothing(STL을 펴서, humphrey+재close)으로 89°→~83° 돌파** — 원본 대비 변형 **max ~1.3%**만, `stl_deviation`으로 측정·보고. 루프가 skew→Frontal, non-ortho→smoothing(deviation 예산 안)으로 자동 개선하고 best를 케이스에 복원. 여전히 RANS-ideal(<70°)은 thin feature 때문에 못 넘어 **`nNonOrthogonalCorrectors`로 보정 solve 가능**(못 쓰는 메시 아님); 진짜 한계는 **boundary layer 부재**(벽면 drag). 한계 시 `snappy_suggested`+`snappy_reason`(변형량+개선)+`solver_hint`로 사용자에게 "gmsh 보정 solve vs snappy(hex+layers)" 선택 제시(원칙 7).
- **`add_boundary_layers` — 3D 벽면 BL, 완료·검증** 🧱➕: gmsh는 3D boundary layer 불가(BoundaryLayer field 2D 전용, 실측) → **gmsh tet 메시 위에 snappyHexMesh addLayers-only**로 벽면 prism 층을 넣는 하이브리드. coverage는 벽 해상도가 좌우(20k→67%)라 layer 전에 벽을 wall_faces(기본 30k)로 재메싱 → motorBike 라이브: **coverage 79%, 평균 2.5 layers, 126,523 cells**, non-ortho 88.8(보정), skew 3.68, Mesh OK. 남은 ~21%는 razor-thin 부품(snappy full도 동일). 외부유동 RANS 벽면 정확도(drag·Cf)의 필수 조각.
- **`scaffold_external_ras` + 난류/Re 옵션 + 성공 레시피 DB — 완료·검증** 🗄️: external_stl/snappy→add_boundary_layers→**scaffold_external_ras**→run_solver(MPI)→build_report 전구간 연결. **난류 모델 선택(kOmegaSST·SpalartAllmaras·kEpsilon·laminar) + Re(=U·ν) 자유 설정** — 모델별 필드(k/omega·nuTilda·epsilon)·BC·wall function·div schemes·solvers 자동 생성. forceCoeffs로 drag/lift, limited schemes·nNonOrthogonalCorrectors로 고 non-ortho 안정화. registry가 **단계별 레시피(mesh·bl·ras·solve)를 한 row에 누적**(merge_results) + cd/cl 기록 → find_similar_runs로 LLM 재사용.
- **`generate_mesh(airfoil_2d)` + 양력/항력 — 완료·검증** ✅: fetch_geometry(airfoil .dat) → 2D gmsh(airfoil 패치, aoa/chord) → scaffold(laminar) + **forceCoeffs(Cl/Cd/Cm) 자동 주입**. naca0012 aoa5°: 8934 cells, non-ortho 35, Mesh OK → solve → **Cl 0.29, Cd 0.45, Cm 0.01**(@Re100 laminar). parse_force_coeffs로 읽혀 registry에 cd/cl 기록.
- **snappy 정량 drag 레퍼런스 — 확정** 🎯: OF12 motorBikeSteady(snappy hex 354k) → **Cd 0.40 수렴(stdev 3e-4)**, registry에 성공 레시피로 기록(find_similar_runs로 재사용). gmsh-tet(non-ortho 88)는 발산(±360) — 정량 drag엔 snappy hex 확정.
- **`generate_mesh(external_netgen)` — clean 형상 BL, 완료·검증** ✅: **clean watertight** 형상용. STL→OCC solid(OCP cadquery-ocp sew)→netgen OCC(box−body)를 **prism boundary layer까지 한 번에** 메싱→netgen→gmsh .msh 변환(tet/prism node 순서 교정)→gmshToFoam. sphere 검증: 126k cells, **prism 3840, non-ortho 51, skew 0.52, Mesh OK**. (바이크 같은 dirty/thin은 netgen BL이 깨져 external_stl+snappy 사용 — `QUALITY_STUDY.md`.)
- **`run_snappy_mesh` — generic snappy-full, 완료·검증** ✅🎯: 임의 STL → 단일STL 병합(OBJ region→1 body 패치) + blockMesh 배경박스(bbox) + surfaceFeatures + **full snappyHexMesh(castellate+snap+addLayers, parallel)** → reconstructPar(OF12) → checkMesh. motorBike 라이브: **95,902 cells, non-ortho 64.7**, scaffold_external_ras→solve→**Cd 0.415 수렴(stdev 0)** = snappy 튜토리얼 ref Cd 0.40과 일치. **이제 에이전트가 어떤 다운로드 STL이든 정량 drag까지** 간다(gmsh-tet는 발산). 핵심 수정: 단일STL 병합·reconstructPar·scaffold body 탐색. 성공 레시피 DB 기록.
- **웹 검색·다운로드 (web_search + web_fetch) — 완료, opt-in** 🌐: Anthropic **서버 도구**를 루프에 연결해 에이전트가 **인터넷을 직접 검색하고 페이지를 읽는다**. 둘 다 Anthropic 서버가 실행 → 루프는 dispatch하지 않고 `server_tool_use`/`web_*_tool_result` 블록을 그대로 보존(`_serialize_content` model_dump), `pause_turn` 시 턴 재개. 흐름: `web_search`로 형상 STL/OBJ URL·레퍼런스값을 찾고 → 그 URL을 `fetch_geometry(kind=url)`로 내려받아(확장자 whitelist) → `generate_mesh`로 잇는다. **기본 OFF**(`OF_AGENT_WEB=1`로 켬; Console에서 web search 활성화 필요, $10/1000 검색). 보안: web_fetch는 대화에 이미 등장한 URL만(임의 URL 생성 불가), 다운로드는 urllib+whitelist. → 다이어그램(이 세션) 참고.
- **수렴-주도 iteration (residualControl) + cap DB 재사용 — 완료·실측 검증** 🎯: scaffold_external_ras가 OF12 drivaerFastback 방식 **residualControl**(residual_tol 기본 1e-4)을 써서 p·U·난류필드 잔차가 모두 그 밑이면 **수렴 시 자동 정지**; end_time은 고정 반복수가 아니라 **상한(cap)**. parse_convergence가 수렴 여부·converged_iters·잔차를 판별하고 run_solver가 DB에 기록 → 다음 run의 cap을 find_similar_runs(과거 converged_iters)·web_search로 결정(SYSTEM 원칙8). 라이브: bike 95k가 cap 1000 중 **325 iter에서 자동 수렴**(잔차 ~3e-7, Cd 0.422). **적대적 리뷰가 HIGH 버그 2개를 잡아 수정**: (1) converged는 OF 명시 메시지로만 인정(timeout·kill·high-Courant 발산이 '수렴'으로 둔갑 차단), (2) run_solver가 clean-exit + foam 발산판정(Courant/NaN)으로 교차검증 → DB에 가짜 converged_iters 없음.
- **회전기계 (MRF) — rotating fan/propeller, 완료·실측 검증** 🌀: rotating machinery를 **Multiple Reference Frame**(steady)로 푼다. `scaffold_mrf`(constant/MRFProperties + 회전 cellZone + 회전벽 noSlip + forces) → run_solver(residualControl 자동수렴) → `rotor_performance`(축방향 thrust·torque·power=τ·ω). OF12 `incompressibleFluid/propeller`+`mixerVessel2DMRF` ground-truth. **라이브**: OF12 native marine propeller(D=0.227m, local geometry) → blockMesh+snappy **525k cells + innerCylinder cellZone** → MRF 1500rpm/물/5m/s → **930 iter 자동수렴**(잔차 1e-7) → **T=782N, Q=20.9N·m, P=3.28kW** → 무차원 J=0.88·Kt=0.47·10Kq=0.55. **문헌비교+정직한 발견**: η₀=1.19>1(비물리) → 데모 operating point가 off-design(과부하)임을 워크플로가 surfacing — method(MRF 검증문헌 thrust<2%)는 건전, 작동점이 문제([REPORT](reports/propellerMRF_REPORT.md), [UIUC PDB](https://m-selig.ae.illinois.edu/props/propDB.html)). 빌드 중 forces 메커니즘 버그 2개(OF12 `system/functions` `#include` 자동로드, regex patch 따옴표)를 실측 solver로 잡아 수정. DB에 mrf·performance 레시피 기록.
- **`generate_propeller` — CAD-free 프로펠러 생성기, 완료·검증** 🛠️🌀: offset table에서 **wrapped-section을 loft**해 propeller.stl(블레이드+허브) + rotatingZone.stl(MRF cellZone) 생성. **두 가지 preset**: ① **DTMB4119**(검증 표준 → Jessup Kt 0.146 대비 0.1464, [dtmb4119_propeller.png](reports/dtmb4119_propeller.png)), ② **Wageningen B-series**(parametric 패밀리 — n_blades=Z·area_ratio=AE/A0·pitch_ratio=P/D로 무한 생성; 라이브 B4-70 검증 c/D@0.7R=0.375, [wageningen_b4_70.png](reports/wageningen_b4_70.png)). handed±1로 추력 방향. "CAD 없이 표/공식에서 형상" 철학. (25 tools)
- **`rotor_report` — 회전기계 사람-판단용 시각 진단, 완료·검증** 👁️🌀: 회전기계 CFD는 정량값만으론 판단이 어렵다(thrust=lift−drag의 작은 net force라 부호가 잘 틀림) → **사람이 형상·방향을 눈으로 봐야 한다**. 6패널 PNG+md: geometry 3D·블레이드 단면(airfoil)·flow vs 축 정렬·회전/thrust/torque 방향 화살표·성능/수렴·**human-judgment 체크리스트**. MRFProperties·0/U·forces·convergence를 읽어 자동 구성. (25 tools)
- **DTMB 4119 정량검증 — 완료·Jessup 일치** ✅🌀🎯: generate_propeller(CAD-free)→cellZone snappy 메시→MRF→Jessup 비교 전 과정 작동. **design J=0.833에서 Kt=0.1476 vs Jessup(1989) 0.146** (RANS 수준 근소 일치는 오차 상쇄를 포함 — 정확도 주장이 아니라 end-to-end 파이프라인 검증·canary 물리 oracle) (10Kq=0.34 vs 0.28, 간이 단면 영향). **진짜 버그 2개는 형상이 아니라 MRF 설정이었음**(사용자 진단): ① 회전벽이 `noSlip`(절대 정지벽)이라 블레이드가 안 돌았음 → **`MRFnoSlip`**(swirl 측정 ≈0으로 적발, MRF zone 활성이어도 noSlip은 안 돈다), ② 회전방향이 손잡이와 반대(noSlip일 땐 정지판이라 무의미했음). scaffold_mrf가 이제 MRFnoSlip 자동 설정. **교훈: 회전기계 thrust 부호가 틀리면 형상보다 MRF 벽 BC·swirl을 먼저 의심.** ([[openfoam-rotating-machinery-human-judgment]])
- **`delete_path` — 안전 삭제 도구, 완료·검증** 🧹: 실패한 메시·오래된 time/processor dir·버린 케이스 정리용. `_confined`로 **RUNS_ROOT·REPORTS_ROOT 안만 허용**(`..`·심링크·바깥 거부), 데이터 루트 자체·시스템·프로젝트 소스는 거부, 폴더는 recursive 필수. GATED(승인). 실측: 시스템경로·루트·바깥·non-recursive 전부 거부, 루트 안만 삭제 확인. (21 tools)
- 남은: 더 많은 형상 · Phase 4 나머지 — 예정.

## 검증 (API 키 불필요)

```bash
# WSL 안에서
python3 smoketest.py        # OpenFOAM 배관 end-to-end
python3 test_registry.py    # 크로스런 registry
```

## 실행 (키 필요)

전용 venv(`~/of_agent_venv`)를 쓰는 런처 권장 — `build_report`의 matplotlib가 venv에 있음:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./run.sh                                              # 대화형
./run.sh --task "cavity 셋업하고 돌려서 리포트까지" --yes   # 한 번에
```

venv 만들기(최초 1회):
```bash
/usr/bin/python3 -m venv ~/of_agent_venv
~/of_agent_venv/bin/pip install -r requirements.txt
```

## 환경변수 (선택)

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | (필수, Phase 1) | Claude API 키 |
| `OF_AGENT_MODEL` | `claude-opus-4-8` | 사용 모델 |
| `OF_AGENT_RUNS` | `~/of_agent_runs` | case 데이터 루트 (ext4) |
| `OF_AGENT_REPORTS` | `<project>/reports` | report 저장 루트 (Dropbox) |
| `OF_AGENT_CACHE` | `1` | prompt caching on/off |
| `OF_AGENT_CONTEXT` | `trim` | 긴 세션 전략: `trim`·`edit`·`compact`·`off` |
| `OF_AGENT_WEB` | `0` | `1`이면 web_search+web_fetch 활성(인터넷 검색·다운로드). Console에서 web search 활성화 필요, $10/1000 검색 |
| `OF_AGENT_WEB_SEARCH_MAX` / `OF_AGENT_WEB_FETCH_MAX` | `8` | 요청당 검색·fetch 최대 횟수 |
| `OF_AGENT_DEBUG` | `0` | `1`이면 호출마다 token usage(cache_read 등) 출력 |
| `OPENFOAM_BASHRC` | `/opt/openfoam12/etc/bashrc` | OF 환경 |
