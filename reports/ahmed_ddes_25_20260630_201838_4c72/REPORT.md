# CFD run report — `ahmed_ddes_25_20260630_201838_4c72`

## 설정 (settings)

| 항목 | 값 |
|---|---|
| case_kind | ahmed_ddes_25 |
| solver | incompressibleFluid |
| turbulence | SpalartAllmarasDDES |
| viscosity nu | [0 2 -1 0 0 0 0] 1.5e-05 |
| endTime | 0.30005 |
| geometry | gmsh:external_stl |

## 격자 품질 (checkMesh)

| 지표 | 값 |
|---|---|
| cells | 1510909 |
| max non-orthogonality | 64.8466 |
| max skewness | 2.9759918 |
| Mesh OK | True |

## 실행 결과 (run)

| 항목 | 값 |
|---|---|
| reached time | 0.30003922 |
| max Courant | 2.8648008 |
| diverged | False |
| converged | 1 |
| wall time [s] | 5.4 |

## 수렴 (residuals)

![residuals](residuals.png)

## 속도장 (velocity)

![velocity](velocity.png)
