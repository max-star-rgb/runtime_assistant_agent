"""
E2E test: JD.com add to cart via browser + cookie injection.

Usage:
    python scripts/e2e_test_jd_cart.py
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
    sid = "jd-cart-test"

    prompt = (
        "请用browser工具完成京东加购测试，严格按步骤执行，不要增加额外步骤：\n"
        "1. set_cookies_file file_path=D:\\cookies\\taobao.json\n"
        "2. navigate url=https://search.jd.com/Search?keyword=华为mate70手机壳&enc=utf-8\n"
        "3. wait timeout_s=2\n"
        "4. evaluate 提取商品：script=() => [...document.querySelectorAll('[data-sku]')].slice(0,5).map(el => ({title: el.querySelector('[title]')?.getAttribute('title'), url: 'https://item.jd.com/' + el.getAttribute('data-sku') + '.html'}))\n"
        "5. 从第4步结果中选第一个商品，navigate 到它的url\n"
        "6. wait timeout_s=2\n"
        "7. click selector=#add-to-cart\n"
        "8. wait timeout_s=1 后告诉我加购完成\n"
        "不要做snapshot，不要额外探测，严格按这8步执行！"
    )

    print(f"[e2e-jd] connecting to {url}")
    async with websockets.connect(url) as ws:
        await send_and_recv(ws, sid, prompt)

    print("\n\n[e2e-jd] DONE")


if __name__ == "__main__":
    asyncio.run(main())
