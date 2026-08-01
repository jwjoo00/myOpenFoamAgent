"""
caeloop — deterministic FSM + validator-gated repair loop for CAE/mesh jobs.

Packaged from mesh_generator_example (the reference framework, copied verbatim).
Contract: deterministic Validators decide pass/fail — never the model; the repair
backend only PROPOSES corrected source on a validation failure; repair is bounded
and re-validated; everything is logged.

    framework  — StepResult, Check, ValidationReport, Step, Runner, execute/validate
    validators — reusable deterministic validators (element_count_between, min_quality, ...)
    backends   — NullBackend (heuristic), AnthropicBackend (claude-opus-4-8), CLIBackend
"""
from . import validators
from .backends import (
    AnthropicBackend,
    CLIBackend,
    NullBackend,
    RepairRule,
    build_repair_prompt,
)
from .framework import (
    Check,
    Runner,
    Step,
    StepOutcome,
    StepResult,
    ValidationReport,
    execute,
    validate,
)

__all__ = [
    "StepResult", "Check", "ValidationReport", "Step", "StepOutcome", "Runner",
    "execute", "validate",
    "build_repair_prompt", "RepairRule", "NullBackend", "AnthropicBackend",
    "CLIBackend", "validators",
]
