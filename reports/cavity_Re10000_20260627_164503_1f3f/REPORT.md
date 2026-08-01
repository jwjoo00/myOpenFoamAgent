# CFD run report — `cavity_Re10000_20260627_164503_1f3f`

## 설정 (settings)

| 항목 | 값 |
|---|---|
| case_kind | cavity_Re10000 |
| solver | incompressibleFluid |
| turbulence | kEpsilon |
| viscosity nu | 1e-05 [m^2/s] |
| endTime | 10 |
| geometry | tutorial:incompressibleFluid/cavity |

## 격자 품질 (checkMesh)

| 지표 | 값 |
|---|---|
| cells | None |
| max non-orthogonality | None |
| max skewness | None |
| Mesh OK | None |

## 실행 결과 (run)

| 항목 | 값 |
|---|---|
| reached time | 10.0 |
| max Courant | 0.108512 |
| diverged | False |
| converged | 1 |
| wall time [s] | 4.5 |

## 수렴 (residuals)

![residuals](residuals.png)

## 속도장 (velocity)

![velocity](velocity.png)
