#!/usr/bin/env python3
"""
agent.py — myOpenFoamAgent. A conversational OpenFOAM 12 (Foundation) CFD agent
where Claude DRIVES via tool-use: it proposes settings step by step, you confirm
gated actions, and every run's settings/results are persisted for later reuse.

Run inside WSL (needs ANTHROPIC_API_KEY):
    python3 agent.py                         # interactive
    python3 agent.py --task "cavity 셋업하고 짧게 돌려줘"
    python3 agent.py --task "..." --yes      # auto-approve gated tools (batch)

The tool-use loop (preserve tool_use blocks, pair every tool_use with a
tool_result, bounded turns) follows the proven window_agent pattern.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# make sibling modules importable regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import session
import tools

for _s in (sys.stdout, sys.stderr, sys.stdin):     # UTF-8 console (Korean safe)
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def _read_user(prompt: str) -> str:
    """Robust line input — reads raw bytes and tries UTF-8 then CP949/EUC-KR, so a
    non-UTF-8 console (e.g. Korean Windows codepage) never crashes the REPL."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    raw = sys.stdin.buffer.readline()
    if not raw:
        raise EOFError
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace").strip()

SYSTEM = """당신은 'myOpenFoamAgent' — OpenFOAM 12 (Foundation, openfoam.org) 전용 CFD 자동화 에이전트입니다. WSL에서 동작하고, 사용자는 CAD가 없습니다. Claude가 tool-use로 직접 케이스 정의→격자→솔버 설정→실행→결과를 운전합니다.

운영 원칙:
1) 단계별로 진행하고 핵심 갈림길에서 사용자에게 설명/확인을 구한다. 비가역·실행 동작(write_dict_entry, run_mesh, run_solver)은 도구 자체가 승인 게이트를 걸므로, 호출 전에 무엇을 왜 하는지 한 줄로 먼저 알린다. **사용자에게 결정/선택 질문을 던졌으면 명확한 답이 올 때까지 멈춘다 — 빈 입력·터미널 로그 붙여넣기(checkMesh 등)·질문과 무관한 내용을 '진행하라'는 동의로 해석하지 말고, 한 줄로 다시 확인한다. 사용자가 고르지 않은 길을 임의로 골라 진행하지 않는다. 애매하면 추측 대신 되묻는다.**
2) OF12 Foundation 문법만 사용한다: foamRun + `solver incompressibleFluid;`, constant/momentumTransport, constant/physicalProperties, surfaceFeatures. 인터넷의 ESI 문법(simpleFoam, turbulenceProperties, transportProperties, surfaceFeatureExtract)을 추측하지 말 것. 불확실하면 list_tutorials로 설치된 실제 케이스를 찾고 read_dict로 진짜 dict를 확인한 뒤 그것을 근거로 삼는다.
3) 표준 흐름: 목표 파악 → find_similar_runs로 과거 비슷한 run 참고 → list_tutorials로 시작 케이스 선택 → copy_case → read_dict로 현재 설정 확인 → 필요시 write_dict_entry로 조정 → 격자(분석형상·blockMeshDict 있으면 run_mesh; 곡선·외부 형상이면 generate_mesh = gmsh + validator-gated repair, snappy보다 robust. 인터넷/유명 외부 형상(motorBike, airfoil 등)은 반드시 new_case → fetch_geometry(kind=motorbike|airfoil|url) → generate_mesh(geometry=external_stl, stl=fetch_geometry가 돌려준 path) → (3D 외부유동이면) add_boundary_layers 순서로 한다) → check_mesh(Mesh OK 아니면 멈추고 상의) → run_solver → build_report(후처리: residual 수렴 + 속도장 + REPORT.md) → 결과 요약. 각 run의 설정·결과는 도구가 자동 저장한다.
4) run_solver 전에 endTime/deltaT가 합리적인지 확인하고, 처음에는 짧게(pilot) 돌려 안정성을 본 뒤 본런을 권한다. **run_solver는 background 기본** — 솔버를 던지고 job_id를 즉시 받아 채팅이 안 멈추고 exit해도 계속 돈다. 진행은 `job_status(job_id)`로 확인(running/done/failed·last_time·수렴), 필요하면 `stop_job`으로 중단(마지막 checkpoint 보존 → 다음에 run_solver로 latestTime 이어받기). 작은 케이스는 던진 뒤 몇 초 후 job_status로 결과를 보면 된다. 긴 실행이면 "백그라운드로 돌리는 중, job_status로 확인하세요"라고 알린다. 아주 작은/스크립트성만 background=False(blocking).
5) 도구가 돌려준 case_dir와 run_id를 기억해 이후 호출에 그대로 사용한다.
6) 필요하면 write_file로 새 파일(예: snappyHexMeshDict, 0/ 필드, 커스텀 dict·메모·스크립트)을 만들고, build_excel로 표+이미지가 든 엑셀 보고서(예: 여러 run 비교표 + 각 run의 velocity.png)를 만든다. 경로는 케이스 폴더나 reports 안으로 한다.
7) external_stl 메시는 generate_mesh가 품질을 self-improve한다: skew↑→Frontal algo, non-ortho↑→벽면 smoothing(STL을 펴서 89°→~83°; 변형량을 stl_deviation으로 측정·보고). **3D 외부유동을 정량적으로(drag·Cf) 풀려면 벽면 boundary layer가 필수**이고 gmsh는 3D BL을 못 만든다 — 그래서 generate_mesh(external_stl) 다음에 **add_boundary_layers**를 돌려 snappy addLayers-only로 벽면 prism 층을 넣는다(gmsh가 형상, snappy가 layer만 — 하이브리드; thin feature는 부분 coverage로 layer_coverage_pct·avg_layers에 보고). 남은 non-ortho는 average가 낮으니 fvSolution nNonOrthogonalCorrectors 2~3으로 보정해 solve한다. 더 높은 fidelity가 필요하면(snappy_suggested) 달성 품질·stl_deviation·layer coverage를 사용자에게 표로 알린 뒤 full snappyHexMesh(castellate+snap+layers, motorBike 튜토리얼 copy_case)로 갈지 물어 고르게 한다.

8) **iteration 수는 고정이 아니라 수렴-주도로 정한다(steady external RANS).** scaffold_external_ras는 residualControl(residual_tol 기본 1e-4)을 쓰므로 p·U·난류필드 잔차가 모두 그 밑이면 **자동으로 멈춘다**; end_time은 '수렴 못 하면 끊는' **상한(cap)**일 뿐이다. cap을 똑똑하게 정하라: ① 먼저 find_similar_runs로 같은 형상/Re/난류모델의 과거 run 중 **converged_iters가 기록된(=실제 수렴한) 것**을 찾아 cap≈1.5×그 값으로 잡는다. ② 선례가 없고 웹 도구가 켜져 있으면 web_search로 이런 케이스의 전형적 수렴 반복수를 찾아 정한다. ③ 둘 다 없으면 합리적 기본(예 2D·작은 케이스 500, 3D 외부 2000~5000)으로 잡되 짧은 pilot로 잔차 추세를 본다. run_solver 결과의 **residual_converged / converged_iters / hit_cap_unconverged / stopped_early_suspect**를 반드시 확인한다: residual_converged면 converged_iters를 보고하고 DB에 기록되어 다음에 재사용된다. **hit_cap_unconverged면 cap까지 가도 수렴 못 한 것** — 무작정 숫자만 키우지 말고 find_similar_runs+(web_search)로 원인을 판단해 cap↑·relaxation↓·메시 refine·발산여부 중 무엇을 할지 정한다. **stopped_early_suspect면 timeout·kill 의심** — read_log로 로그를 확인한 뒤 판단한다. 결정은 사용자에게 한 줄로 알린다. residual_tol=0은 옛 고정-반복 방식(특별한 이유 있을 때만).

9) **회전기계(rotating machinery)는 MRF로 푼다.** 팬·프로펠러·임펠러처럼 회전부가 있으면: ① 메시에 **회전영역 cellZone**이 필요하다 — snappy refinementSurfaces에서 회전부를 감싸는 닫힌 표면(cylinder 등)에 faceZone/cellZone을 지정해 만든다(회전부 STL + 회전영역 cylinder STL 둘 다 필요). OF12 `incompressibleFluid/propeller`·`mixerVessel2DMRF`가 ground-truth(MRF는 constant/MRFProperties, fvModels 아님). ② **scaffold_mrf**(cellzone·axis·origin·rpm·rotor_patches)로 MRFProperties를 깔고 steady SIMPLE로 만든다(회전메시 transient보다 싸다). **회전벽은 반드시 `MRFnoSlip`**(MRF 프레임과 함께 회전 — 그냥 `noSlip`이면 절대프레임 정지벽이라 **블레이드가 안 돌고 drag만 난다**; scaffold_mrf가 rotor_patches에 자동 적용). ③ run_solver → **rotor_performance**로 축방향 thrust·torque·power. **검증 순서:** (a) `rotor_report`로 형상·flow·회전 방향을 사람에게 보이고, (b) swirl(접선속도 U)이 실제로 omega·r 수준인지 확인(0이면 MRF가 안 도는 것), (c) **thrust가 reverse(음수)면 회전방향/handedness가 반대** → rpm 부호를 뒤집어 forward로 맞춘다(둘 다 도는 상태에서만 방향이 의미 있음). ④ 문헌비교: UIUC Propeller DB·DTMB 4119(Jessup) Kt-Kq-J 등과 비교(잘 맞추면 Kt 오차 ~수%). 결과는 DB에 mrf·rotor 레시피로 기록.

11) **너는 자신의 코드를 진화시킬 수 있다(self-evolution).** 새 능력이 필요하거나 코드에서 버그를 발견하면, 백업·검증·diff 원칙 그대로 — (1) 백업 (2) 검증 (3) diff — 안전 절차를 따른다. **새 도구는 가능하면 플러그인으로** 추가하라: `tools_plugins/tool_<x>.py`에 SPEC+handler(+GATED)를 쓰고(propose_self_edit(filename, create=True)) → 통과하면 apply → **핫리로드되어 다음 턴부터 코어 재시작 없이 사용 가능**. 기존 모듈(propgen·meshgen·tools 등) 수정은 propose_self_edit(old→new)로 격리 incubator에서 canary를 돌려 검증한 뒤 diff를 사용자에게 보이고 apply(=스냅샷 롤백점 생성)하되 **커널 변경은 재시작(bootstrap)이 필요**하다고 알린다. **`run_self_tests`(canary: 문법+도구정합성+물리검증 프로펠러 수학)가 oracle** — 자가수정 전후로 돌려 DTMB/B-series가 여전히 맞는지 확인한다. 자가수정이 잘못되면 `rollback_self(snapshot_id)`로 되돌린다(evolve_log로 스냅샷 확인). **플러그인 handler는 live 프로세스가 아니라 confined subprocess(bwrap: 네트워크 차단·최소 bind로 시크릿 숨김·RUNS만 쓰기·trust store read-only·메모리 격리)에서 실행**된다 — 단 **real sandbox(bwrap/firejail)가 있을 때만**이고, 없으면 plugin 실행·자율 모두 fail-closed로 거부된다(canary가 RED). **자율 진화**는 real sandbox + set_auto_evolve(True)일 때만, **기존 플러그인 한정·canary 통과분만·예산 내**에서 auto_evolve로 한다(신규 도구·커널은 항상 사람 승인). 단, 샌드박스 handler도 bind된 트리는 read 가능하니 plugin 출력(return)은 untrusted로 취급한다. `selfevolve.py`·`bootstrap.py`(엔진과 신뢰 루트)는 함부로 건드리지 않는다. 안전을 위해 에이전트는 `python bootstrap.py`로 실행하는 것이 좋다(커널 자가수정 후 자동 검증·롤백).

**도구는 반드시 정식 tool_use로 호출한다 — 도구 호출 문법(`<invoke>`/`<parameter>` 등 XML)을 텍스트 답변 본문에 절대 쓰지 않는다.** 도구를 부르려면 텍스트로 묘사하지 말고 실제 tool_use로 호출하라.

답변은 한국어+영어 혼용 가능, 기술 용어는 영어. 표·핵심 위주로 간결하게."""

MAX_TURNS = config.MAX_TURNS


def _serialize_content(content) -> list:
    """Assistant response blocks -> JSON-able dicts, preserving ALL block types
    (text, tool_use, and — critically for compaction — compaction blocks). Uses
    model_dump on real SDK blocks; falls back to manual extraction for test doubles."""
    out = []
    for b in content:
        if isinstance(b, dict):
            out.append(b)
        elif hasattr(b, "model_dump"):
            out.append(b.model_dump(mode="json", exclude_none=True))
        else:
            t = getattr(b, "type", "text")
            if t == "tool_use":
                out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            else:
                out.append({"type": t, "text": getattr(b, "text", "")})
    if not out:
        out.append({"type": "text", "text": "(빈 응답)"})
    return out


# A model occasionally writes a tool call as TEXT (<invoke name="x"><parameter ...>) instead of
# emitting a structured tool_use block. We salvage those so the loop doesn't stall on raw XML.
_INVOKE_RE = re.compile(r"<(?:antml:)?invoke\s+name=\"([^\"]+)\"\s*>(.*?)</(?:antml:)?invoke>", re.S)
_PARAM_RE = re.compile(r"<(?:antml:)?parameter\s+name=\"([^\"]+)\"\s*>(.*?)</(?:antml:)?parameter>", re.S)


def _coerce(v: str):
    s = v.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+", s):
        return float(s)
    return v


def _extract_text_tool_calls(text: str):
    """Return (display_text_without_xml, [(tool_name, args_dict), ...]) for tool calls the model
    wrote into the TEXT channel instead of as structured tool_use blocks."""
    calls = []
    for m in _INVOKE_RE.finditer(text):
        args = {pm.group(1): _coerce(pm.group(2)) for pm in _PARAM_RE.finditer(m.group(2))}
        calls.append((m.group(1), args))
    cleaned = _INVOKE_RE.sub("", text)
    cleaned = re.sub(r"(?m)^\s*call\s*$", "", cleaned)     # drop lone 'call' label lines
    return cleaned.strip(), calls


# --- context window management (caching + trimming) -------------------------
_WEB_GUIDE = """

10) 웹 도구(활성화됨): web_search로 인터넷을 검색하고 web_fetch로 특정 페이지·PDF 본문을 읽을 수 있다(둘 다 Anthropic 서버가 실행하니 너는 호출만 하면 된다). 형상 파일이나 레퍼런스 값이 필요할 때 쓴다: 예) "Ahmed body STL 찾아줘" → web_search로 .stl/.obj 다운로드 URL을 찾고 → 그 직접 URL을 fetch_geometry(kind=url, url=...)에 넘겨 실제로 내려받은 뒤 → generate_mesh(geometry=external_stl)로 잇는다. "세단 전형적 Cd?" 같은 검증값도 web_search로 확인해 시뮬레이션과 비교한다. **원칙 8의 cap 결정**: find_similar_runs에 선례가 없으면 web_search로 이 케이스 유형의 전형적 SIMPLE 수렴 반복수를 찾아 cap을 정한다. 검색·fetch로 얻은 정보는 출처 URL을 함께 밝힌다. 단순 상식·OF12 문법은 검색하지 말고(설치된 tutorial이 근거) 정말 외부 최신/특정 자료가 필요할 때만 쓴다."""


def _system_param():
    """system as a cached block list (prompt caching) or a plain string."""
    sys_text = SYSTEM + (_WEB_GUIDE if config.WEB_TOOLS else "")
    if config.PROMPT_CACHE:
        return [{"type": "text", "text": sys_text, "cache_control": {"type": "ephemeral"}}]
    return sys_text


def _msg_chars(messages: list) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)


def _trim_messages(messages: list) -> list:
    """Locally elide old tool_result CONTENTS (big dict reads / log tails) when the
    history grows large, preserving tool_use<->tool_result pairing and the message
    sequence. Returns a copy-with-elisions; the stored/persisted `messages` keep the
    full text. (Strategy 'trim'.)"""
    if _msg_chars(messages) <= config.CONTEXT_TRIM_CHARS:
        return messages
    keep_recent = 8
    view = list(messages)
    for i in range(max(0, len(view) - keep_recent)):
        m = view[i]
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        new_blocks, changed = [], False
        for b in m["content"]:
            if (isinstance(b, dict) and b.get("type") == "tool_result"
                    and len(str(b.get("content", ""))) > 100):
                nb = dict(b)
                nb["content"] = "[older tool result elided to save context]"
                new_blocks.append(nb)
                changed = True
            else:
                new_blocks.append(b)
        if changed:
            view[i] = {**m, "content": new_blocks}
        if _msg_chars(view) <= config.CONTEXT_TRIM_CHARS:
            break
    return view


def _cache_messages(messages: list) -> list:
    """Rolling prompt-cache breakpoint: mark cache_control on the last block of the
    most recent (up to 2) list-content user messages so the growing conversation
    prefix caches. Returns a shallow view with copied blocks (does not mutate the
    stored messages, keeping the persisted session clean)."""
    view = list(messages)
    marks = 0
    for i in range(len(view) - 1, -1, -1):
        m = view[i]
        if m.get("role") == "user" and isinstance(m.get("content"), list) and m["content"]:
            last = dict(m["content"][-1])
            last["cache_control"] = {"type": "ephemeral"}
            view[i] = {**m, "content": list(m["content"][:-1]) + [last]}
            marks += 1
            if marks >= 2:
                break
    return view


def _web_tools() -> list:
    """Anthropic SERVER-tool specs (web_search + web_fetch), appended to the tools
    list when config.WEB_TOOLS is on. Claude calls them and Anthropic executes them
    server-side; the loop never dispatches them — they arrive as server_tool_use /
    web_*_tool_result blocks that _serialize_content preserves verbatim. Claude can
    then call our fetch_geometry(kind=url) on any URL it found. No beta header and no
    code-execution tool needed for the basic tool versions."""
    if not config.WEB_TOOLS:
        return []
    return [
        {"type": config.WEB_SEARCH_TYPE, "name": "web_search",
         "max_uses": config.WEB_SEARCH_MAX_USES},
        {"type": config.WEB_FETCH_TYPE, "name": "web_fetch",
         "max_uses": config.WEB_FETCH_MAX_USES, "citations": {"enabled": True}},
    ]


def _create(client, messages: list):
    """One messages.create call with the configured caching + context strategy."""
    eff = _trim_messages(messages) if config.CONTEXT_STRATEGY == "trim" else messages
    if config.PROMPT_CACHE:
        eff = _cache_messages(eff)
    base = dict(model=config.MODEL, max_tokens=config.MAX_TOKENS,
                system=_system_param(), tools=list(tools.TOOL_SPECS) + _web_tools(),
                messages=eff)
    strat = config.CONTEXT_STRATEGY
    if strat == "compact":      # server-side summarization near the context limit
        return client.beta.messages.create(
            betas=["compact-2026-01-12"],
            context_management={"edits": [{"type": "compact_20260112"}]}, **base)
    if strat == "edit":         # server-side clearing of old tool results
        return client.beta.messages.create(
            betas=["context-management-2025-06-27"],
            context_management={"edits": [{"type": "clear_tool_uses_20250919"}]}, **base)
    return client.messages.create(**base)


def _compact(d, limit: int = 160) -> str:
    s = json.dumps(d, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def _summarize(name: str, r: dict) -> str:
    if not isinstance(r, dict):
        return str(r)[:140]
    if not r.get("ok", True):
        if r.get("declined"):
            return "거부됨"
        return f"실패: {r.get('error')}"
    if name == "list_tutorials":
        return f"{r.get('count')}개" + (" (잘림)" if r.get("truncated") else "")
    if name == "copy_case":
        return f"run_id={r.get('run_id')} solver={r.get('solver')} turb={r.get('turb_model')}"
    if name == "new_case":
        return f"run_id={r.get('run_id')} case_dir={r.get('case_dir')}"
    if name == "fetch_geometry":
        return f"{r.get('kind')} -> {r.get('path')} ({r.get('bytes')}B, {r.get('format')})"
    if name == "run_mesh":
        return f"{r.get('tool')} rc={r.get('returncode')} ({r.get('duration_s')}s)"
    if name == "check_mesh":
        m = r.get("metrics", {})
        return f"cells={m.get('cells')} nonOrtho={m.get('max_non_ortho')} meshOK={m.get('mesh_ok')}"
    if name == "generate_mesh":
        return (f"{'OK' if r.get('ok') else 'FAIL'} cells={r.get('mesh_cells') or r.get('n_elements')} "
                f"minSICN={r.get('min_sicn')} attempts={r.get('attempts')} meshOK={r.get('mesh_ok')}")
    if name == "run_solver":
        return (f"[{r.get('mode')}] t={r.get('last_time')} maxCo={r.get('max_courant')} "
                f"diverged={r.get('diverged')} ({r.get('duration_s')}s)")
    if name == "build_report":
        a = r.get("artifacts", {})
        return f"report -> {r.get('out_dir')} ({', '.join(k for k in a if k.endswith('_png'))})"
    if name == "write_file":
        return f"{'덮어씀' if r.get('overwritten') else '생성'} {r.get('path')} ({r.get('bytes')}B)"
    if name == "build_excel":
        return (f"xlsx -> {r.get('path')} (sheets={len(r.get('sheets', []))}, "
                f"tables={r.get('tables')}, imgs={r.get('images')})")
    if name in ("list_runs", "find_similar_runs"):
        return f"{r.get('count')} run(s)"
    if name == "write_dict_entry":
        return f"{r.get('key')}: {r.get('old')} -> {r.get('new')}"
    return "ok"


def _approve(name: str, args: dict, auto: bool) -> bool:
    # Consult the immutable REQUIRED_GATED baseline too, so a tampered tools.GATED can never
    # silently un-gate a destructive tool (defense for the self-evolution attack surface).
    try:
        import selfevolve
        _required = selfevolve.REQUIRED_GATED
    except Exception:
        _required = frozenset()
    if name not in tools.GATED and name not in _required:
        return True
    print(f"\n   ⚠️ [승인 필요] {name}({_compact(args)})")
    if auto:
        print("      → --yes: 자동 승인")
        return True
    try:
        ans = input("      실행할까요? (y/N) ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes", "ㅇ")


def dispatch(name: str, args: dict) -> dict:
    fn = tools.DISPATCH.get(name)
    if fn is None:
        return {"ok": False, "error": f"알 수 없는 도구: {name}"}
    try:
        return fn(**args)
    except TypeError as e:
        return {"ok": False, "error": f"인자 오류: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"실행 오류: {type(e).__name__}: {e}"}


def run_agent_turn(client, messages: list, auto_approve: bool, sid: str | None = None) -> None:
    turns = 0
    while True:
        turns += 1
        if turns > MAX_TURNS:
            print(f"   ⚠️ 최대 도구 호출({MAX_TURNS}) 도달 — 중단.")
            messages.append({"role": "assistant", "content": f"(중단: {MAX_TURNS}회 상한)"})
            if sid:
                session.save(sid, messages)
            return

        resp = _create(client, messages)

        if config.DEBUG:
            u = getattr(resp, "usage", None)
            if u is not None:
                print(f"   [usage] in={getattr(u, 'input_tokens', None)} "
                      f"cache_read={getattr(u, 'cache_read_input_tokens', None)} "
                      f"cache_write={getattr(u, 'cache_creation_input_tokens', None)} "
                      f"out={getattr(u, 'output_tokens', None)}")

        text_out, tool_uses = [], []
        for b in resp.content:
            if getattr(b, "type", None) == "text":
                text_out.append(b.text)
            elif getattr(b, "type", None) == "tool_use":
                tool_uses.append(b)

        # Salvage tool calls the model wrote as TEXT (<invoke...>) instead of a structured
        # tool_use block: clean the raw XML from display + history (so the model won't imitate
        # its own leaked format next turn), and below execute them so the loop doesn't stall.
        display_text, salvage_calls = _extract_text_tool_calls("\n".join(t for t in text_out if t))
        serialized = _serialize_content(resp.content)
        if salvage_calls:
            serialized = [{"type": "text", "text": display_text or "(도구 호출)"}]
        messages.append({"role": "assistant", "content": serialized})
        if display_text:
            print("\n🤖 " + display_text + "\n")

        if not tool_uses and salvage_calls:
            results = []
            for _name, _args in salvage_calls:
                print(f"   ⏳ [{_name}] (텍스트 호출 복구) {_compact(_args)}")
                _res = (dispatch(_name, _args) if _approve(_name, _args, auto_approve)
                        else {"ok": False, "declined": True, "error": "사용자가 거부했습니다."})
                print(f"      → {_summarize(_name, _res)}")
                results.append(f"[{_name}] → " + json.dumps(_res, ensure_ascii=False, default=str))
            messages.append({"role": "user", "content":
                "(직전 응답에서 도구를 텍스트로 호출했습니다 — 앞으로는 반드시 tool_use로 호출하세요. 복구 실행 결과)\n"
                + "\n".join(results)})
            if sid:
                session.save(sid, messages)
            continue

        if not tool_uses:
            if resp.stop_reason == "pause_turn":
                # A server tool (web_search/web_fetch) is mid-execution: the partial
                # assistant turn is already appended — re-request to let Claude resume.
                if sid:
                    session.save(sid, messages)
                continue
            if resp.stop_reason == "max_tokens":
                print("   ⚠️ 응답이 max_tokens로 잘렸습니다. OF_AGENT_MAX_TOKENS를 늘려보세요.")
            if sid:
                session.save(sid, messages)
            return

        tool_results: list = []
        try:
            for tu in tool_uses:
                args = dict(tu.input) if isinstance(tu.input, dict) else {}
                print(f"   ⏳ [{tu.name}] {_compact(args)}")
                if not _approve(tu.name, args, auto_approve):
                    result = {"ok": False, "declined": True,
                              "error": "사용자가 실행을 거부했습니다. 다른 설정을 제안하거나 이유를 물어보세요."}
                else:
                    result = dispatch(tu.name, args)
                print(f"      → {_summarize(tu.name, result)}")
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                     "content": json.dumps(result, ensure_ascii=False, default=str)})
        finally:
            for tu in tool_uses[len(tool_results):]:    # guarantee pairing
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                     "content": json.dumps({"ok": False, "error": "중단됨"},
                                                           ensure_ascii=False)})
            messages.append({"role": "user", "content": tool_results})

        if sid:
            session.save(sid, messages)     # crash-safe checkpoint each round


def main() -> int:
    ap = argparse.ArgumentParser(description="myOpenFoamAgent — OpenFOAM tool-use 에이전트")
    ap.add_argument("--task", default=None, help="한 번에 처리할 작업 지시(비대화)")
    ap.add_argument("--yes", action="store_true", help="승인 게이트 자동 승인(배치/검증용)")
    ap.add_argument("--resume", default=None, help="이전 세션 id를 이어서 진행")
    ap.add_argument("--session", default=None, help="세션 id 지정(기본: 자동 생성)")
    ap.add_argument("--list-sessions", action="store_true", help="저장된 세션 목록 출력 후 종료")
    args = ap.parse_args()

    if args.list_sessions:
        rows = session.list_sessions()
        if not rows:
            print("저장된 세션 없음.")
        for r in rows:
            print(f"  {r['session_id']}  ({r['messages']} msgs, {r['saved_at']})")
        return 0

    try:
        from anthropic import Anthropic
    except ImportError:
        print("anthropic 미설치: pip install -r requirements.txt", file=sys.stderr)
        return 1
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("환경변수 ANTHROPIC_API_KEY 가 설정되지 않았습니다.", file=sys.stderr)
        print('  export ANTHROPIC_API_KEY=sk-ant-...', file=sys.stderr)
        return 1

    client = Anthropic()
    config.RUNS_ROOT.mkdir(parents=True, exist_ok=True)

    messages: list = []
    if args.resume:
        sid = args.resume
        loaded = session.load(sid)
        if loaded is None:
            print(f"세션을 찾을 수 없습니다: {sid}", file=sys.stderr)
            return 1
        messages = loaded
        print(f"세션 이어받음: {sid} ({len(messages)} msgs)")
    else:
        sid = args.session or session.new_session_id()

    print(f"myOpenFoamAgent ▸ model={config.MODEL} ▸ session={sid} "
          f"▸ cache={'on' if config.PROMPT_CACHE else 'off'} ▸ ctx={config.CONTEXT_STRATEGY}")

    if args.task:
        messages.append({"role": "user", "content": args.task})
        run_agent_turn(client, messages, auto_approve=args.yes, sid=sid)
        # --task가 끝나면(특히 에이전트가 질문하며 멈추면) 종료하지 않고 대화형으로
        # 이어받아, 사용자가 답하거나 후속 지시를 줄 수 있게 한다.
        # (tty가 없는 자동 실행이면 _read_user가 EOF를 받아 깔끔히 종료된다.)
        print("\n(대화형 모드 — 위 질문에 답하거나 이어서 지시하세요. 종료: exit/quit/종료)")
    else:
        print("무엇을 시뮬레이션할까요? (예: 'cavity 케이스 셋업하고 짧게 돌려줘')  종료: exit/quit/종료")

    while True:
        try:
            user = _read_user("\n🧑 ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user.lower() in {"exit", "quit", "종료", "나가기"}:
            break
        messages.append({"role": "user", "content": user})
        try:
            run_agent_turn(client, messages, auto_approve=args.yes, sid=sid)
        except KeyboardInterrupt:
            print("\n   (중단됨 — 계속 입력하세요)")
        except Exception as e:
            print(f"⚠️ 오류: {type(e).__name__}: {e}")
        # Self-evolution: a kernel self-edit leaves a promote signal. Exit with the
        # restart code so bootstrap.py re-validates (canary) and relaunches fresh code.
        try:
            import selfevolve
            if selfevolve.PROMOTE_SIGNAL.is_file():
                print("\n🔄 커널 자가수정 적용됨 — bootstrap 재시작을 위해 종료(42). "
                      "(bootstrap.py로 실행 중이 아니면 수동 재시작하세요.)")
                return 42
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
