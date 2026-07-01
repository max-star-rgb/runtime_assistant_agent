from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..infra.metrics import gateway_metrics
from ..infra.structured_log import log_event
from ..protocol import (
    CALL_HANGUP,
    CALL_HANGUP_ACK,
    CALL_INCOMING,
    CALL_READY,
    CONFIG_UPDATE,
    SUPPORTED_MODALITIES,
    Frame,
    frame,
)
from ..transport import Endpoint

if TYPE_CHECKING:
    from .runtime_manager import RuntimeManager


def _forward_event_to_external_client() -> bool:
    """
    Controls whether event.skill and event.tool frames are forwarded to external clients.
    Default off — set GATEWAY_FORWARD_EVENT_TOOL=1 in .env to enable (useful for debugging).
    """
    return (os.environ.get("GATEWAY_FORWARD_EVENT_TOOL") or "0").strip() == "1"


def _should_send_runtime_frame_to_client(f: dict[str, Any]) -> bool:
    ftype = f.get("type", "")
    if ftype.startswith("_"):
        return False  # internal frames (e.g. _evict) are never forwarded
    if ftype in ("event.skill", "event.tool") and not _forward_event_to_external_client():
        return False
    # Suppress internal cleanup errors (e.g. run_not_found from stale cancel)
    if ftype == "error":
        err = f.get("error") or {}
        if err.get("code") == "run_not_found":
            return False
    return True


@dataclass
class ClientConn:
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    active_run_id: Optional[str] = None
    _cancel_event: Optional[asyncio.Event] = None  # set to signal bridge should stop


class GatewayService:
    """
    Gateway semantics (transport-agnostic):
    - accepts external frames from a "client endpoint"
    - forwards to runtime endpoint
    - forwards runtime events back to client (with user_id attached)
    - tracks active run per connection to support "cancel current run" on disconnect
    - handles call.incoming / call.hangup / config.update for telephony scenario
    """

    def __init__(self, *, runtime_manager: Optional["RuntimeManager"] = None) -> None:
        self._clients: Dict[str, ClientConn] = {}
        self._lock = asyncio.Lock()
        self._runtime_manager = runtime_manager

    async def bridge(
        self,
        *,
        client_id: str,
        client_ep: Endpoint,
        runtime_ep: Endpoint,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        snap = gateway_metrics.on_connection_open()
        log_event("gateway", "connection_open", client_id=client_id, user_id=user_id, **snap)

        # Evict stale bridges for the same user — only one bridge should read
        # from the shared runtime_ep at a time.
        cancel_event = asyncio.Event()
        async with self._lock:
            if user_id:
                for cid, conn in list(self._clients.items()):
                    if conn.user_id == user_id and cid != client_id and conn._cancel_event:
                        conn._cancel_event.set()
                        log_event("gateway", "evict_stale_bridge", client_id=cid, user_id=user_id)
            self._clients[client_id] = ClientConn(
                user_id=user_id, session_id=session_id, _cancel_event=cancel_event
            )
        # Send a wake-up frame into the runtime_ep read queue so the old bridge's
        # _runtime_to_client loop unblocks from queue.get() and sees cancel_event.
        if user_id:
            try:
                runtime_ep._inject(frame(type="_evict", user_id=user_id))  # type: ignore[attr-defined]
            except Exception:
                pass

        _disconnect_reason = "normal"  # hangup | connection_lost | normal

        async def _client_to_runtime() -> None:
            nonlocal _disconnect_reason
            try:
                async for f in client_ep:
                    t = f.get("type")
                    uid = f.get("user_id") or user_id

                    if t == CALL_INCOMING:
                        log_event("gateway", "call_incoming_relay", client_id=client_id, user_id=uid)
                        continue

                    elif t == CALL_HANGUP:
                        await client_ep.send(frame(type=CALL_HANGUP_ACK, user_id=uid))
                        log_event("gateway", "call_hangup", client_id=client_id, user_id=uid)
                        if self._runtime_manager and uid:
                            asyncio.create_task(self._runtime_manager.mark_hangup(uid))
                        _disconnect_reason = "hangup"
                        break

                    elif t == CONFIG_UPDATE:
                        payload = f.get("payload") or {}
                        key = str(payload.get("key") or "")
                        value = str(payload.get("value") or "")
                        target_uid = str(uid or "")
                        if self._runtime_manager and key and target_uid:
                            asyncio.create_task(
                                self._runtime_manager.inject_config(target_uid, key, value)
                            )
                        log_event("gateway", "config_update", client_id=client_id, user_id=target_uid, key=key)

                    elif t in {"session.open", "session.resume"}:
                        sid = (f.get("payload") or {}).get("session_id") or f.get("session_id")
                        async with self._lock:
                            self._clients[client_id].session_id = sid
                        await runtime_ep.send(frame(type=t, session_id=sid, user_id=uid, payload=f.get("payload")))

                    elif t == "message.user":
                        sid = f.get("session_id") or (f.get("payload") or {}).get("session_id")
                        text = ((f.get("payload") or {}).get("text") or "")[:200]
                        log_event("gateway", "message_user", client_id=client_id, user_id=uid,
                                  session_id=sid, text_preview=text)
                        modality = ((f.get("payload") or {}).get("modality") or "text")
                        if modality not in SUPPORTED_MODALITIES:
                            await client_ep.send(frame(
                                type="error",
                                user_id=uid,
                                error={"code": "unsupported_modality", "message": f"{modality} modality not yet supported"},
                            ))
                            continue
                        enriched = dict(f)
                        if uid:
                            enriched["user_id"] = uid
                        await runtime_ep.send(enriched)  # type: ignore[arg-type]
                        async with self._lock:
                            conn = self._clients.get(client_id)
                            if conn:
                                conn.session_id = sid or conn.session_id

                    elif t == "run.cancel":
                        await runtime_ep.send(f)

                    elif t == "ping":
                        await client_ep.send(frame(type="pong", user_id=uid))

                    else:
                        await client_ep.send(frame(
                            type="error",
                            user_id=uid,
                            error={"code": "unknown_frame", "message": f"unknown type: {t}"},
                        ))
            except Exception:
                # Client WebSocket dropped without close frame (signal loss, process killed, etc.)
                _disconnect_reason = "connection_lost"

        async def _runtime_to_client() -> None:
            try:
                async for f in runtime_ep:
                    # If this bridge was evicted (new connection for same user), stop.
                    if cancel_event.is_set():
                        return
                    if f.get("type") == "run.started":
                        async with self._lock:
                            conn = self._clients.get(client_id)
                            if conn:
                                conn.active_run_id = f.get("run_id")
                                if f.get("session_id"):
                                    conn.session_id = f.get("session_id")
                    if f.get("type") == "run.end":
                        payload = f.get("payload") or {}
                        log_event("gateway", "run_end", client_id=client_id, user_id=user_id,
                                  run_id=f.get("run_id"), reason=f.get("reason"),
                                  expects_reply=payload.get("expects_reply"))
                        async with self._lock:
                            conn = self._clients.get(client_id)
                            if conn and conn.active_run_id == f.get("run_id"):
                                conn.active_run_id = None
                    if _should_send_runtime_frame_to_client(f):
                        if user_id and not f.get("user_id"):
                            enriched = dict(f)
                            enriched["user_id"] = user_id
                            await client_ep.send(enriched)  # type: ignore[arg-type]
                        else:
                            await client_ep.send(f)
                        # Throttle stream.chunk delivery to avoid overwhelming the client
                        if f.get("type") == "stream.chunk":
                            p = f.get("payload") or {}
                            text = p.get("text") or ""
                            ctype = p.get("content_type", "text")
                            log_event("gateway", "chunk_sent", client_id=client_id,
                                      user_id=user_id, text_preview=text, content_type=ctype)
                            await asyncio.sleep(0.01)
            except Exception:
                # Client disconnected — normal for mobile clients.
                pass

        t1 = asyncio.create_task(_client_to_runtime())
        t2 = asyncio.create_task(_runtime_to_client())

        try:
            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()

            # On disconnect: cancel active run for this client (best-effort)
            # But NOT if this bridge was evicted by a newer connection for the same user.
            async with self._lock:
                conn = self._clients.get(client_id)
                run_id = conn.active_run_id if conn else None
                sid = conn.session_id if conn else None
                self._clients.pop(client_id, None)

            if not cancel_event.is_set() and (run_id or sid):
                await runtime_ep.send(frame(type="run.cancel", run_id=run_id, session_id=sid, user_id=user_id))
        finally:
            snap = gateway_metrics.on_connection_close()
            _log_level = "error" if _disconnect_reason == "connection_lost" else "info"
            log_event("gateway", "connection_close", client_id=client_id, user_id=user_id,
                      reason=_disconnect_reason, level=_log_level, **snap)

