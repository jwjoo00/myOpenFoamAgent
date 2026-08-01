"""
job_mesh.py — a CAE job the FSM runs. Meshes a sphere in a box and writes the quality
JSON contract. The SIZE_* literals at the top are the tunable parameters that a repair
backend (heuristic / LLM) rewrites. This template ships with a PLANTED BUG: the cells
are far too coarse, so the mesh collapses to too few elements and fails validation.
"""
import os, sys
sys.path.insert(0, os.environ["CFD_MESHER_PATH"])   # set by the runner's environment
import cfd_mesher as cm

# ---- tunable parameters (rewritten by the repair backend) -------------------
SIZE_MIN = 1.8      # BUG: way too coarse -> ~600 cells -> fails element_count
SIZE_MAX = 1.8
# -----------------------------------------------------------------------------

cfg = cm.MeshConfig(dim=3, size_min=SIZE_MIN, size_max=SIZE_MAX, algorithm_3d=10,
                    quality_metric="minSICN", quality_threshold=0.1)
r = cm.run_mesh(cfg, builder=cm.sphere_in_box_3d(box=6.0, r=0.5),
                outdir=".", basename="mesh", formats=("openfoam",), verbose=False)
print("n_elements =", r.quality["n_elements"], " min_SICN =", round(r.quality["min"], 3))
