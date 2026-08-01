"""
example_pipeline.py — end-to-end demo of the self-correcting loop with NO API key
(heuristic backend). Swap `backend` for AnthropicBackend(...) or CLIBackend([...]) and
the rest is unchanged.

The loop: run job (isolated, timeout) -> deterministic validators judge -> on failure,
backend proposes a patched source -> re-run -> validators judge again. Bounded + logged.
"""
import os, sys, re, shutil, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))                      # import caeloop
os.environ["CFD_MESHER_PATH"] = os.path.abspath(os.path.join(HERE, "..", "..", "cfd_mesher"))
import caeloop as cl
from caeloop import validators as V

# fresh run dir; copy the (buggy) job template in so the template stays pristine
work = os.path.join(HERE, "_run")
shutil.rmtree(work, ignore_errors=True); os.makedirs(work)
shutil.copy(os.path.join(HERE, "job_mesh.py"), os.path.join(work, "job.py"))

# ---- a heuristic repair rule: rescale SIZE_* toward the target element band -----
LO, HI = 3000, 200000
def element_count_failed(res, rep):
    return any(c.name == "element_count" and not c.ok for c in rep.checks)
def rescale_sizes(src, res, rep):
    n = res.metrics.get("n_elements")
    if not n:
        return src
    target = (LO * HI) ** 0.5
    scale = max(0.3, min(3.0, (n / target) ** (1 / 3)))        # isotropic edge-length scale
    repl = lambda m: f"{m.group(1)}{float(m.group(2)) * scale:.4f}"
    src = re.sub(r"(SIZE_MIN\s*=\s*)([0-9.]+)", repl, src)
    src = re.sub(r"(SIZE_MAX\s*=\s*)([0-9.]+)", repl, src)
    return src
rule = cl.RepairRule("rescale_mesh_size", element_count_failed, rescale_sizes)

backend = cl.NullBackend([rule])
# backend = cl.AnthropicBackend(model="claude-opus-4-8")      # <- drop-in, needs API key
# backend = cl.CLIBackend(["mymodel", "--prompt"])            # <- any local model CLI

runner = cl.Runner(backend, log_path=os.path.join(work, "run_log.jsonl"))
step = cl.Step(
    name="sphere_mesh",
    script=os.path.join(work, "job.py"),
    metrics_file="mesh_quality.json",
    timeout_s=120, max_repairs=3,
    validators=[
        V.process_succeeded(),
        V.no_stderr_signature(["overlapping facets", "non-manifold", "self-intersect"]),
        V.metric_present("n_elements"),
        V.element_count_between(LO, HI),
        V.quality_pass(),
    ])
ctx = {"task": "mesh a sphere in a far-field box; want 3k-200k cells and quality pass"}

print("=== self-correcting mesh pipeline (heuristic backend, no API key) ===")
ok, outcomes = runner.run_pipeline([step], ctx)
oc = outcomes[0]
print(f"\nresult: {'PASS' if ok else 'FAIL'} in {oc.attempts} run(s)")
print("size evolution + counts per attempt:")
for h in oc.history:
    print(f"  attempt {h['attempt']}: n_elements={h['metrics'].get('n_elements')}, "
          f"action={h['action']}")
print(f"\nfull audit log: {os.path.relpath(os.path.join(work,'run_log.jsonl'))}")
