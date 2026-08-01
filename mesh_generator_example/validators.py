"""
caeloop.validators — reusable DETERMINISTIC validators. Each is a factory returning a
Validator: (StepResult, ctx) -> [Check]. They read the job's structured metrics
contract (e.g. the mesher's quality.json), the process result, and stderr signatures.
These are the ground truth that decides pass/fail. The LLM never overrides them.
"""
from .framework import Check


def process_succeeded():
    def v(res, ctx):
        if res.timed_out:
            return [Check("process", False, f"timed out after {res.duration_s:.0f}s")]
        return [Check("process", res.returncode == 0,
                      "exit 0" if res.returncode == 0 else f"exit {res.returncode}")]
    return v


def no_stderr_signature(patterns):
    """Fail if any known fatal geometry/mesh signature appears (e.g. 'overlapping
    facets', 'non-manifold', 'self-intersect')."""
    pats = [p.lower() for p in patterns]
    def v(res, ctx):
        s = res.stderr.lower()
        hit = next((p for p in pats if p in s), None)
        return [Check("stderr_signature", hit is None,
                      "clean" if hit is None else f"matched '{hit}'")]
    return v


def metric_present(key):
    def v(res, ctx):
        return [Check(f"has[{key}]", key in res.metrics,
                      "present" if key in res.metrics else "missing (no contract written)")]
    return v


def quality_pass(key="pass"):
    """The mesher already emits pass = (frac below threshold acceptable). Trust it."""
    def v(res, ctx):
        val = res.metrics.get(key)
        return [Check("quality_pass", val is True, f"{key}={val}")]
    return v


def min_quality(threshold, key="min"):
    def v(res, ctx):
        val = res.metrics.get(key)
        ok = isinstance(val, (int, float)) and val >= threshold
        return [Check("min_quality", ok, f"{key}={val} (need >= {threshold})")]
    return v


def element_count_between(lo, hi, key="n_elements"):
    """Catches both a collapsed/over-coarse mesh (too few) and an explosion (too many)."""
    def v(res, ctx):
        n = res.metrics.get(key)
        ok = isinstance(n, (int, float)) and lo <= n <= hi
        return [Check("element_count", ok, f"{key}={n} (want {lo}..{hi})")]
    return v


def expect_count(key, value, tol=0):
    """Topology check, e.g. exactly 1 volume (watertight) or an expected #faces/patches.
    The job must emit these counts in its contract."""
    def v(res, ctx):
        got = res.metrics.get(key)
        ok = isinstance(got, (int, float)) and abs(got - value) <= tol
        return [Check(f"{key}=={value}", ok, f"{key}={got}")]
    return v
