#!/usr/bin/env python3
"""Quick E2E test for weather query with fresh session."""
from __future__ import annotations
import asyncio, json, os, sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from openclaw_gateway_runtime.infra import load_dotenv
from openclaw_gateway_runtime.protocol import frame
import websockets

async def main():
    load_dotenv()
    url = os.environ.get("GATEWAY_WS_URL", "ws://127.0.0.1:8765")
    text = sys.argv[1] if len(sys.argv) > 1 else "帮我搜索一下今天北京天气怎么样"
    sid = "weather-fresh"
    print(f"[e2e] sending: {text}\n")
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(frame(type="message.user", session_id=sid, payload={"text": text})))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            obj = json.loads(raw)
            t = obj.get("type")
            if t == "stream.chunk":
                print((obj.get("payload") or {}).get("text", ""), end="", flush=True)
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
                print("[run.started]", flush=True)
            elif t == "run.end":
                print(f"\n[run.end] reason={obj.get('reason')}", flush=True)
                break
            elif t == "error":
                print(f"\n[error] {obj.get('error')}", flush=True)
                break

if __name__ == "__main__":
    asyncio.run(main())
