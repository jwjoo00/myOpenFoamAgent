"""
caeloop.framework — deterministic FSM backbone for self-correcting CAE/mesh jobs.

Design contract (do not violate):
  * The LLM (or heuristic) NEVER decides success. pass/fail is decided ONLY by
    deterministic Validators reading a structured metrics contract + process result.
  * Every step runs in an isolated subprocess with a timeout, so a gmsh segfault or
    runaway mesh becomes a structured StepResult, never a hung orchestrator.
  * Repair is bounded and produces a *new source*; the framework re-runs and the same
    validators judge again. No progress (identical source) ends the loop.
  * Everything is logged (JSONL) for a full audit trail.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Optional
import os, sys, json, time, shutil, subprocess


# ---------------------------------------------------------------- data records
@dataclass
class StepResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    metrics: dict = field(default_factory=dict)      # parsed from the job's JSON contract

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ValidationReport:
    checks: list                                     # list[Check]

    @property
    def ok(self) -> bool:
        return len(self.checks) > 0 and all(c.ok for c in self.checks)

    @property
    def failed(self):
        return [c for c in self.checks if not c.ok]

    def as_dict(self):
        return {"ok": self.ok, "checks": [c.__dict__ for c in self.checks]}


# Validator: pure function of (result, context) -> list[Check]. Deterministic.
Validator = Callable[[StepResult, dict], list]


@dataclass
class Step:
    name: str
    script: str                                      # path to runnable job (mutated by repairs)
    validators: list                                 # list[Validator]
    metrics_file: Optional[str] = "quality.json"     # job's JSON contract, relative to script dir
    timeout_s: float = 600.0
    max_repairs: int = 3


# ----------------------------------------------------------------- execution
def execute(script: str, timeout_s: float, metrics_file: Optional[str]) -> StepResult:
    """Run a job script in its own directory, isolated, with a hard timeout."""
    wd = os.path.dirname(os.path.abspath(script))
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, os.path.basename(script)], cwd=wd,
                           capture_output=True, text=True, timeout=timeout_s)
        rc, out, err, to = p.returncode, p.stdout, p.stderr, False
    except subprocess.TimeoutExpired as e:
        rc, out, err, to = -9, (e.stdout or ""), (e.stderr or "") + "\n[TIMEOUT]", True
    dur = time.time() - t0
    metrics = {}
    if metrics_file:
        mp = os.path.join(wd, metrics_file)
        if os.path.exists(mp):
            try:
                metrics = json.load(open(mp))
            except Exception:
                pass
    return StepResult(rc, out, err, to, dur, metrics)


def validate(result: StepResult, validators, ctx: dict) -> ValidationReport:
    checks = []
    for v in validators:
        checks.extend(v(result, ctx))
    return ValidationReport(checks)


# ------------------------------------------------------------------- runner
@dataclass
class StepOutcome:
    name: str
    ok: bool
    attempts: int
    report: ValidationReport
    history: list                                    # per-attempt dicts


class Runner:
    """Drives steps: execute -> validate -> (repair -> re-execute) bounded by max_repairs."""

    def __init__(self, backend, log_path: Optional[str] = None, verbose=True):
        self.backend = backend
        self.verbose = verbose
        self.log_path = log_path
        if log_path:
            open(log_path, "w").close()

    def _log(self, rec: dict):
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        if self.verbose:
            tag = "OK " if rec.get("ok") else "FAIL"
            extra = ""
            if rec.get("timed_out"):
                extra = " [TIMEOUT]"
            elif rec.get("returncode"):
                extra = f" [rc={rec['returncode']}]"
            fails = ",".join(c["name"] for c in rec["checks"] if not c["ok"])
            print(f"    [{rec['step']}] attempt {rec['attempt']}: {tag}"
                  f"{extra}  ({rec['duration_s']:.1f}s)"
                  + (f"  failed: {fails}" if fails else "")
                  + (f"  -> {rec['action']}" if rec.get("action") else ""))

    def run_step(self, step: Step, ctx: dict) -> StepOutcome:
        src = open(step.script).read()
        history = []
        for attempt in range(step.max_repairs + 1):
            res = execute(step.script, step.timeout_s, step.metrics_file)
            rep = validate(res, step.validators, ctx)
            action = None
            if not rep.ok and attempt < step.max_repairs:
                new_src = self.backend.propose_fix(src, res, rep, ctx)
                if not new_src or new_src == src:
                    action = f"{self.backend.name}: no change -> stop"
                    self._log_attempt(step, attempt, res, rep, action); history.append(
                        self._rec(step, attempt, res, rep, action)); break
                shutil.copy(step.script, f"{step.script}.v{attempt}")     # version history
                open(step.script, "w").write(new_src)
                action = f"{self.backend.name}: patched source -> v{attempt+1}"
                src = new_src
            self._log_attempt(step, attempt, res, rep, action)
            history.append(self._rec(step, attempt, res, rep, action))
            if rep.ok:
                return StepOutcome(step.name, True, attempt + 1, rep, history)
        return StepOutcome(step.name, False, len(history), rep, history)

    def run_pipeline(self, steps, ctx: dict):
        outcomes = []
        for step in steps:
            if self.verbose:
                print(f"  >> step '{step.name}'")
            oc = self.run_step(step, ctx)
            outcomes.append(oc)
            if not oc.ok:
                if self.verbose:
                    print(f"  xx pipeline halted at '{step.name}' after {oc.attempts} attempts")
                break
        ok = all(o.ok for o in outcomes) and len(outcomes) == len(steps)
        if self.verbose:
            print(f"  == pipeline {'PASSED' if ok else 'FAILED'} "
                  f"({sum(o.attempts for o in outcomes)} total runs)")
        return ok, outcomes

    def _rec(self, step, attempt, res, rep, action):
        return {"step": step.name, "attempt": attempt, "ok": rep.ok,
                "returncode": res.returncode, "timed_out": res.timed_out,
                "duration_s": round(res.duration_s, 2), "metrics": res.metrics,
                "checks": [c.__dict__ for c in rep.checks], "action": action,
                "stderr_tail": res.stderr[-400:]}

    def _log_attempt(self, step, attempt, res, rep, action):
        self._log(self._rec(step, attempt, res, rep, action))
