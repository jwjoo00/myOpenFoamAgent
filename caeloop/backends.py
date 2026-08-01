"""
caeloop.backends — pluggable repair backends. All share one interface so the FSM is
model-agnostic: a local model CLI, Claude/Gemini CLI, or the Anthropic API just
swap in. A backend ONLY proposes corrected source; it never judges success.

    propose_fix(source, result, report, ctx) -> new_source (str)
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
import os, re, subprocess, textwrap


def build_repair_prompt(source, result, report, ctx) -> str:
    """The narrow-judgment prompt: failing source + ground-truth failure signals."""
    failed = "\n".join(f"  - {c.name}: {c.detail}" for c in report.failed)
    return textwrap.dedent(f"""\
        You are repairing a CAE geometry/meshing job that FAILED deterministic validation.
        Task context: {ctx.get('task','(none)')}

        FAILING VALIDATORS:
        {failed}

        PROCESS: returncode={result.returncode} timed_out={result.timed_out} \
        duration={result.duration_s:.1f}s
        METRICS (job's JSON contract): {result.metrics}
        STDERR (tail):
        {result.stderr[-1500:]}

        CURRENT SOURCE:
        ```python
        {source}
        ```

        Return ONLY the COMPLETE corrected Python source (no prose, no markdown fences).
        Make the minimal change that fixes the validated failure. Do not weaken the
        validation thresholds or fake the metrics; fix the actual geometry/mesh cause.
        """)


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


# ---------------------------------------------------------- heuristic (no API)
@dataclass
class RepairRule:
    name: str
    matches: Callable          # (result, report) -> bool
    transform: Callable        # (source, result, report) -> source


class NullBackend:
    """No LLM. Applies the first matching RepairRule. Encodes the fact that many
    geometry/mesh failures map to *known* fixes (so they never need an API call).
    Also the safe default when external LLM access is restricted."""
    name = "heuristic"

    def __init__(self, rules):
        self.rules = rules

    def propose_fix(self, source, result, report, ctx):
        for r in self.rules:
            try:
                if r.matches(result, report):
                    new = r.transform(source, result, report)
                    if new != source:
                        return new
            except Exception:
                continue
        return source              # no rule fired -> no change -> loop stops


# ----------------------------------------------------------- Anthropic API
class AnthropicBackend:
    """Real Anthropic Messages API shape. Requires ANTHROPIC_API_KEY + `anthropic` SDK.
    Returns the complete corrected file. (Endpoint also swappable to Bedrock/Vertex/
    Foundry via the SDK's env vars without touching this code.)"""
    def __init__(self, model="claude-opus-4-8", max_tokens=8000):
        self.name = f"anthropic:{model}"
        self.model, self.max_tokens = model, max_tokens

    def propose_fix(self, source, result, report, ctx):
        import anthropic                                   # lazy: only if used
        client = anthropic.Anthropic()                     # reads ANTHROPIC_API_KEY
        msg = client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            messages=[{"role": "user",
                       "content": build_repair_prompt(source, result, report, ctx)}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _strip_fences(text)


# ------------------------------------------------------- generic CLI backend
class CLIBackend:
    """Shell out to any 'prompt-in, text-out' CLI (Gauss / gemini / claude -p ...).
    cmd is the argv prefix; the prompt is piped on stdin. Matches a swappable-LLM setup
    where the model runs locally under contract constraints."""
    def __init__(self, cmd, name=None, timeout=120):
        self.cmd, self.timeout = cmd, timeout
        self.name = name or f"cli:{cmd[0]}"

    def propose_fix(self, source, result, report, ctx):
        prompt = build_repair_prompt(source, result, report, ctx)
        p = subprocess.run(self.cmd, input=prompt, capture_output=True,
                           text=True, timeout=self.timeout)
        if p.returncode != 0 or not p.stdout.strip():
            return source
        return _strip_fences(p.stdout)
