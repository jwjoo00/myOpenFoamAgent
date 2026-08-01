#!/usr/bin/env python3
"""test_session.py — conversation persistence round-trip. NO LLM, NO key."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        config.RUNS_ROOT = Path(d)
        config.SESSIONS_DIR = config.RUNS_ROOT / "sessions"
        import session

        ok = True
        sid = session.new_session_id()
        msgs = [
            {"role": "user", "content": "cavity 돌려줘"},
            {"role": "assistant", "content": [{"type": "text", "text": "네"},
                                              {"type": "tool_use", "id": "t1",
                                               "name": "copy_case", "input": {"tutorial": "x"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": "{\"ok\": true}"}]},
        ]
        session.save(sid, msgs)
        back = session.load(sid)
        ok &= back == msgs
        print(f"[save/load] {sid}: {len(back) if back else 0} msgs, equal={back == msgs}")

        sid2 = session.new_session_id()
        ok &= sid2 != sid
        session.save(sid2, [{"role": "user", "content": "x"}])
        names = [r["session_id"] for r in session.list_sessions()]
        ok &= sid in names and sid2 in names
        print(f"[list] {names}")

        ok &= session.load("nope_does_not_exist") is None
        print("\nRESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
