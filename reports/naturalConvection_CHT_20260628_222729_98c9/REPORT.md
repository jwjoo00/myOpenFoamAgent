# Natural Convection CHT — circuitBoardCooling (OpenFOAM 12)

**Run ID:** `naturalConvection_CHT_20260628_222729_98c9`
**Case kind:** multiRegion CHT (conjugate heat transfer) + natural convection
**Source:** `$FOAM_TUTORIALS/multiRegion/CHT/circuitBoardCooling`
**Date:** 2026-06-28

---

## 1. 목표
발열 회로보드(solid)를 공기 자연대류(buoyancy-driven)로 냉각하는
**conjugate heat transfer (CHT)** 해석. fluid(공기) + baffle3D(solid) 두 region을
함께 풀어 고체-유체 열결합을 계산.

## 2. 물리 / 솔버 설정 (OF12 Foundation 문법)
| 항목 | 설정 |
|---|---|
| application | `foamMultiRun` (regionSolvers: fluid=fluid, baffle3D=solid) |
| 중력 | `g = (0, -9.81, 0)` → **buoyancy 구동** |
| 상태방정식 | `heRhoThermo` + `perfectGas` (밀도-온도 결합) |
| 압력변수 | `p_rgh` = p − ρgh (정수압 분리, 자연대류 표준) |
| 난류 | RAS kEpsilon |
| 시간진행 | transient, deltaT=1, residualControl(U·h 1e-4) |
| 도메인 | 1.0 × 0.5 × 0.1 m 채널 (2D, front/back empty) |

## 3. 메시 (multiRegion extrude 체인)
생성 절차: `blockMesh -region fluid` → `topoSet` → `extrudeToRegionMesh`
(baffle3D solid region 생성) → `createBaffles` (1D baffle).

**checkMesh 결과 — 두 region 모두 완벽:**
| 지표 | 값 | 판정 |
|---|---|---|
| Max cell openness | 1.08e-16 | OK |
| Max aspect ratio | 1.6 | OK |
| **Max non-orthogonality** | **0** | OK (정렬 hex) |
| Max skewness | 3.3e-14 | OK |
| Cell volumes | 균일 2.5e-5 | OK |
| **Coupled point location match** | avg 0 | **OK (CHT 인터페이스 정합)** |
| 종합 | **Mesh OK.** | ✅ |

- fluid 패치: floor/ceiling(wall), inlet/outlet(patch), fixedWalls(empty=2D),
  `fluid_to_baffle3D_*` = **mappedWall / mappedExtrudedWall** (열결합)
- baffle3D 패치: `baffle3D_to_fluid_*` = mappedWall (반대편 열결합), baffle3D_side, floor
- 양쪽 `neighbourRegion`/`neighbourPatch`가 정확히 매칭 → **conjugate 결합 정상 구성**

## 4. 해석 실행 및 검증
`foamMultiRun`을 Time=200s(pilot)까지 실행 — **안정적으로 완주, 발산 없음.**

마지막 timestep 잔차 (log.foamMultiRun):
```
fluid    Ux       Initial residual = 4.7e-4
fluid    Uy       Initial residual = 6.3e-4
fluid    h        Initial residual = 1.2e-3   (enthalpy/온도)
baffle3D e        Initial residual = 1.4e-4   (solid energy) ← CHT 결합 작동
fluid    p_rgh    Initial residual = 4.4e-3   (buoyancy 압력)
fluid    k/eps    Initial residual ~ 9e-4 / 4.5e-4
```

**핵심 검증:**
- ✅ `baffle3D: Solving for e` — solid region 에너지 방정식이 fluid와 동시에 풀림
  → **conjugate heat transfer가 실제로 작동**
- ✅ `p_rgh` + `h` 동시 해 → **자연대류(부력+열) 정상**
- ✅ 발산 없음, 모든 잔차 하강 추세 (정상상태로 수렴 중)

## 5. 현재 한계 / 다음 단계
- **결과 필드 미저장:** pilot에서 writeInterval(500) > endTime(200)이라 시간
  디렉토리가 디스크에 안 남음 → 온도장/속도장 시각화 미생성.
  (설정은 writeInterval=50, endTime=1000으로 수정 완료 — 재실행하면 해결)
- **정상상태 미도달:** 200s에서 잔차 1e-3 수준. 자연대류는 수렴에 더 긴 시간 필요.
- **다음 단계(재실행 1줄):**
  ```bash
  cd <case_dir>
  rm -f log.foamMultiRun && foamMultiRun > log.foamMultiRun 2>&1
  ```
  → 50s마다 결과 저장, residualControl 1e-4 도달 시 자동 정지
  → 이후 build_report로 온도장·속도장·수렴 그래프 생성

## 6. 도구/환경 메모
- 이 케이스의 multiRegion 메시 체인(topoSet/extrudeToRegionMesh/createBaffles)과
  solver(foamMultiRun)는 에이전트 ALLOWED_BINS 보안 allowlist 밖이라,
  해당 단계는 사용자가 WSL 터미널에서 직접 실행함(OF 환경 source 후).
- 에이전트는 케이스 정의·dict 설정·메시 검증·로그 진단·보고서를 담당.
- 보안 경계(PROTECTED config/foam, ALLOWED_BINS 불변)는 의도적으로 유지함
  — CHT 튜토리얼 하나를 위해 신뢰 루트를 완화하지 않음.

---

## 결론
**Natural convection CHT 셋업·메시·솔버가 모두 검증됨.**
완벽한 메시(non-ortho 0, coupled point match OK)에서 foamMultiRun이 안정적으로
실행되어 fluid 자연대류와 solid 열전도가 결합(CHT)되어 풀림을 확인.
정량 온도장 시각화는 writeInterval 수정 후 1회 재실행으로 완성 가능.
