# CFD run report — `cavity_Re400_20260627_164502_0ba1`

## 설정 (settings)

| 항목 | 값 |
|---|---|
| case_kind | cavity_Re400 |
| solver | incompressibleFluid |
| turbulence | kEpsilon |
| viscosity nu | 2.5e-04 [m^2/s] |
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
| max Courant | 0.290214 |
| diverged | False |
| converged | 1 |
| wall time [s] | 4.1 |

## 수렴 (residuals)

![residuals](residuals.png)

## 속도장 (velocity)

![velocity](velocity.png)
