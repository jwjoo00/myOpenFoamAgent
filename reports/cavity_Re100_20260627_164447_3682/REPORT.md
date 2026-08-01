# CFD run report — `cavity_Re100_20260627_164447_3682`

## 설정 (settings)

| 항목 | 값 |
|---|---|
| case_kind | cavity_Re100 |
| solver | incompressibleFluid |
| turbulence | kEpsilon |
| viscosity nu | 1e-03 [m^2/s] |
| endTime | 10 |
| geometry | tutorial:incompressibleFluid/cavity |

## 격자 품질 (checkMesh)

| 지표 | 값 |
|---|---|
| cells | 400 |
| max non-orthogonality | 0.0 |
| max skewness | 1.66533e-14 |
| Mesh OK | True |

## 실행 결과 (run)

| 항목 | 값 |
|---|---|
| reached time | 10.0 |
| max Courant | 0.334297 |
| diverged | False |
| converged | 1 |
| wall time [s] | 4.1 |

## 수렴 (residuals)

![residuals](residuals.png)

## 속도장 (velocity)

![velocity](velocity.png)
