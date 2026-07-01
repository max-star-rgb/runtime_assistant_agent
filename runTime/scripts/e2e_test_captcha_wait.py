"""
Quick test: navigate directly to a Taobao item page to verify CAPTCHA auto-wait.

Usage:
    python scripts/e2e_test_captcha_wait.py
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


async def send_and_recv(ws, session_id: str, text: str) -> None:
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
                result_text = ""
                for c in (p.get("result") or {}).get("content", []):
                    result_text += c.get("text", "")
                preview = result_text[:500] + ("..." if len(result_text) > 500 else "")
                print(f"\n  [tool result] {name} error={is_err} | {preview}")
            else:
                inp = p.get("input", {})
                print(f"\n  [tool call] {name} {json.dumps(inp, ensure_ascii=False)[:300]}")
        elif t == "run.started":
            print(f"\n[run.started]")
        elif t == "run.end":
            print(f"\n[run.end]")
            break


async def main() -> None:
    import websockets

    load_dotenv()
    url = os.environ.get("GATEWAY_WS_URL", "ws://127.0.0.1:8765")
    sid = "captcha-wait-test"

    prompt = (
        "请用browser工具执行以下步骤：\n"
        "1. set_cookies_file 注入 D:\\cookies\\taobao.json\n"
        "2. navigate 到 https://item.taobao.com/item.htm?id=635180501529\n"
        "3. snapshot 查看页面\n"
        "如果遇到验证码，navigate 会自动等待，请在浏览器窗口中手动完成验证。"
    )

    print(f"[e2e] connecting to {url}")
    async with websockets.connect(url) as ws:
        await send_and_recv(ws, sid, prompt)

    print("\n\n[e2e] DONE")


if __name__ == "__main__":
    asyncio.run(main())
