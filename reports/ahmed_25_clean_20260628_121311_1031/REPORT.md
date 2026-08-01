# CFD run report — `ahmed_25_clean_20260628_121311_1031`

## 설정 (settings)

| 항목 | 값 |
|---|---|
| case_kind | ahmed_25_clean |
| solver | incompressibleFluid |
| turbulence | kOmegaSST |
| viscosity nu | [0 2 -1 0 0 0 0] 1.5e-05 |
| endTime | 500 |
| geometry | None |

## 격자 품질 (checkMesh)

| 지표 | 값 |
|---|---|
| cells | 291881 |
| max non-orthogonality | 63.1462 |
| max skewness | 2.82637 |
| Mesh OK | True |

## 실행 결과 (run)

| 항목 | 값 |
|---|---|
| reached time | 500.0 |
| max Courant | None |
| diverged | False |
| converged | 1 |
| wall time [s] | 57.9 |

## 수렴 (residuals)

![residuals](residuals.png)

## 속도장 (velocity)

![velocity](velocity.png)
