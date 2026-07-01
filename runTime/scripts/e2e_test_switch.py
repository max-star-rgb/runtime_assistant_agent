#!/usr/bin/env python3
"""Test: web_search then switch to skill - verify no lock conflict."""
from __future__ import annotations
import asyncio, json, os, sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from openclaw_gateway_runtime.infra import load_dotenv
from openclaw_gateway_runtime.protocol import frame
import websockets

async def send_and_recv(ws, sid, text):
    print(f"\n{'='*60}")
    print(f"[USER] {text}")
    print("=" * 60)
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

async def main():
    load_dotenv()
    sid = "switch-test"
    print("[test] Turn 1: web_search (no skill)")
    print("[test] Turn 2: should be able to use taobao skill")
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        await send_and_recv(ws, sid, "帮我搜索一下今天北京天气")
        await send_and_recv(ws, sid, "帮我查一下罗技鼠标在淘宝上多少钱")
    print("\n\n[test] DONE")

if __name__ == "__main__":
    asyncio.run(main())
