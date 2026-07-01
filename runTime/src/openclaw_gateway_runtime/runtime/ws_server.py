from __future__ import annotations

import asyncio
from typing import Optional

import websockets

from ..infra import configure_logging, load_dotenv
from ..infra.structured_log import log_event
from ..ws import WsEndpoint
from .runtime import RuntimeService


async def serve_runtime_ws(*, host: str = "127.0.0.1", port: int = 8766) -> None:
    """
    Runtime WebSocket server (internal): Gateway connects here.
    """

    runtime = RuntimeService()

    async def handler(ws) -> None:
        peer = getattr(ws, "remote_address", None)
        log_event("runtime", "ws_accept", peer=str(peer) if peer is not None else None)
        ep = WsEndpoint.wrap(ws)
        await runtime.serve(ep)  # type: ignore[arg-type]

    async with websockets.serve(handler, host, port):
        await asyncio.Future()


def main() -> None:
    # Load .env (if present) before constructing RuntimeService/clients.
    load_dotenv()
    configure_logging()
    asyncio.run(serve_runtime_ws())


if __name__ == "__main__":
    main()

