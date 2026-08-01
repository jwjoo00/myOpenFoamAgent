#!/usr/bin/env python3
"""test_context.py — prompt-cache / trim / serialize helpers. NO LLM, NO key."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
config.CONTEXT_TRIM_CHARS = 2000      # small cap so trimming triggers in the test

import agent


class FakeBlk:
    """A response block exposing model_dump, like a real SDK block."""
    def __init__(self, d):
        self._d = d

    def model_dump(self, **k):
        return self._d


def main() -> int:
    ok = True

    # 1) serialize preserves ALL block types (compaction must survive)
    ser = agent._serialize_content([FakeBlk({"type": "text", "text": "hi"}),
                                    FakeBlk({"type": "compaction", "x": 1})])
    ok &= any(b.get("type") == "compaction" for b in ser)
    print(f"[serialize] types={[b.get('type') for b in ser]}")

    # 2) rolling cache_control on the last block of the last list-content user msg;
    #    the stored message is NOT mutated
    msgs = [{"role": "user", "content": "task"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": "R"}]}]
    cv = agent._cache_messages(msgs)
    ok &= cv[-1]["content"][-1].get("cache_control") == {"type": "ephemeral"}
    ok &= "cache_control" not in msgs[-1]["content"][-1]
    print(f"[cache] marked=True orig_clean={'cache_control' not in msgs[-1]['content'][-1]}")

    # 3) trim elides old tool_result contents, preserves tool_use<->tool_result pairing
    big = "X" * 300
    many = [{"role": "user", "content": "task"}]
    for i in range(40):
        many.append({"role": "assistant",
                     "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}]})
        many.append({"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": big}]})
    tv = agent._trim_messages(many)
    elided = sum(1 for m in tv if m.get("role") == "user" and isinstance(m.get("content"), list)
                 and any(isinstance(b, dict) and "elided" in str(b.get("content", ""))
                         for b in m["content"]))
    pairing_ok = all(any(isinstance(b, dict) and b.get("tool_use_id") for b in m["content"])
                     for m in tv if m.get("role") == "user" and isinstance(m.get("content"), list))
    ok &= elided > 0 and pairing_ok
    ok &= agent._msg_chars(tv) < agent._msg_chars(many)
    print(f"[trim] elided={elided} pairing_ok={pairing_ok} "
          f"chars {agent._msg_chars(many)}->{agent._msg_chars(tv)}")

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
