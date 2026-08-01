# CFD run report — `ahmed_body_20260628_120217_0483`

## 설정 (settings)

| 항목 | 값 |
|---|---|
| case_kind | ahmed_body |
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
| wall time [s] | 57.2 |

## 수렴 (residuals)

![residuals](residuals.png)

## 속도장

_(생략: UnicodeDecodeError: 'utf-8' codec can't decode byte 0x9d in position 666: invalid start byte)_
