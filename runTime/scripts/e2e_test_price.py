#!/usr/bin/env python3
"""
Non-interactive E2E test: send one message, print all frames until run.end.
Usage: python scripts/e2e_test_price.py "帮我比较下罗技MX Master 3在京东淘宝拼多多的价格"
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from openclaw_gateway_runtime.infra import load_dotenv
from openclaw_gateway_runtime.protocol import frame

import websockets


async def main() -> None:
    load_dotenv()
    url = os.environ.get("GATEWAY_WS_URL", "ws://127.0.0.1:8765")
    text = sys.argv[1] if len(sys.argv) > 1 else "帮我比较下罗技MX Master 3在京东淘宝拼多多的价格"
    sid = "e2e-test"

    print(f"[e2e] connecting to {url}")
    print(f"[e2e] sending: {text}\n")

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(frame(type="message.user", session_id=sid, payload={"text": text})))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=180)
            obj = json.loads(raw)
            t = obj.get("type")
            if t == "stream.chunk":
                txt = (obj.get("payload") or {}).get("text") or ""
                print(txt, end="", flush=True)
            elif t == "event.tool":
                p = obj.get("payload") or {}
                phase = p.get("phase", "call")
                name = p.get("name", "?")
                if phase == "result":
                    is_err = (p.get("result") or {}).get("is_error")
                    print(f"\n  [tool result] {name} error={is_err}", flush=True)
                else:
                    inp = p.get("input") or {}
                    print(f"\n  [tool call] {name} {json.dumps(inp, ensure_ascii=False)[:200]}", flush=True)
            elif t == "run.started":
                print(f"[run.started] run_id={obj.get('run_id')}", flush=True)
            elif t == "run.end":
                reason = obj.get("reason")
                err = obj.get("error")
                print(f"\n[run.end] reason={reason}", flush=True)
                if err:
                    print(f"  error: {err}", flush=True)
                break
            elif t == "error":
                print(f"\n[error] {obj.get('error')}", flush=True)
                break


if __name__ == "__main__":
    asyncio.run(main())
