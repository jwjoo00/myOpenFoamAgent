"""
session.py — conversation persistence (Phase 4, feature 1).

The agent's `messages` list is the whole conversation context. In-memory only, it
dies with the process (a --task crash lost everything). This saves it to disk per
session so a run can be resumed with `--resume <id>`, and checkpoints after every
tool round so a crash mid-task keeps the progress.

One JSON file per session under config.SESSIONS_DIR (ext4 — state, not a report).
No LLM dependency.
"""
from __future__ import annotations

import datetime
import json
import uuid
from pathlib import Path

import config


def _dir() -> Path:
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return config.SESSIONS_DIR


def new_session_id() -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"sess_{ts}_{uuid.uuid4().hex[:4]}"


def _path(sid: str) -> Path:
    return _dir() / f"{sid}.json"


def save(sid: str, messages: list) -> None:
    """Atomic write (tmp + replace) so a crash mid-save never corrupts the file."""
    payload = {
        "session_id": sid,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }
    tmp = _path(sid).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str))
    tmp.replace(_path(sid))


def load(sid: str) -> list | None:
    p = _path(sid)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text()).get("messages", [])
    except Exception:
        return None


def list_sessions(limit: int = 30) -> list[dict]:
    d = _dir()
    files = sorted(d.glob("sess_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        try:
            data = json.loads(f.read_text())
            out.append({"session_id": f.stem, "saved_at": data.get("saved_at"),
                        "messages": len(data.get("messages", []))})
        except Exception:
            out.append({"session_id": f.stem, "saved_at": "?", "messages": "?"})
    return out
