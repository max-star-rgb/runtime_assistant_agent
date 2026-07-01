#!/usr/bin/env python3
"""
交互式通话模式测试客户端

模拟电信通话场景：
  1. 发 call.incoming（来电）
  2. 交互式发消息对话
  3. 输入 /hangup 或 Ctrl+C 挂断

用法：
  python scripts/cli_call_client.py
  python scripts/cli_call_client.py --url ws://127.0.0.1:8765 --user-id 13800138000
  python scripts/cli_call_client.py --language en-US --user-info "VIP用户"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from openclaw_gateway_runtime.infra import load_dotenv

import websockets


HELP_TEXT = """
可用命令：
  /hangup          挂断通话
  /config <k> <v>  下发配置（通道2），如 /config language en-US
  /cancel          取消当前生成
  /session         显示当前 session_id 和 user_id
  /help            显示此帮助
  （直接输入文字发送消息）
"""


async def recv_loop(ws, user_id: str, stop_event: asyncio.Event) -> None:
    """后台持续接收帧并打印。"""
    try:
        async for raw in ws:
            obj = json.loads(raw)
            t = obj.get("type", "")
            uid = obj.get("user_id", "")

            if t == "stream.chunk":
                txt = (obj.get("payload") or {}).get("text") or ""
                print(txt, end="", flush=True)
            elif t == "run.started":
                print(f"\n[▶ run.started  run_id={obj.get('run_id', '')[:8]}…]", flush=True)
            elif t == "run.end":
                reason = obj.get("reason", "?")
                payload = obj.get("payload") or {}
                expects_reply = payload.get("expects_reply", True)
                icon = "✓" if reason == "completed" else "✗"
                print(f"\n[{icon} run.end  reason={reason}  expects_reply={expects_reply}]\n", flush=True)
                if not expects_reply:
                    print("[expects_reply=false] 本轮无需用户回复", flush=True)
            elif t == "event.tool":
                p = obj.get("payload") or {}
                phase = p.get("phase", "call")
                name = p.get("name", "?")
                if phase == "result":
                    is_err = (p.get("result") or {}).get("is_error", False)
                    print(f"  [工具结果] {name}  error={is_err}", flush=True)
                else:
                    inp = json.dumps(p.get("input") or {}, ensure_ascii=False)
                    print(f"  [工具调用] {name}  {inp[:120]}", flush=True)
            elif t == "call.hangup_ack":
                print(f"\n[☎ 通话已结束  user_id={uid}]", flush=True)
                stop_event.set()
                break
            elif t == "error":
                err = obj.get("error") or {}
                print(f"\n[✗ 错误]  code={err.get('code')}  msg={err.get('message')}", flush=True)
            elif t == "call.ready":
                pass  # already handled in main
            else:
                print(f"\n[帧] {t}  user_id={uid}", flush=True)
    except websockets.exceptions.ConnectionClosed:
        stop_event.set()


async def main_async(args: argparse.Namespace) -> int:
    url = args.url
    user_id = args.user_id or f"call-{uuid.uuid4().hex[:8]}"

    print(f"\n☎  连接 {url!r}")
    print(f"   用户号码: {user_id}")
    print(f"   语言: {args.language}  |  用户信息: {args.user_info or '（无）'}")
    print("   发送来电请求…\n")

    try:
        ws = await websockets.connect(url)
    except Exception as e:
        print(f"[错误] 无法连接 Gateway: {e}")
        return 1

    # 1. 发送 call.incoming
    call_frame = {
        "v": 1,
        "type": "call.incoming",
        "user_id": user_id,
        "payload": {
            "language": args.language,
            "user_info": args.user_info or "",
            "agent_profile": args.profile,
        },
    }
    await ws.send(json.dumps(call_frame))

    # 2. 等待 call.ready
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        obj = json.loads(raw)
        if obj.get("type") != "call.ready":
            print(f"[错误] 未收到 call.ready，收到: {obj}")
            await ws.close()
            return 1
        session_id: str = obj["session_id"]
        print(f"[✓ 通话接通]  session_id={session_id}")
        print("输入消息后回车；/help 查看命令\n")
    except asyncio.TimeoutError:
        print("[错误] 等待 call.ready 超时")
        await ws.close()
        return 1

    stop_event = asyncio.Event()
    recv_task = asyncio.create_task(recv_loop(ws, user_id, stop_event))
    loop = asyncio.get_event_loop()

    while not stop_event.is_set():
        try:
            text = await loop.run_in_executor(None, sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            text = "/hangup"

        text = text.strip()
        if not text:
            continue

        if text == "/help":
            print(HELP_TEXT)
            continue

        if text == "/session":
            print(f"  user_id={user_id}  session_id={session_id}")
            continue

        if text == "/hangup":
            hangup_frame = {
                "v": 1, "type": "call.hangup",
                "user_id": user_id, "session_id": session_id,
            }
            await ws.send(json.dumps(hangup_frame))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                print("[警告] 未收到 hangup_ack")
            break

        if text.startswith("/config "):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                print("用法: /config <key> <value>   例: /config language en-US")
                continue
            _, key, value = parts
            cfg_frame = {
                "v": 1, "type": "config.update",
                "user_id": user_id,
                "payload": {"key": key, "value": value},
            }
            await ws.send(json.dumps(cfg_frame))
            print(f"  [已发送] config.update: {key} = {value}")
            continue

        if text == "/cancel":
            cancel_frame = {
                "v": 1, "type": "run.cancel",
                "user_id": user_id, "session_id": session_id,
            }
            await ws.send(json.dumps(cancel_frame))
            print("  [已发送] run.cancel")
            continue

        # 普通消息
        msg_frame = {
            "v": 1, "type": "message.user",
            "user_id": user_id, "session_id": session_id,
            "payload": {"text": text},
        }
        await ws.send(json.dumps(msg_frame))

    recv_task.cancel()
    try:
        await recv_task
    except asyncio.CancelledError:
        pass

    await ws.close()
    return 0


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="交互式通话模式测试客户端")
    parser.add_argument("--url", default=os.environ.get("GATEWAY_WS_URL", "ws://127.0.0.1:8765"))
    parser.add_argument("--user-id", default=None, help="用户号码（默认随机生成）")
    parser.add_argument("--language", default="zh-CN", help="语言设定（默认 zh-CN）")
    parser.add_argument("--user-info", default="", help="用户简介")
    parser.add_argument("--profile", default="default", help="Agent 配置文件名")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
