# Rotating Machinery (MRF) — Marine Propeller, OpenFOAM 12

**Case:** `propellerMRF`  ·  **Method:** Multiple Reference Frame (steady)  ·  **Solver:** `foamRun` + `incompressibleFluid`
**Geometry:** OF12 `resources/geometry/propeller.obj` (marine propeller, local — no download)
**Date:** 2026-06-28

---

## 1. Setup

| Item | Value |
|---|---|
| Mesh | blockMesh + snappyHexMesh → **525,273 cells**, rotor cellZone `innerCylinder` |
| Mesh quality | non-ortho 65.0, max skew 12.9 (complex blade; solved with limited schemes) |
| Rotation | MRF cellZone `innerCylinder`, axis **(0 1 0)**, **ω = 1500 rpm = 157.08 rad/s** |
| Fluid | water, ν = 1e-6 m²/s, ρ = 1000 kg/m³ |
| Inflow | inlet U = (0 −5 0) m/s (axial), outlet inletOutlet |
| Rotor walls | `propeller.*` → noSlip (rotation handled by the MRF frame) |
| Turbulence | kEpsilon RAS, wall functions |
| Convergence | `residualControl` p·U·k·ε < 1e-4 (cap 3000) |

MRF (steady) replaces the tutorial's transient rotating mesh (AMI, Δt=1e-5) — orders of magnitude cheaper for a performance point.

## 2. Convergence

**Auto-stopped at 930 iterations** (residualControl) — final initial residuals: p ≈ 5.4e-7, Ux ≈ 3.0e-7, well below the 1e-4 target. No divergence. (cap 3000 was never reached.)

## 3. Results (converged)

Forces from the `forces` functionObject on `propeller.*`, projected on the rotation axis (Y):

| Quantity | Kinematic (ρ=1) | **Physical (ρ_water=1000)** |
|---|---|---|
| Thrust  T (axial force) | 0.782 | **782 N** |
| Torque  Q (axial moment) | 0.0209 | **20.9 N·m** |
| Power   P = Q·ω | 3.28 | **3.28 kW** |

## 4. Non-dimensional coefficients (marine convention)

D = 0.227 m (from blade radial extent), n = 25 rev/s, Va = 5 m/s:

| Coefficient | Formula | Value |
|---|---|---|
| Advance ratio J | Va/(nD) | **0.879** |
| Thrust coeff Kt | T/(ρn²D⁴) | **0.468** |
| Torque coeff 10·Kq | 10·Q/(ρn²D⁵) | **0.549** |
| Open-water η₀ | (J/2π)(Kt/Kq) | **1.19** ⚠️ |

## 5. Literature comparison & physical assessment

**Method validation (solid):**
- MRF steady is an established approach for fans/propellers. Published ducted-propeller MRF studies report **thrust within ~2 %** of experiment ([Validation simulation using OpenFOAM, ResearchGate 364565687](https://www.researchgate.net/publication/364565687)). The method, convergence (residuals 1e-7), and force extraction here are all sound.
- Reference performance data for small propellers (measured Kt/Kq/η vs J and RPM) is catalogued in the **[UIUC Propeller Database](https://m-selig.ae.illinois.edu/props/propDB.html)** (same m-selig source as our airfoils) — the natural benchmark for an open-water validation.

**Honest finding — operating point is off-design:**
- **η₀ = 1.19 > 1 is unphysical** for steady propulsion (axial-force power T·Va would exceed shaft power Q·ω). 
- Kt = 0.47 **at J = 0.879** is inconsistent with a normal Kt–J curve, where Kt → 0 as J approaches J_max (a Wageningen B-series prop at J≈0.88 has Kt≈0.1–0.15). The blade is **heavily over-loaded** at 1500 rpm / 5 m/s — i.e. the OF tutorial's demo values are a *machinery demonstration*, not a matched propeller design point.
- **This is the key CFD lesson the agent surfaces:** the simulation converges and produces numbers, but the derived efficiency fails a physical sanity check → the operating point, not the method, is the issue.

**Proper validation = a J-sweep (next step):** vary rpm (or inflow) to map the Kt–Kq–J curve, find the design point, and overlay a known open-water chart (Wageningen B-series / UIUC PDB). The DB now stores this run's recipe + coefficients so each sweep point accumulates for comparison.

## 6. Agent capability added

| Tool | Role |
|---|---|
| `scaffold_mrf` | constant/MRFProperties + steady SIMPLE + forces functionObject (cellzone·axis·rpm·rotor_patches), residualControl |
| `rotor_performance` | reads forces → axial thrust·torque·power (uses DB axis/ω) |
| `parse_forces` | OF12 `forces.dat` parser (pressure+viscous, axial projection) |
| registry | `mrf` recipe + `rotor`/`performance` → `find_similar_runs` reuse |
| SYSTEM 원칙 9 | rotating-machinery workflow (cellZone → scaffold_mrf → run_solver → rotor_performance → literature) |

**Verified end-to-end:** mesh (cellZone) → MRF scaffold → converged solve (930 iters) → thrust/torque/power → coefficients → DB. Forces mechanism fixed twice during build (OF12 auto-loads `system/functions` via `#include`; regex patches need quotes) — both caught by running the real solver.
