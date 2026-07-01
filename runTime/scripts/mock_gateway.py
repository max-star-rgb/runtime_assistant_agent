"""
Mock Gateway — 用于电话平台对接测试。

不连接 Runtime / LLM，直接返回 mock 流式回复。
帧格式、时序、字段与真实 Gateway 完全一致，确保前端对接后可无缝切换。

用法：
  python scripts/mock_gateway.py
  python scripts/mock_gateway.py --port 8765
  python scripts/mock_gateway.py --delay 0.05   # 每个 chunk 间隔（秒）

支持：
  - call.incoming → call.ready（电话模式）
  - message.user → run.started → stream.chunk × N → run.end（流式回复）
  - run.cancel → run.end(cancelled)
  - call.hangup → call.hangup_ack
  - config.update（静默接受）
  - ping → pong
  - session.open / session.resume（Legacy 模式）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any, Literal, Optional, TypedDict

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import websockets

ProtocolVersion = Literal[1]

# Call-session frame types (telephony scenario)
CALL_INCOMING = "call.incoming"
CALL_READY = "call.ready"
CALL_HANGUP = "call.hangup"
CALL_HANGUP_ACK = "call.hangup_ack"
CONFIG_UPDATE = "config.update"


class Frame(TypedDict, total=False):
    v: ProtocolVersion
    type: str
    session_id: str
    turn_id: str
    run_id: str
    # user identifier carried on every frame in telephony scenario
    user_id: str
    payload: Any
    reason: str
    error: Any


def frame(
    *,
    type: str,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    run_id: Optional[str] = None,
    user_id: Optional[str] = None,
    payload: Any = None,
    reason: Optional[str] = None,
    error: Any = None,
    v: ProtocolVersion = 1,
) -> Frame:
    f: Frame = {"v": v, "type": type}
    if session_id is not None:
        f["session_id"] = session_id
    if turn_id is not None:
        f["turn_id"] = turn_id
    if run_id is not None:
        f["run_id"] = run_id
    if user_id is not None:
        f["user_id"] = user_id
    if payload is not None:
        f["payload"] = payload
    if reason is not None:
        f["reason"] = reason
    if error is not None:
        f["error"] = error
    return f


# ---------------------------------------------------------------------------
# Mock 回复内容
# ---------------------------------------------------------------------------

MOCK_REPLIES: dict[str, list[str]] = {
    "default": [
        "你好！",
        "我是智能助手，",
        "很高兴为你服务。",
        "请问有什么可以帮你的吗？",
    ],
    "买": [
        "帮你搜搜看~\n\n",
        "找到了以下商品：\n\n",
        "1. **华为Mate70 Pro** 12GB+512GB\n",
        "   - 京东价：4689元（优惠券15元）\n",
        "   - 拼多多价：3629元（优惠券150元）\n\n",
        "2. **华为Mate70 Pro** 16GB+512GB\n",
        "   - 京东价：5299元（优惠券100元）\n\n",
        "需要我帮你加入购物车吗？",
    ],
    "谢": [
        "不客气，",
        "有需要随时找我~",
    ],
    "再见": [
        "再见！",
        "祝你生活愉快~",
    ],
}


def _pick_reply(text: str) -> list[str]:
    """根据用户输入关键词选择 mock 回复。"""
    for keyword, chunks in MOCK_REPLIES.items():
        if keyword == "default":
            continue
        if keyword in text:
            return chunks
    return MOCK_REPLIES["default"]


def _expects_reply(text: str) -> bool:
    """判断是否需要用户继续回复。"""
    farewell = ("谢", "再见", "拜拜", "不用了", "好的", "OK", "ok", "没事了")
    return not any(kw in text for kw in farewell)


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

async def handle_connection(ws: Any, chunk_delay: float) -> None:
    """处理单个 WebSocket 连接。"""
    client_id = str(uuid.uuid4())[:8]
    session_id: str | None = None
    user_id: str | None = None
    active_cancel: asyncio.Event | None = None

    print(f"[{client_id}] 连接建立", flush=True)

    try:
        async for raw in ws:
            f = json.loads(raw)
            ftype = f.get("type", "")
            uid = f.get("user_id") or user_id

            # ── call.incoming（电话模式入口）──
            if ftype == CALL_INCOMING:
                user_id = str(f.get("user_id") or (f.get("payload") or {}).get("user_id") or uuid.uuid4())
                session_id = str(uuid.uuid4())
                await ws.send(json.dumps(
                    frame(type=CALL_READY, user_id=user_id, session_id=session_id)
                ))
                print(f"[{client_id}] call.incoming → call.ready  user={user_id}  session={session_id[:8]}", flush=True)

            # ── session.open / session.resume（Legacy 模式）──
            elif ftype in ("session.open", "session.resume"):
                payload = f.get("payload") or {}
                session_id = payload.get("session_id") or f.get("session_id") or str(uuid.uuid4())
                user_id = uid or str(uuid.uuid4())
                print(f"[{client_id}] {ftype}  session={session_id[:8]}", flush=True)

            # ── message.user → 流式回复 ──
            elif ftype == "message.user":
                payload = f.get("payload") or {}
                text = str(payload.get("text", ""))
                sid = f.get("session_id") or session_id or str(uuid.uuid4())
                session_id = sid
                if not user_id:
                    user_id = uid or str(uuid.uuid4())

                turn_id = str(uuid.uuid4())
                run_id = str(uuid.uuid4())

                print(f"[{client_id}] message.user: {text[:50]}", flush=True)

                # run.started
                await ws.send(json.dumps(
                    frame(type="run.started", session_id=sid, turn_id=turn_id, run_id=run_id, user_id=user_id)
                ))

                # stream.chunk × N（模拟流式输出）
                active_cancel = asyncio.Event()
                chunks = _pick_reply(text)
                cancelled = False

                for chunk_text in chunks:
                    if active_cancel.is_set():
                        cancelled = True
                        break
                    await ws.send(json.dumps(
                        frame(
                            type="stream.chunk",
                            session_id=sid,
                            turn_id=turn_id,
                            run_id=run_id,
                            user_id=user_id,
                            payload={"text": chunk_text},
                        )
                    ))
                    await asyncio.sleep(chunk_delay)

                # run.end
                if cancelled:
                    await ws.send(json.dumps(
                        frame(
                            type="run.end",
                            session_id=sid,
                            turn_id=turn_id,
                            run_id=run_id,
                            user_id=user_id,
                            reason="cancelled",
                            payload={"expects_reply": True},
                        )
                    ))
                    print(f"[{client_id}] run.end reason=cancelled", flush=True)
                else:
                    er = _expects_reply(text)
                    await ws.send(json.dumps(
                        frame(
                            type="run.end",
                            session_id=sid,
                            turn_id=turn_id,
                            run_id=run_id,
                            user_id=user_id,
                            reason="completed",
                            payload={"expects_reply": er},
                        )
                    ))
                    print(f"[{client_id}] run.end reason=completed expects_reply={er}", flush=True)

                active_cancel = None

            # ── run.cancel ──
            elif ftype == "run.cancel":
                if active_cancel:
                    active_cancel.set()
                print(f"[{client_id}] run.cancel", flush=True)

            # ── call.hangup ──
            elif ftype == CALL_HANGUP:
                await ws.send(json.dumps(
                    frame(type=CALL_HANGUP_ACK, user_id=uid)
                ))
                print(f"[{client_id}] call.hangup → hangup_ack", flush=True)
                break

            # ── config.update（静默接受）──
            elif ftype == "config.update":
                payload = f.get("payload") or {}
                print(f"[{client_id}] config.update: {payload.get('key')}={payload.get('value')}", flush=True)

            # ── ping ──
            elif ftype == "ping":
                await ws.send(json.dumps(frame(type="pong", user_id=uid)))

            # ── 未知帧 ──
            else:
                await ws.send(json.dumps(
                    frame(type="error", user_id=uid, error={"code": "unknown_frame", "message": f"unknown type: {ftype}"})
                ))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        print(f"[{client_id}] 连接断开", flush=True)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

async def serve(host: str, port: int, chunk_delay: float) -> None:
    async def handler(ws: Any) -> None:
        await handle_connection(ws, chunk_delay)

    async with websockets.serve(handler, host, port):
        print(f"Mock Gateway 已启动: ws://{host}:{port}", flush=True)
        print(f"  chunk 间隔: {chunk_delay}s", flush=True)
        print(f"  支持: call.incoming / message.user / run.cancel / call.hangup / config.update / ping", flush=True)
        print(f"  帧格式与真实 Gateway 完全一致，可直接用 cli_call_client.py 测试\n", flush=True)
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Gateway — 电话平台对接测试")
    parser.add_argument("--host", default="0.0.0.0", help="绑定地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    parser.add_argument("--delay", type=float, default=0.08, help="每个 chunk 间隔秒数（默认 0.08）")
    args = parser.parse_args()
    asyncio.run(serve(args.host, args.port, args.delay))


if __name__ == "__main__":
    main()
