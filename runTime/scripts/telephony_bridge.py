#!/usr/bin/env python3
"""
telephony_bridge.py  -  电话平台(WS) <-> Gateway(WS) 双向桥接

电话平台发给 Bridge 的帧（左侧协议，可按实际调整字段名）：
  {"event": "call.start",  "user_id": "13800138000", "language": "zh-CN", "user_info": "..."}
  {"event": "asr.result",  "user_id": "13800138000", "text": "我想查订单"}
  {"event": "call.hangup", "user_id": "13800138000"}

Bridge 发给电话平台的帧：
  {"event": "call.ready",  "user_id": "...", "session_id": "..."}
  {"event": "tts.speak",   "user_id": "...", "session_id": "...", "text": "Agent 回复"}
  {"event": "call.ended",  "user_id": "..."}
  {"event": "error",       "user_id": "...", "code": "...", "message": "..."}

运行：
  pip install websockets
  python scripts/telephony_bridge.py
  python scripts/telephony_bridge.py --host 0.0.0.0 --port 7000 --gateway ws://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Optional

import websockets

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
try:
    from openclaw_gateway_runtime.infra import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bridge")


# ---------------------------------------------------------------------------
# per-call session
# ---------------------------------------------------------------------------

@dataclass
class CallSession:
    user_id: str
    phone_ws: object          # WS connection from telephony platform  (left)
    gw_ws: object             # WS connection to Gateway               (right)
    session_id: Optional[str] = None
    _p2g_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _g2p_task: Optional[asyncio.Task] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# left -> right:  phone platform frames  ->  Gateway frames
# ---------------------------------------------------------------------------

async def _phone_to_gw(session: CallSession) -> None:
    """
    Translate telephony-platform events to Gateway protocol frames.

    asr.result   ->  message.user
    call.hangup  ->  call.hangup  (then stop)
    """
    try:
        async for raw in session.phone_ws:
            try:
                f = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("[%s] p2g: bad JSON", session.user_id)
                continue

            event = f.get("event", "")

            if event == "asr.result":
                text = str(f.get("text", "")).strip()
                if not text:
                    continue
                log.info("[%s] ASR -> GW: %r", session.user_id, text[:80])
                await session.gw_ws.send(json.dumps({
                    "v": 1, "type": "message.user",
                    "user_id": session.user_id,
                    "session_id": session.session_id,
                    "payload": {"text": text, "modality": "text"},
                }))

            elif event == "call.hangup":
                log.info("[%s] phone hangup -> GW", session.user_id)
                await session.gw_ws.send(json.dumps({
                    "v": 1, "type": "call.hangup",
                    "user_id": session.user_id,
                    "session_id": session.session_id,
                }))
                break  # _g2p_task handles the ack and closes everything

            else:
                log.debug("[%s] phone event ignored: %s", session.user_id, event)

    except Exception as exc:
        log.warning("[%s] p2g ended: %s", session.user_id, exc)


# ---------------------------------------------------------------------------
# right -> left:  Gateway frames  ->  phone platform frames
# ---------------------------------------------------------------------------

async def _gw_to_phone(session: CallSession) -> None:
    """
    Translate Gateway protocol frames to telephony-platform events.

    stream.chunk     ->  accumulate
    run.end          ->  tts.speak  (full reply)
    call.hangup_ack  ->  call.ended  (then stop)
    error            ->  error
    """
    reply_buf: list[str] = []
    try:
        async for raw in session.gw_ws:
            try:
                f = json.loads(raw)
            except json.JSONDecodeError:
                continue

            t = f.get("type", "")

            if t == "stream.chunk":
                chunk = (f.get("payload") or {}).get("text", "")
                if chunk:
                    reply_buf.append(chunk)

            elif t == "run.end":
                full_reply = "".join(reply_buf)
                reply_buf.clear()
                if full_reply:
                    log.info("[%s] TTS -> phone: %r", session.user_id, full_reply[:80])
                    await session.phone_ws.send(json.dumps({
                        "event": "tts.speak",
                        "user_id": session.user_id,
                        "session_id": session.session_id,
                        "text": full_reply,
                    }))

            elif t == "call.hangup_ack":
                log.info("[%s] hangup_ack -> phone", session.user_id)
                await session.phone_ws.send(json.dumps({
                    "event": "call.ended",
                    "user_id": session.user_id,
                }))
                break

            elif t == "error":
                err = f.get("error") or {}
                log.error("[%s] gateway error: %s", session.user_id, err)
                try:
                    await session.phone_ws.send(json.dumps({
                        "event": "error",
                        "user_id": session.user_id,
                        "code": err.get("code", "unknown"),
                        "message": err.get("message", ""),
                    }))
                except Exception:
                    pass

            else:
                log.debug("[%s] gw frame ignored: %s", session.user_id, t)

    except Exception as exc:
        log.warning("[%s] g2p ended: %s", session.user_id, exc)


# ---------------------------------------------------------------------------
# connection handler  (one call = one WebSocket from phone platform)
# ---------------------------------------------------------------------------

async def handle_phone_connection(phone_ws, gateway_url: str) -> None:
    """
    Lifecycle for a single call:
      1. Read call.start from phone platform
      2. Open WS to Gateway, send call.incoming, wait for call.ready
      3. Forward call.ready to phone platform
      4. Run bidirectional bridge until either side closes
      5. Close both WebSocket connections
    """
    peer = getattr(phone_ws, "remote_address", "?")
    log.info("phone connected  peer=%s", peer)

    # ── Step 1: read call.start ───────────────────────────────────────────
    try:
        raw = await asyncio.wait_for(phone_ws.recv(), timeout=15)
        first = json.loads(raw)
    except Exception as exc:
        log.error("failed to read call.start: %s", exc)
        return

    if first.get("event") != "call.start":
        await phone_ws.send(json.dumps({
            "event": "error", "message": "first frame must be call.start",
        }))
        return

    user_id   = str(first.get("user_id") or uuid.uuid4())
    language  = first.get("language", "zh-CN")
    user_info = first.get("user_info", "")
    log.info("[%s] call.start  lang=%s", user_id, language)

    # ── Step 2: connect to Gateway (one WS, stays open for entire call) ───
    try:
        gw_ws = await asyncio.wait_for(websockets.connect(gateway_url), timeout=10)
    except Exception as exc:
        log.error("[%s] cannot connect to gateway: %s", user_id, exc)
        await phone_ws.send(json.dumps({"event": "error", "message": "gateway_unreachable"}))
        return

    await gw_ws.send(json.dumps({
        "v": 1, "type": "call.incoming", "user_id": user_id,
        "payload": {"user_id": user_id, "language": language, "user_info": user_info},
    }))

    try:
        ready_raw = await asyncio.wait_for(gw_ws.recv(), timeout=15)
        ready = json.loads(ready_raw)
    except asyncio.TimeoutError:
        log.error("[%s] gateway call.ready timeout", user_id)
        await phone_ws.send(json.dumps({"event": "error", "message": "gateway_timeout"}))
        await gw_ws.close()
        return

    if ready.get("type") != "call.ready":
        log.error("[%s] unexpected gateway frame: %s", user_id, ready)
        await phone_ws.send(json.dumps({"event": "error", "message": "unexpected_gateway_frame"}))
        await gw_ws.close()
        return

    session_id = ready["session_id"]
    log.info("[%s] call.ready  session_id=%s", user_id, session_id)

    # ── Step 3: notify phone platform ────────────────────────────────────
    await phone_ws.send(json.dumps({
        "event": "call.ready",
        "user_id": user_id,
        "session_id": session_id,
    }))

    # ── Step 4: bidirectional bridge ─────────────────────────────────────
    session = CallSession(
        user_id=user_id, phone_ws=phone_ws, gw_ws=gw_ws, session_id=session_id
    )
    session._p2g_task = asyncio.create_task(_phone_to_gw(session), name=f"p2g-{user_id}")
    session._g2p_task = asyncio.create_task(_gw_to_phone(session), name=f"g2p-{user_id}")

    # Wait for either side to finish, then cancel the other
    done, pending = await asyncio.wait(
        {session._p2g_task, session._g2p_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    # ── Step 5: close both connections ───────────────────────────────────
    for ws in (phone_ws, gw_ws):
        try:
            await ws.close()
        except Exception:
            pass

    log.info("[%s] call ended", user_id)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Telephony Bridge")
    parser.add_argument(
        "--gateway", default=os.environ.get("GATEWAY_WS_URL", "ws://127.0.0.1:8765"),
        help="Gateway WebSocket URL (default: GATEWAY_WS_URL env or ws://127.0.0.1:8765)",
    )
    parser.add_argument("--host", default="0.0.0.0",  help="Bridge listen host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=7000, help="Bridge listen port (default: 7000)")
    args = parser.parse_args()

    async def serve() -> None:
        async def _handler(ws) -> None:
            await handle_phone_connection(ws, args.gateway)

        async with websockets.serve(_handler, args.host, args.port):
            log.info(
                "Telephony Bridge  ws://%s:%s  ->  Gateway %s",
                args.host, args.port, args.gateway,
            )
            await asyncio.Future()

    asyncio.run(serve())


if __name__ == "__main__":
    main()