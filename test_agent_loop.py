#!/usr/bin/env python3
"""
test_agent_loop.py — verify the tool-use loop WITHOUT the Anthropic API / key.

A fake client returns a scripted (tool_use -> end_turn) sequence; we check the
loop dispatches the real tool, pairs every tool_use with a tool_result, preserves
the assistant blocks, and stops on end_turn. Exercises the risky new loop code in
agent.py deterministically.

Run inside WSL:  python3 test_agent_loop.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent


class Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, scripted):
        self.scripted, self.calls = scripted, 0

    def create(self, **kw):
        r = self.scripted[self.calls]
        self.calls += 1
        return r


class FakeClient:
    def __init__(self, scripted):
        self.messages = FakeMessages(scripted)


def main() -> int:
    tu = Blk(type="tool_use", id="t1", name="list_tutorials",
             input={"filter": "cavity", "limit": 5})
    scripted = [
        Resp([Blk(type="text", text="Let me look for tutorials."), tu], stop_reason="tool_use"),
        Resp([Blk(type="text", text="Found some cavity candidates.")], stop_reason="end_turn"),
    ]
    client = FakeClient(scripted)
    messages = [{"role": "user", "content": "find cavity"}]

    agent.run_agent_turn(client, messages, auto_approve=True)

    ok = True
    # expected message tape: user, assistant(text+tool_use), user(tool_result), assistant(text)
    ok &= len(messages) == 4
    ok &= messages[1]["role"] == "assistant"
    assistant_blocks = messages[1]["content"]
    ok &= any(b["type"] == "tool_use" and b["id"] == "t1" for b in assistant_blocks)
    tr = messages[2]["content"][0]
    ok &= tr["type"] == "tool_result" and tr["tool_use_id"] == "t1"
    payload = json.loads(tr["content"])
    ok &= payload.get("ok") is True and isinstance(payload.get("count"), int)
    ok &= client.messages.calls == 2          # looped exactly twice, stopped on end_turn

    print(f"[loop] api_calls={client.messages.calls} msgs={len(messages)} "
          f"tutorials_found={payload.get('count')}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
