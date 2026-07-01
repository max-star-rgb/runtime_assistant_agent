"""
E2E test for the browser tool.

Sends a message asking the agent to navigate to example.com and take a snapshot,
verifying the browser tool works end-to-end through Gateway → Runtime → Playwright.

Usage:
    python scripts/e2e_test_browser.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openclaw_gateway_runtime.infra.dotenv import load_dotenv


def frame(*, type: str, session_id: str, payload: dict) -> dict:
    return {"type": type, "session_id": session_id, "payload": payload}


async def send_and_recv(ws: object, session_id: str, text: str) -> None:
    import websockets

    print(f"\n{'='*60}")
    print(f"[user] {text}")
    print("=" * 60)

    await ws.send(json.dumps(frame(type="message.user", session_id=session_id, payload={"text": text})))

    async for raw in ws:
        msg = json.loads(raw)
        t = msg.get("type", "")
        p = msg.get("payload") or {}

        if t == "stream.chunk":
            print(p.get("text", ""), end="", flush=True)
        elif t == "event.tool":
            phase = p.get("phase", "call")
            name = p.get("name", "?")
            if phase == "result":
                is_err = (p.get("result") or {}).get("is_error", False)
                print(f"\n  [tool result] {name} error={is_err}")
            else:
                inp = p.get("input", {})
                print(f"\n  [tool call] {name} {json.dumps(inp, ensure_ascii=False)}")
        elif t == "run.started":
            print(f"\n[run.started] run_id={p.get('run_id')}")
        elif t == "run.end":
            print(f"\n[run.end] reason={p.get('reason')}")
            break


async def main() -> None:
    import websockets

    load_dotenv()
    url = os.environ.get("GATEWAY_WS_URL", "ws://127.0.0.1:8765")
    sid = "browser-test"

    text = sys.argv[1] if len(sys.argv) > 1 else (
        "请使用browser工具打开 https://example.com 页面，获取snapshot查看页面结构"
    )

    print(f"[e2e-browser] connecting to {url}")
    print(f"[e2e-browser] sending: {text}")

    async with websockets.connect(url) as ws:
        await send_and_recv(ws, sid, text)

    print("\n\n[e2e-browser] DONE")


if __name__ == "__main__":
    asyncio.run(main())
