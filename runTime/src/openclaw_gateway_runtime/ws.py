from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from .protocol import Frame


class WsProtocolError(Exception):
    pass


def dumps_frame(f: Frame) -> str:
    return json.dumps(f, ensure_ascii=False, separators=(",", ":"))


def loads_frame(s: str) -> Frame:
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        raise WsProtocolError(f"invalid json: {e}") from e
    if not isinstance(obj, dict):
        raise WsProtocolError("frame must be a JSON object")
    # Narrow to our TypedDict shape at runtime.
    return obj  # type: ignore[return-value]


@dataclass
class WsEndpoint:
    """
    Minimal adapter to present a WebSocket as an Endpoint-like interface:
    - send(Frame)
    - async iteration yielding Frame
    """

    _ws: Any
    _send_lock: asyncio.Lock
    _pending: "asyncio.Queue[Optional[Frame]]"

    @classmethod
    def wrap(cls, ws: Any) -> "WsEndpoint":
        return cls(_ws=ws, _send_lock=asyncio.Lock(), _pending=asyncio.Queue())

    def _inject(self, f: "Frame") -> None:
        """Pre-pend a frame to be returned before reading from the WebSocket."""
        self._pending.put_nowait(f)

    async def send(self, f: Frame) -> None:
        msg = dumps_frame(f)
        async with self._send_lock:
            await self._ws.send(msg)

    async def __aiter__(self) -> AsyncIterator[Frame]:
        # Drain pre-injected frames first
        while not self._pending.empty():
            item = self._pending.get_nowait()
            if item is not None:
                yield item
        async for msg in self._ws:
            if not isinstance(msg, str):
                raise WsProtocolError("only text frames are supported")
            yield loads_frame(msg)

