# External-STL mesh quality study (motorBike, gmsh path)

**질문**: gmsh로 외부 형상(motorBike) 메시 품질을 어디까지 올릴 수 있나? 특히
checkMesh `max non-orthogonality`(RANS 정확도 핵심)를 얼마나 낮출 수 있나?

**한 줄 결론**: 표면을 **보존**하는 모든 lever(아래 1~9: knob·optimizer·dual·repair·mesher
종류)로는 max non-ortho ~89°가 한계. 하지만 **STL을 적극 수정(벽면 smoothing + 재-close)**
하면 89°→**~81-83°**까지 내려간다 — 원본 대비 변형은 max ~1.3% / mean ~0.13%로 미미하며
`stl_deviation`으로 측정·보고된다. 그래도 thin feature(거울·번호판·스포크)가 만드는
boundary tet 때문에 RANS-ideal(<70°)은 못 넘으므로, 그 이상은 hex/prism(snappyHexMesh /
cfMesh) 또는 `nNonOrthogonalCorrectors` solver 보정이 필요.

> 단, **max 89° / average ~21° / bad face(>70°) ≈ 0.4%** 라서 메시가 "못 쓰는" 건 아님.
> `fvSolution`의 `nNonOrthogonalCorrectors 2~3`으로 보정하면 RANS solve 가능.
> 진짜 정확도 한계는 non-ortho가 아니라 **boundary layer 부재**(벽면 drag/Cf).

## 실측한 lever 전부 (harness: `qexp.py`, `_tetgen_test.py`)

| # | lever | 도구 | max non-ortho | 결과 |
|--:|-------|------|:---:|------|
| 1 | decimation faces (12k~60k) | gmsh | 88.8~89.6 | 단조 미세변화, 무의미 |
| 2 | 3D algorithm (HXT/Delaunay/Frontal) | gmsh | ~89.4 | **Frontal=skew 최저(3.12)**, non-ortho 불변 |
| 3 | optimize passes (2 vs 8) | gmsh Netgen | 89.41 | **완전 무효** (p2==p8) |
| 4 | near-wall grading (Distance+Threshold) | gmsh | 89.9 | **유해** (skew 3.8→8.9) |
| 5 | surface smoothing **재-close 없이** | trimesh | — | 메시 깨짐(0 elements, self-intersect) |
| 6 | tet→polyhedral dual | OF polyDualMesh | — | **cellZone 깨짐**, checkMesh abort |
| 7 | uniform surface remesh | pyacvd | 89.99 | 표면 균일화(형상 보존)는 **무효** (skew↑) |
| 8 | 대체 watertight repair | manifold3d | — | repair 실패(빈 메시) |
| 9 | quality-bounded tets (-q 1.414) | tetgen/meshpy | **89.86** | quality 보장 mesher도 ~90° |
| **10** | **벽면 smoothing (humphrey + 재-close)** | **trimesh** | **83~81** | ✅ **WINNER** — STL을 펴서 floor 돌파, 변형 ~1.3% |

> #5와 #10의 차이 = **재-close**. smoothing이 만든 self-intersection을 pymeshfix로 다시
> 닫으면(#10) gmsh가 멈추지 않고, **coarse decimation + 많이 smooth**할수록 non-ortho가
> 더 내려간다(fine 표면은 sharp edge를 보존해 역효과). humphrey가 taubin보다 feature 보존 우수.
> 변형량은 `repair_to_watertight`가 원본 대비 Hausdorff(%)로 측정 → 결과 `stl_deviation`.

## framework 반영

검증된 winner는 코드로, 실패/무효 lever는 문서·메시지로 — 패턴 유지:

- **`meshgen._next_external_config`**: 품질 self-improve 정책 = `skew↑→Frontal` →
  **`non-ortho↑→벽면 smoothing`(humphrey 0→15→50, max_deviation_pct 예산 안)** → `sicn↓→faces↑`.
  passes·grade는 데이터 근거로 제외. `_better`는 non-ortho를 1순위로 best 선택.
- **`mesher.repair_to_watertight`**: `smooth` 파라미터(+재-close) + 원본 대비 `deviation` 측정.
- **`meshgen.generate_external`**: 라운드별 best 추적 + best 메시 케이스 복원 + `stl_deviation` 보고.
  한계 도달 시 `snappy_suggested` + `snappy_reason`(변형량+개선 명시) + `solver_hint`.
- **`mesher.mesh_external_stl_3d`**: 검증된 knob(algo·opt_passes·grade·sizemin) 파라미터화.
- **`agent.py` 원칙 7**: 한계 시 임의 진행 말고 사용자에게 "보정 걸어 gmsh solve vs snappy" 선택 제시.
- **`qexp.py`**: 연구용 harness — `--acvd/--manifold/--smooth/--grade/--algo/--passes`.
  매끈한 body(sphere·Ahmed 등)엔 pyacvd/tetgen가 도움될 수 있어 보존(bike엔 무효).

## 3D boundary layer (벽면 prism) — `add_boundary_layers`

외부유동 RANS의 벽면 정확도(drag·Cf)엔 prism BL이 필수. 실측 결론:

- **gmsh는 3D BL 불가** — `BoundaryLayer` field가 `FacesList`를 모름(2D 곡선 전용). 확정.
- **해법 = 하이브리드**: gmsh가 잡은 tet 메시(constant/polyMesh) 위에 **snappyHexMesh를
  `addLayers`-only**(castellatedMesh/snap false)로 돌려 벽면 prism 층만 삽입.
- **tet base 주의**: 기본 파라미터는 0 layer로 끝남. **absolute firstLayerThickness +
  minThickness↓ + meshQuality 완화(minTetQuality −1e30, maxNonOrtho 89) + nRelaxedIter↑**
  로 풀어야 삽입됨. nGrow>0 / maxThicknessToMedialRatio↑는 coverage를 오히려 붕괴시킴(실측).
- **coverage는 벽면 메시 해상도가 좌우**(가장 큰 lever): 20k면→67%, **30k면+smooth20→79%**
  (40k/50k는 82%지만 base skew>4로 checkMesh 무효). 그래서 `add_boundary_layers`가 layer 전에
  벽을 wall_faces(기본 30k)로 재메싱한다.
- motorBike 실측(30k 재메싱): 101k→**126,523 cells, coverage 79%, 평균 2.5 layers**,
  non-ortho 88.8(보정), skew 3.68, Mesh OK.
- 남은 ~21% 분석(ray-cast 두께 측정, bike 1.7m, BL 한쪽 9.9mm): 순수 "너무 얇음"(<2×BL≈20mm,
  최박 ~5mm 거울·번호판·스포크)은 **~4.6%뿐**. 나머지 ~16%는 thinness가 아니라 **concave 코너·
  부품 간 좁은 틈·sharp edge**에서 양면 prism이 충돌/quality 거부되는 것(더 얇은 BL을 줘도 coverage
  안 올랐던 실측이 뒷받침 — 병목은 두께가 아니라 형상의 빽빽함). snappy full도 동일한 한계.
- 반영: `meshgen.add_boundary_layers` + `add_boundary_layers` 도구(GATED). 흐름은
  `generate_mesh(external_stl)` → `add_boundary_layers` → (RAS scaffold) → solve.
- **다른 BL 라이브러리(netgen) + geometry 변환 — exhaustive 시도**:
  1. netgen `BoundaryLayer`는 clean 형상(Box−Sphere, box-in-box)에 정상 작동.
  2. **dirty STL→OCC solid 변환을 찾음**: OCP(cadquery-ocp)로 watertight STL을 sew→solid→
     ShapeFix→STEP. netgen OCC가 box−body를 **외부 메시 33,915 cells로 메싱 성공** —
     geometry 기반으로 dirty 바이크 외부 메싱을 달성(gmsh OCC createGeometry는 실패했던 것).
  3. **그러나 그 solid에 netgen BL은 전부 실패**: 해상도(92~812면)·ShapeFix healing·thin
     1-layer·limit_growth_vectors·grow_edges=False 모두 빈 NgException. box-in-box는 되는데
     바이크는 안 됨 → **바이크의 thin feature·concave가 prism 성장을 깨는 형상 문제**(알고리즘 아님,
     snappy 79% 한계와 동일한 벽).
  - 결론: geometry 기반 *외부 메싱*은 OCP+netgen으로 가능해졌다. 그러나 *BL*은 바이크 형상이
    보편적 벽이라(geometry·mesh기반 무관) **snappy addLayers 79% partial이 실용 최선**.

## RANS solve + drag (scaffold_external_ras + forceCoeffs)

전구간 파이프라인을 BUILT·RUNS: gmsh mesh → snappy BL → kOmegaSST scaffold(foamRun +
forceCoeffs) → potentialFoam init → solve → force 추출. 메커니즘은 모두 작동.

- 고-non-ortho(88) 메시 안정화로 **반드시** 필요했던 것: `limited 0.2` laplacian/snGrad
  (corrected/SIMPLEC는 즉시 FPE 발산), `div(phi,U) bounded Gauss upwind`, SIMPLE +
  pressure relaxation 0.3(consistent no), nNonOrthogonalCorrectors 3, potentialFoam init.
- **버그 수정**: box face→patch 매핑이 흐름축(X)과 어긋나 inlet이 -Y면에 있었음(차체가 흐름을
  안 받아 Cd≈0). inlet=-X, outlet=+X로 수정(mesher).
- **그러나 drag는 정량적으로 신뢰 불가**: Cd가 ±300+로 발진(last 200 stdev ~77), 난류
  residual(k/omega)이 1e-2에서 정체(p/U는 수렴). non-ortho 88 + minTetQuality 완화한 BL cell이
  force 적분을 망친다 — 우리가 문서화한 mesh-quality 한계가 **force noise로** 발현.
- **정량 drag엔 더 깨끗한 메시(snappy full hex, non-ortho<65, clean prism layers)가 필요**.
  scaffold/forceCoeffs 코드 자체는 정상이라 그런 메시엔 그대로 쓸 수 있다. 결론: gmsh 경로 =
  형상·메싱·정성 흐름엔 충분, **정량 aero force엔 부족**.

## 안 한 것 (설치/범위 밖)

- **cfMesh `cartesianMesh`** (hex+layers, robust, non-snappy) — OF12에 미설치(컴파일 필요).
  설치되면 snappy 대안으로 1순위.
- **MMG3D** (anisotropic quality remesh) — `pymmg` PyPI 부재로 미설치.
- 둘 다 tet 기반이 아닌 만큼 non-ortho를 낮출 잠재력 있음 → 추후 후보.
