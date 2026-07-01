from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, Optional


class SseError(RuntimeError):
    pass


def iter_sse_data_lines(byte_iter: Iterable[bytes]) -> Iterator[str]:
    """
    Parse an SSE byte stream into concatenated `data:` payload strings.
    Returns each event's data as a string (may be JSON).
    """
    buf: list[str] = []
    for raw in byte_iter:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if buf:
                yield "\n".join(buf)
                buf = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buf.append(line[len("data:") :].lstrip())
    if buf:
        yield "\n".join(buf)


@dataclass
class AnthropicStreamBuilder:
    """
    Incrementally builds a Messages API response dict from SSE events.
    Supports text_delta and tool_use input_json_delta (best-effort).
    """

    response: Dict[str, Any] = field(default_factory=lambda: {"content": []})
    _blocks: list[dict[str, Any]] = field(default_factory=list)
    _tool_input_buf: dict[int, str] = field(default_factory=dict)

    def feed(self, event: dict[str, Any]) -> Optional[str]:
        """
        Feed one SSE event object. Returns a text delta if present.
        """
        t = event.get("type")
        if t == "message_start":
            msg = event.get("message") or {}
            if isinstance(msg, dict):
                self.response.update({k: v for k, v in msg.items() if k != "content"})
            return None

        if t == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict):
                self._blocks.append(block)
                self.response["content"] = self._blocks
            return None

        if t == "content_block_delta":
            index = int(event.get("index") or 0)
            delta = event.get("delta") or {}
            if not isinstance(delta, dict):
                return None
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text")
                if isinstance(text, str):
                    # apply to block if present
                    if 0 <= index < len(self._blocks):
                        blk = self._blocks[index]
                        if blk.get("type") == "text":
                            blk["text"] = (blk.get("text") or "") + text
                    return text
            if dtype == "input_json_delta":
                partial = delta.get("partial_json")
                if isinstance(partial, str):
                    self._tool_input_buf[index] = self._tool_input_buf.get(index, "") + partial
            return None

        if t == "content_block_stop":
            index = int(event.get("index") or 0)
            if 0 <= index < len(self._blocks):
                blk = self._blocks[index]
                if blk.get("type") == "tool_use" and index in self._tool_input_buf:
                    raw = self._tool_input_buf.get(index, "")
                    if raw.strip():
                        try:
                            blk["input"] = json.loads(raw)
                        except Exception:
                            blk["input"] = {"_raw": raw}
            return None

        if t == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict):
                # e.g. stop_reason, stop_sequence
                self.response.update(delta)
            return None

        if t == "message_stop":
            return None

        if t == "error":
            raise SseError(str(event))

        return None

