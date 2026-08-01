# External-STL mesh quality study (motorBike, gmsh path)

**Question**: how far can gmsh push the mesh quality of an external geometry (motorBike)? In particular,
how low can checkMesh `max non-orthogonality` (the key driver of RANS accuracy) be pushed?

**One-line conclusion**: with every lever that **preserves** the surface (1~9 below: knobs, optimizers,
dual, repair, mesher choice) max non-ortho ~89° is the floor. But **aggressively modifying the STL
(wall smoothing + re-close)** brings 89° down to **~81-83°** — deviation from the original is a negligible
max ~1.3% / mean ~0.13%, and it is measured and reported as `stl_deviation`. Even so, the boundary tets
created by thin features (mirrors, license plate, spokes) keep RANS-ideal (<70°) out of reach, so going
beyond that requires hex/prism (snappyHexMesh / cfMesh) or `nNonOrthogonalCorrectors` solver correction.

> That said, at **max 89° / average ~21° / bad faces (>70°) ≈ 0.4%** the mesh is not "unusable".
> Corrected with `nNonOrthogonalCorrectors 2~3` in `fvSolution`, a RANS solve is possible.
> The real accuracy limit is not non-ortho but the **absence of a boundary layer** (wall drag/Cf).

## Every lever actually measured (harness: `qexp.py`, `_tetgen_test.py`)

| # | lever | tool | max non-ortho | result |
|--:|-------|------|:---:|------|
| 1 | decimation faces (12k~60k) | gmsh | 88.8~89.6 | monotone but tiny change, meaningless |
| 2 | 3D algorithm (HXT/Delaunay/Frontal) | gmsh | ~89.4 | **Frontal = lowest skew (3.12)**, non-ortho unchanged |
| 3 | optimize passes (2 vs 8) | gmsh Netgen | 89.41 | **completely ineffective** (p2==p8) |
| 4 | near-wall grading (Distance+Threshold) | gmsh | 89.9 | **harmful** (skew 3.8→8.9) |
| 5 | surface smoothing **without re-close** | trimesh | — | mesh breaks (0 elements, self-intersect) |
| 6 | tet→polyhedral dual | OF polyDualMesh | — | **cellZone breaks**, checkMesh abort |
| 7 | uniform surface remesh | pyacvd | 89.99 | surface uniformization (shape-preserving) is **ineffective** (skew↑) |
| 8 | alternative watertight repair | manifold3d | — | repair failed (empty mesh) |
| 9 | quality-bounded tets (-q 1.414) | tetgen/meshpy | **89.86** | even a quality-guaranteeing mesher lands at ~90° |
| **10** | **wall smoothing (humphrey + re-close)** | **trimesh** | **83~81** | ✅ **WINNER** — flattening the STL breaks the floor, deviation ~1.3% |

> The difference between #5 and #10 = **re-close**. If the self-intersections created by smoothing are
> closed again with pymeshfix (#10), gmsh does not stall, and the **coarser the decimation + the more
> smoothing**, the lower non-ortho goes (a fine surface preserves sharp edges and backfires). humphrey
> preserves features better than taubin.
> Deviation is measured by `repair_to_watertight` as Hausdorff (%) against the original → reported as `stl_deviation`.

## Framework integration

Verified winners go into code, failed/ineffective levers into docs and messages — keep the pattern:

- **`meshgen._next_external_config`**: quality self-improve policy = `skew↑→Frontal` →
  **`non-ortho↑→wall smoothing` (humphrey 0→15→50, within the max_deviation_pct budget)** → `sicn↓→faces↑`.
  passes and grade are excluded on the evidence. `_better` picks the best with non-ortho as first priority.
- **`mesher.repair_to_watertight`**: `smooth` parameter (+re-close) + `deviation` measured against the original.
- **`meshgen.generate_external`**: per-round best tracking + restore of the best mesh case + `stl_deviation` reporting.
  On reaching the limit: `snappy_suggested` + `snappy_reason` (states deviation + improvement) + `solver_hint`.
- **`mesher.mesh_external_stl_3d`**: the verified knobs (algo, opt_passes, grade, sizemin) exposed as parameters.
- **`agent.py` principle 7**: at the limit, do not proceed arbitrarily — offer the user the choice "gmsh solve with correction vs snappy".
- **`qexp.py`**: research harness — `--acvd/--manifold/--smooth/--grade/--algo/--passes`.
  Kept because pyacvd/tetgen may help on smooth bodies (sphere, Ahmed, etc.) even though they are ineffective on the bike.

## 3D boundary layer (wall prisms) — `add_boundary_layers`

Prism BLs are mandatory for the wall accuracy (drag, Cf) of external-flow RANS. Measured conclusions:

- **gmsh cannot do 3D BL** — its `BoundaryLayer` field does not know `FacesList` (2D curves only). Confirmed.
- **Solution = hybrid**: on top of the tet mesh gmsh produced (constant/polyMesh), run **snappyHexMesh
  `addLayers`-only** (castellatedMesh/snap false) to insert only the wall prism layers.
- **Caution with a tet base**: default parameters end with 0 layers. They only get inserted with **absolute
  firstLayerThickness + minThickness↓ + relaxed meshQuality (minTetQuality −1e30, maxNonOrtho 89) + nRelaxedIter↑**.
  nGrow>0 / maxThicknessToMedialRatio↑ actually collapse coverage (measured).
- **Coverage is governed by wall mesh resolution** (the biggest lever): 20k faces→67%, **30k faces+smooth20→79%**
  (40k/50k reach 82% but base skew>4 invalidates checkMesh). That is why `add_boundary_layers` remeshes the
  walls to wall_faces (default 30k) before layering.
- motorBike measurement (30k remesh): 101k→**126,523 cells, coverage 79%, 2.5 layers on average**,
  non-ortho 88.8 (corrected), skew 3.68, Mesh OK.
- Analysis of the remaining ~21% (ray-cast thickness measurement, bike 1.7 m, BL 9.9 mm per side): pure "too thin"
  (<2×BL≈20 mm, thinnest ~5 mm — mirrors, license plate, spokes) is **only ~4.6%**. The other ~16% is not thinness but
  prisms from both sides colliding / being rejected on quality at **concave corners, narrow gaps between parts, and
  sharp edges** (backed by the measurement that even a thinner BL did not raise coverage — the bottleneck is not thickness
  but how densely packed the geometry is). snappy full hits the same limit.
- Integrated as: `meshgen.add_boundary_layers` + the `add_boundary_layers` tool (GATED). The flow is
  `generate_mesh(external_stl)` → `add_boundary_layers` → (RAS scaffold) → solve.
- **Other BL library (netgen) + geometry conversion — exhaustive attempt**:
  1. netgen `BoundaryLayer` works correctly on clean geometry (Box−Sphere, box-in-box).
  2. **Found a dirty STL→OCC solid conversion**: with OCP (cadquery-ocp), sew→solid→
     ShapeFix→STEP a watertight STL. netgen OCC **successfully meshed box−body into an external mesh of 33,915 cells** —
     geometry-based external meshing of the dirty bike achieved (what gmsh OCC createGeometry had failed at).
  3. **But netgen BL failed on that solid, every time**: resolution (92~812 faces), ShapeFix healing, thin
     1-layer, limit_growth_vectors, grow_edges=False all gave an empty NgException. box-in-box works but
     the bike does not → **a geometry problem where the bike's thin features and concavities break prism growth** (not the
     algorithm; the same wall as the snappy 79% limit).
  - Conclusion: geometry-based *external meshing* is now possible via OCP+netgen. But for *BL* the bike geometry
    is a universal wall (geometry-based or mesh-based alike), so **snappy addLayers at 79% partial is the practical best**.

## RANS solve + drag (scaffold_external_ras + forceCoeffs)

The full pipeline is BUILT and RUNS: gmsh mesh → snappy BL → kOmegaSST scaffold (foamRun +
forceCoeffs) → potentialFoam init → solve → force extraction. Every mechanism works.

- What was **strictly** required to stabilize the high-non-ortho (88) mesh: `limited 0.2` laplacian/snGrad
  (corrected/SIMPLEC diverge to FPE immediately), `div(phi,U) bounded Gauss upwind`, SIMPLE +
  pressure relaxation 0.3 (consistent no), nNonOrthogonalCorrectors 3, potentialFoam init.
- **Bug fix**: the box face→patch mapping was misaligned with the flow axis (X), so the inlet sat on the -Y face
  (the body received no flow, giving Cd≈0). Fixed to inlet=-X, outlet=+X (mesher).
- **But drag is not quantitatively trustworthy**: Cd oscillates by ±300+ (last-200 stdev ~77), and the turbulence
  residuals (k/omega) stall at 1e-2 (p/U do converge). non-ortho 88 + BL cells built with relaxed minTetQuality
  wreck the force integration — the mesh-quality limit we documented surfacing **as force noise**.
- **Quantitative drag needs a cleaner mesh (snappy full hex, non-ortho<65, clean prism layers)**.
  The scaffold/forceCoeffs code itself is sound, so it can be used as-is on such a mesh. Conclusion: the gmsh path =
  sufficient for geometry, meshing, and qualitative flow, **insufficient for quantitative aero forces**.

## Not attempted (not installed / out of scope)

- **cfMesh `cartesianMesh`** (hex+layers, robust, non-snappy) — not installed in OF12 (needs compiling).
  If installed, it is the first choice as a snappy alternative.
- **MMG3D** (anisotropic quality remesh) — not installed, `pymmg` absent from PyPI.
- Neither is tet-based, so both have potential to lower non-ortho → future candidates.
