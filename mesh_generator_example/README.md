# caeloop — a self-correcting FSM loop for CAE / meshing jobs

A small, model-agnostic framework that wraps a geometry/meshing job in a
**deterministic FSM** with a **bounded, validator-gated repair loop**. Built to slot
into an LLM-assisted CAE automation agent where the LLM is a *narrow judgment layer*,
not the orchestrator.

## The contract (the part that makes it reliable)

1. **Validators decide pass/fail — never the model.** Each `Validator` is a pure,
   deterministic function reading the job's structured metrics contract (e.g. the
   mesher's `quality.json`), the process result, and stderr signatures. The repair
   backend only *proposes corrected source*; it cannot declare success or weaken a
   threshold.
2. **Every step is isolated with a timeout.** Jobs run in a subprocess; a gmsh segfault
   or runaway mesh becomes a structured `StepResult` (`timed_out`, `returncode`), never
   a hung orchestrator.
3. **Repair is bounded and produces a new source.** The framework writes the patched
   source (keeping `*.vN` backups), re-runs, and the *same* validators judge again.
   Identical source = no progress = stop.
4. **Everything is logged** to JSONL for a full audit trail.

## Layout

```
caeloop/
  framework.py   # StepResult, Check, ValidationReport, Step, Runner, execute(), validate()
  backends.py    # NullBackend (heuristic, no API) | AnthropicBackend | CLIBackend
  validators.py  # reusable deterministic validators (quality_pass, element_count_between, ...)
example/
  job_mesh.py        # a job the FSM runs (meshes sphere-in-box, writes quality.json) — ships buggy
  example_pipeline.py# end-to-end demo, runnable with NO API key
```

## Run the demo (no API key needed)

```bash
python3 example/example_pipeline.py
```

Expected: attempt 0 fails `element_count` (mesh too coarse, ~600 cells) → the heuristic
backend rescales the `SIZE_*` literals → attempt 1 passes (~6.6k cells). The loop, the
version backups, and `example/_run/run_log.jsonl` are all produced.

## Swapping the backend (model-agnostic)

The backend is the *only* thing that changes between "heuristic", "Anthropic API", and
"a local model / Gemini / Claude CLI":

```python
backend = cl.NullBackend([rule])                       # heuristic rules, no LLM
backend = cl.AnthropicBackend(model="claude-opus-4-8") # Messages API (needs ANTHROPIC_API_KEY;
                                                       #   endpoint also -> Bedrock/Vertex/Foundry via SDK env)
backend = cl.CLIBackend(["gauss", "--prompt"])         # any prompt-in/text-out local CLI
```

All three implement one method:

```python
propose_fix(source, result, report, ctx) -> new_source
```

The repair prompt (see `backends.build_repair_prompt`) hands the model the failing
source plus the *ground-truth* failure signals (failed validators, metrics, stderr) and
asks for the complete corrected file — explicitly forbidding it from faking metrics or
weakening thresholds.

## Mapping to a CAE automation agent

- **Deterministic FSM backbone** = `Runner` + `Step` ordering. Each physics/stage step
  (geometry build, mesh, solve setup, post) is a `Step` with its own validators.
- **Narrow LLM judgment** = the backend, invoked *only on validation failure*. The happy
  path makes zero LLM calls.
- **Quality JSON contract** = whatever your job writes; the mesher's `quality.json`
  (n_elements, min/mean SICN, pass, histogram) is the worked example here.
- **Writing good validators is 80% of the reliability** — watertight/volume-conservation
  checks, face/patch counts, periodic node-match, quality contract. The API call is the
  easy 10 lines; the eval harness is the real work.
