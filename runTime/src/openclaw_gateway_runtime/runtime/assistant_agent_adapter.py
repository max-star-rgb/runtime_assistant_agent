from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .openclaw_adapter import AdapterEvent, CancelToken, OpenClawAdapter


RealtimeEventSink = Callable[[Any], Awaitable[None]]


class RealtimeBackendLike(Protocol):
    async def run_turn(
        self,
        request: Any,
        *,
        event_sink: RealtimeEventSink | None = None,
        cancel_token: Any | None = None,
    ) -> Any:
        ...


@dataclass
class _FallbackRealtimeAgentRequest:
    user_id: str
    session_id: str
    run_id: str | None = None
    turn_id: str | None = None
    text: str = ""
    image_ids: list[str] = field(default_factory=list)
    video_ids: list[str] = field(default_factory=list)
    audio_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class RuntimeCancelBridge:
    """Expose runTime cancellation through assistant_agent's cancel token shape."""

    def __init__(self, cancel: CancelToken) -> None:
        self._cancel = cancel

    def is_cancelled(self) -> bool:
        return self._cancel.is_cancelled()

    async def cancelled(self) -> None:
        await self._cancel.cancelled()


class AssistantAgentAdapter(OpenClawAdapter):
    """Bridge runTime's adapter interface to assistant_agent's realtime backend."""

    def __init__(
        self,
        *,
        backend: RealtimeBackendLike | None = None,
        backend_factory: Callable[[], RealtimeBackendLike] | None = None,
        request_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._backend = backend
        self._backend_factory = backend_factory
        self._request_factory = request_factory

    async def run(
        self,
        *,
        session_id: str,
        turn_id: str,
        run_id: str,
        user_text: str,
        history: list[str],
        cancel: CancelToken,
        user_id: str = "default",
    ) -> AsyncIterator[AdapterEvent]:
        request = self._build_request(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            user_text=user_text,
            history=history,
        )
        queue: asyncio.Queue[AdapterEvent] = asyncio.Queue()
        error_emitted = False

        async def event_sink(event: Any) -> None:
            mapped = realtime_event_to_adapter_event(event)
            if mapped is not None:
                await queue.put(mapped)

        backend = self._resolve_backend()
        task = asyncio.create_task(
            backend.run_turn(
                request,
                event_sink=event_sink,
                cancel_token=RuntimeCancelBridge(cancel),
            )
        )

        try:
            while not task.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if event.type == "event.error":
                    error_emitted = True
                yield event

            result = await task
        except Exception as exc:  # noqa: BLE001 - adapter boundary converts backend errors.
            yield AdapterEvent(
                "event.error",
                {
                    "message": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            yield AdapterEvent("_meta", None, meta={"expects_reply": True})
            return

        status = str(getattr(result, "status", "completed"))
        if status == "error" and not error_emitted:
            metadata = _as_dict(getattr(result, "metadata", None))
            yield AdapterEvent(
                "event.error",
                {
                    "message": metadata.get("error_message") or "assistant_agent backend error",
                    "error_type": metadata.get("error_type"),
                    "metadata": metadata,
                },
            )

        yield AdapterEvent(
            "_meta",
            None,
            meta={"expects_reply": bool(getattr(result, "expects_reply", False))},
        )

    def _resolve_backend(self) -> RealtimeBackendLike:
        if self._backend is not None:
            return self._backend
        if self._backend_factory is not None:
            self._backend = self._backend_factory()
            return self._backend
        self._backend = _create_default_backend()
        return self._backend

    def _build_request(
        self,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        turn_id: str,
        user_text: str,
        history: list[str],
    ) -> Any:
        request_factory = self._request_factory or _load_realtime_request_class()
        return request_factory(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            text=user_text,
            metadata={"runtime": {"history": list(history)}},
        )


def realtime_event_to_adapter_event(event: Any) -> AdapterEvent | None:
    event_type = str(getattr(event, "type", ""))
    payload = _as_dict(getattr(event, "payload", None))
    text = getattr(event, "text", None)
    display_only = bool(getattr(event, "display_only", False))
    content_type = str(getattr(event, "content_type", "text") or "text")

    if event_type == "response.chunk":
        return AdapterEvent(
            "stream.chunk",
            {
                "text": "" if text is None else str(text),
                "display_only": display_only,
                "content_type": content_type,
                "realtime": payload,
            },
        )
    if event_type in {"tool.started", "tool.finished", "tool.failed"}:
        return AdapterEvent("event.tool", _tool_payload(event_type, payload, text))
    if event_type in {"trace.decision", "trace.observation"}:
        phase = event_type.removeprefix("trace.")
        return AdapterEvent(
            "event.trace",
            {
                **payload,
                "phase": phase,
                "text": text,
                "display_only": True,
            },
        )
    if event_type == "error":
        return AdapterEvent("event.error", _error_payload(payload, text))
    if event_type == "response.final":
        return None
    return None


def _tool_payload(event_type: str, payload: dict[str, Any], text: Any) -> dict[str, Any]:
    phase_by_type = {
        "tool.started": "start",
        "tool.finished": "result",
        "tool.failed": "error",
    }
    mapped = dict(payload)
    mapped["phase"] = phase_by_type[event_type]
    mapped["name"] = mapped.get("tool_name") or mapped.get("name")
    if event_type == "tool.finished":
        mapped.setdefault("success", True)
    if event_type == "tool.failed":
        mapped.setdefault("success", False)
    if text is not None:
        mapped["text"] = text
    return mapped


def _error_payload(payload: dict[str, Any], text: Any) -> dict[str, Any]:
    mapped = dict(payload)
    if text is not None:
        mapped["message"] = str(text)
    mapped.setdefault("message", "assistant_agent error")
    return mapped


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _load_realtime_request_class() -> Callable[..., Any]:
    try:
        from assistant_agent.realtime import RealtimeAgentRequest
    except ImportError:
        return _FallbackRealtimeAgentRequest
    return RealtimeAgentRequest


def _create_default_backend() -> RealtimeBackendLike:
    try:
        from assistant_agent.realtime import AgentGraphRealtimeBackend
    except ImportError as exc:
        raise RuntimeError(
            "assistant_agent is not importable; inject a realtime backend or install "
            "assistant_agent on PYTHONPATH."
        ) from exc
    return AgentGraphRealtimeBackend()
