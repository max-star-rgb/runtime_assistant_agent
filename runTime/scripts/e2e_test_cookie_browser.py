"""
E2E test for browser cookie injection + Taobao demo.

Test steps:
  1. Inject test cookies for .taobao.com
  2. Navigate to taobao.com
  3. Verify cookies are present
  4. Take a snapshot to see page state

Usage:
    python scripts/e2e_test_cookie_browser.py
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


async def send_and_recv(ws: object, session_id: str, text: str, timeout: int = 120) -> None:
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
                preview = result_text[:200] + ("..." if len(result_text) > 200 else "")
                print(f"\n  [tool result] {name} error={is_err} | {preview}")
            else:
                inp = p.get("input", {})
                print(f"\n  [tool call] {name} {json.dumps(inp, ensure_ascii=False)[:200]}")
        elif t == "run.started":
            print(f"\n[run.started] run_id={p.get('run_id')}")
        elif t == "run.end":
            print(f"\n[run.end] reason={p.get('reason')}")
            break


async def main() -> None:
    import websockets

    load_dotenv()
    url = os.environ.get("GATEWAY_WS_URL", "ws://127.0.0.1:8765")
    sid = "cookie-test"

    prompt = (
        "请使用browser工具完成以下步骤（按顺序执行）：\n"
        "1. 先用 set_cookies 注入以下测试cookie：\n"
        '   cookies=[{"name":"test_demo","value":"hello123","domain":".taobao.com","path":"/"}]\n'
        "2. 然后 navigate 到 https://www.taobao.com\n"
        "3. 用 get_cookies 查看当前 cookie 确认注入成功\n"
        "4. 用 snapshot 获取页面快照查看页面状态\n"
        "每步都执行，最后总结结果。"
    )

    print(f"[e2e-cookie] connecting to {url}")
    async with websockets.connect(url) as ws:
        await send_and_recv(ws, sid, prompt)

    print("\n\n[e2e-cookie] DONE")


if __name__ == "__main__":
    asyncio.run(main())
