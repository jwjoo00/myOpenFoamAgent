"""
config — central settings for the OpenFOAM agent (WSL / OpenFOAM 12 Foundation).

Everything that depends on THIS machine lives here so the rest of the code stays
portable. Values can be overridden by environment variables.
"""
from __future__ import annotations

import os
from pathlib import Path

# --- OpenFOAM (Foundation v12) on this WSL box ------------------------------
# Sourcing this gives blockMesh / foamRun / checkMesh / ... on PATH.
OPENFOAM_BASHRC = os.environ.get("OPENFOAM_BASHRC", "/opt/openfoam12/etc/bashrc")
# Bundled tutorial cases (also the LLM's ground-truth corpus for OF12 syntax).
FOAM_TUTORIALS = os.environ.get("FOAM_TUTORIALS_DIR", "/opt/openfoam12/tutorials")

# --- Where things live ------------------------------------------------------
PROJECT_DIR = Path(os.path.abspath(__file__)).parent
# Heavy case data (mesh, solver time dirs, logs, registry) on NATIVE ext4 — fast
# IO, and NOT churned through Dropbox sync.
RUNS_ROOT = Path(os.environ.get("OF_AGENT_RUNS", os.path.expanduser("~/of_agent_runs")))
# Only the lightweight REPORTS (REPORT.md + pngs) go in the project folder, so
# they are Dropbox-synced and easy to browse/back up.
REPORTS_ROOT = Path(os.environ.get("OF_AGENT_REPORTS", PROJECT_DIR / "reports"))

# --- LLM (Claude API) -------------------------------------------------------
MODEL = os.environ.get("OF_AGENT_MODEL", "claude-opus-4-8")
MAX_TOKENS = int(os.environ.get("OF_AGENT_MAX_TOKENS", "8000"))
MAX_TURNS = int(os.environ.get("OF_AGENT_MAX_TURNS", "200"))  # runaway-loop backstop

# --- Security: model output is untrusted ------------------------------------
# Only these executables may be run by the agent's tools. A binary not in this
# set is refused before any subprocess starts.
ALLOWED_BINS: set[str] = {
    # meshing
    "blockMesh", "snappyHexMesh", "surfaceFeatures", "extrudeMesh",
    "transformPoints", "surfaceConvert",
    "gmshToFoam", "createPatch", "collapseEdges",   # gmsh-based meshing (Phase 3)
    # solving (mpirun for parallel foamRun -parallel)
    "foamRun", "mpirun",
    # checks / utilities
    "checkMesh", "decomposePar", "reconstructPar", "postProcess",
    "foamDictionary", "foamListTimes",
    # multiRegion CHT (conjugate heat transfer): region split + foamMultiRun solver, and the
    # extrude/baffle utilities for cases that build solid regions from internal faces/patches.
    "foamMultiRun", "splitMeshRegions", "foamSetupCHT",
    "topoSet", "createBaffles", "extrudeToRegionMesh",
    # download helpers (used by fetch_geometry later)
    "wget", "git", "gunzip",
}

DEFAULT_TIMEOUT = 1800  # seconds, per single command

# --- MPI / parallel ---------------------------------------------------------
# Auto rank count from cell size, capped here. User wants 1..14; leave headroom
# (cores-2) but never exceed 14.
# This box: 16 PHYSICAL cores + hyperthreading (os.cpu_count()=32 logical). CFD/MPI is
# memory-bandwidth-bound — map ranks to PHYSICAL cores; oversubscribing onto hyperthreads
# SLOWS the solver. os.cpu_count() reports logical, so halve it for physical, then cap at 14
# (user's max; ≤16 physical leaves headroom, OpenMPI binds 14 ranks to distinct cores).
_PHYSICAL_CORES = int(os.environ.get("OF_AGENT_PHYS_CORES", max(1, (os.cpu_count() or 2) // 2)))
MAX_RANKS = min(14, _PHYSICAL_CORES)
CELLS_PER_RANK = 8000         # target mesh load per rank (aggressive — drive ranks toward MAX_RANKS)
MIN_PARALLEL_CELLS = 20000    # below this -> run serial (decompose overhead not worth it)

# --- Web tools (server-side: web_search + web_fetch) ------------------------
# Lets Claude search the internet and read pages itself. These are Anthropic
# SERVER tools: Claude calls them, Anthropic runs them, and the results come
# back INSIDE the response — the agent loop never dispatches or executes them
# (it only skips them and preserves the result blocks). Claude can then call our
# fetch_geometry(kind=url) to actually download any STL/OBJ/DAT URL it found.
#
# Requires web search to be ENABLED in your Claude Console (org admin setting),
# and bills $10 / 1000 searches (web_fetch adds only token cost). OFF by default
# — opt in with OF_AGENT_WEB=1 once web search is enabled on your account.
WEB_TOOLS = os.environ.get("OF_AGENT_WEB", "0") != "0"
WEB_SEARCH_MAX_USES = int(os.environ.get("OF_AGENT_WEB_SEARCH_MAX", "8"))
WEB_FETCH_MAX_USES = int(os.environ.get("OF_AGENT_WEB_FETCH_MAX", "8"))
# Tool versions: basic variants need no beta header and no code-execution tool —
# the most robust choice. (Newer *_20260209 add dynamic filtering but pull in the
# code-execution tool; not needed for finding/downloading geometry.)
WEB_SEARCH_TYPE = os.environ.get("OF_AGENT_WEB_SEARCH_TYPE", "web_search_20250305")
WEB_FETCH_TYPE = os.environ.get("OF_AGENT_WEB_FETCH_TYPE", "web_fetch_20250910")

# --- LLM context / caching (Phase 4) ----------------------------------------
# Prompt caching: cache_control on the system + conversation prefix, so the many
# tool-use round trips re-read the prefix at ~0.1x instead of full price.
# (Opus 4.8 min cacheable prefix is 4096 tokens; a rolling breakpoint on the
# latest user turn makes the growing conversation cache once it exceeds that.)
PROMPT_CACHE = os.environ.get("OF_AGENT_CACHE", "1") != "0"
# Long-session context strategy:
#   trim    — locally elide old tool-result contents past a size cap (default; no beta)
#   edit    — server-side context editing (clear_tool_uses_20250919, beta)
#   compact — server-side compaction near the context limit (compact_20260112, beta)
#   off     — send full history every time (relies on the 1M window)
CONTEXT_STRATEGY = os.environ.get("OF_AGENT_CONTEXT", "trim")
# 'trim' threshold (chars; ~4 chars/token, so 480k chars ≈ 120k tokens).
CONTEXT_TRIM_CHARS = int(os.environ.get("OF_AGENT_TRIM_CHARS", "480000"))

# --- Conversation persistence (resume after exit/crash) ---------------------
# Session state lives on ext4 (not a report) so it survives restarts.
SESSIONS_DIR = RUNS_ROOT / "sessions"

# OF_AGENT_DEBUG=1 prints per-call token usage (incl. cache_read/cache_write), so
# you can verify prompt caching is actually hitting.
DEBUG = os.environ.get("OF_AGENT_DEBUG", "0") != "0"
