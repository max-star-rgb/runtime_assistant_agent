from __future__ import annotations

import asyncio
import json
import os
import uuid

from ..infra import configure_logging, load_dotenv
from ..infra.structured_log import log_event
from ..protocol import CALL_INCOMING, CALL_READY, frame
from ..ws import WsEndpoint
from .gateway import GatewayService
from .runtime_manager import RuntimeManager
from .user_config import UserConfig, load_user_config, merge_config


def _gateway_host() -> str:
    return (os.environ.get("GATEWAY_HOST") or "0.0.0.0").strip()


def _gateway_port() -> int:
    try:
        return int((os.environ.get("GATEWAY_PORT") or "8765").strip())
    except ValueError:
        return 8765


async def serve_gateway_ws(
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """
    External WebSocket server: clients connect here.

    Bind address and port are resolved in this order (highest priority first):
      1. Arguments passed directly to this function
      2. Environment variables: GATEWAY_HOST / GATEWAY_PORT
      3. Defaults: 0.0.0.0 / 8765

    Telephony mode (call.incoming first frame):
      - Parse user_id and initial config from call.incoming
      - Load + merge local hot-config
      - RuntimeManager.get_or_create() → in-process RuntimeService endpoint
      - Reply call.ready with assigned session_id
      - Hand off to GatewayService.bridge()

    Legacy mode (session.open / any other first frame):
      - Use session_id from the frame as user_id (or generate one)
      - RuntimeManager.get_or_create() → same in-process RuntimeService
      - Hand off to GatewayService.bridge() (no call.ready sent)
    """
    import websockets

    _host = host if host is not None else _gateway_host()
    _port = port if port is not None else _gateway_port()

    gateway = GatewayService(runtime_manager=RuntimeManager())

    async def handler(client_ws) -> None:
        client_id = str(uuid.uuid4())
        peer = getattr(client_ws, "remote_address", None)
        log_event("gateway", "ws_accept", client_id=client_id, peer=str(peer) if peer is not None else None)
        client_ep = WsEndpoint.wrap(client_ws)

        # --- Peek at the first frame to decide routing mode ---
        first_frame: dict | None = None
        try:
            raw = await asyncio.wait_for(client_ws.recv(), timeout=30)
            first_frame = json.loads(raw)
        except Exception as exc:
            log_event("gateway", "first_frame_error", client_id=client_id, error=str(exc))
            return

        if first_frame and first_frame.get("type") == CALL_INCOMING:
            # ── Telephony mode ──────────────────────────────────────────────
            payload = first_frame.get("payload") or {}
            user_id: str = str(first_frame.get("user_id") or payload.get("user_id") or uuid.uuid4())
            log_event("gateway", "call_incoming", client_id=client_id, user_id=user_id,
                      language=payload.get("language", "zh-CN"),
                      agent_profile=payload.get("agent_profile", "default"))

            incoming_config = {
                "language": payload.get("language", "zh-CN"),
                "user_info": payload.get("user_info", ""),
                "agent_profile": payload.get("agent_profile", "default"),
                "tone": payload.get("tone", "friendly"),
            }

            local_cfg = load_user_config(user_id)
            user_config = merge_config(local_cfg, incoming_config)

            try:
                runtime_ep = await gateway._runtime_manager.get_or_create(user_id, user_config)  # type: ignore[union-attr]
            except RuntimeError as exc:
                code = "runtime_limit_reached" if "runtime_limit_reached" in str(exc) else "runtime_error"
                await client_ep.send(frame(type="error", user_id=user_id, error={"code": code, "message": str(exc)}))
                return

            session_id = str(uuid.uuid4())
            await client_ep.send(frame(type=CALL_READY, user_id=user_id, session_id=session_id))
            log_event("gateway", "call_ready", client_id=client_id, user_id=user_id, session_id=session_id)

            await gateway.bridge(
                client_id=client_id,
                client_ep=client_ep,
                runtime_ep=runtime_ep,
                user_id=user_id,
                session_id=session_id,
            )

        else:
            # ── Legacy mode: also use RuntimeManager (in-process) ──────────
            # Extract session_id from the first frame if present (session.open / message.user)
            temp_user_id = (
                (first_frame.get("payload") or {}).get("session_id")
                or first_frame.get("session_id")
                or str(uuid.uuid4())
            )
            temp_config = UserConfig(user_id=temp_user_id)

            try:
                runtime_ep = await gateway._runtime_manager.get_or_create(temp_user_id, temp_config)  # type: ignore[union-attr]
            except RuntimeError as exc:
                code = "runtime_limit_reached" if "runtime_limit_reached" in str(exc) else "runtime_error"
                await client_ep.send(frame(type="error", error={"code": code, "message": str(exc)}))
                return

            # Re-inject the first frame so GatewayService sees it
            client_ep._inject(first_frame)  # type: ignore[attr-defined]

            await gateway.bridge(
                client_id=client_id,
                client_ep=client_ep,
                runtime_ep=runtime_ep,
                user_id=temp_user_id,
            )

    async with websockets.serve(handler, _host, _port):
        log_event("gateway", "server_start", host=_host, port=_port)
        await asyncio.Future()


def main() -> None:
    import argparse

    load_dotenv()
    configure_logging()

    parser = argparse.ArgumentParser(description="OpenClaw Gateway WebSocket Server")
    parser.add_argument(
        "--host",
        default=None,
        help="Bind host (default: GATEWAY_HOST env or 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default: GATEWAY_PORT env or 8765)",
    )
    args = parser.parse_args()

    asyncio.run(serve_gateway_ws(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
