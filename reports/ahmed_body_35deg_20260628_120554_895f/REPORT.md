# CFD run report — `ahmed_body_35deg_20260628_120554_895f`

## 설정 (settings)

| 항목 | 값 |
|---|---|
| case_kind | ahmed_body_35deg |
| solver | incompressibleFluid |
| turbulence | kOmegaSST |
| viscosity nu | [0 2 -1 0 0 0 0] 1.5e-05 |
| endTime | 500 |
| geometry | None |

## 격자 품질 (checkMesh)

| 지표 | 값 |
|---|---|
| cells | 303981 |
| max non-orthogonality | 62.7619 |
| max skewness | 2.71517 |
| Mesh OK | True |

## 실행 결과 (run)

| 항목 | 값 |
|---|---|
| reached time | 500.0 |
| max Courant | None |
| diverged | False |
| converged | 1 |
| wall time [s] | 55.5 |

## 수렴 (residuals)

![residuals](residuals.png)

## 속도장 (velocity)

![velocity](velocity.png)
