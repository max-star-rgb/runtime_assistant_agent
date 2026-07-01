from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from openclaw_gateway_runtime.runtime.assistant_agent_adapter import (
    AssistantAgentAdapter,
    realtime_event_to_adapter_event,
)
from openclaw_gateway_runtime.runtime.openclaw_adapter import CancelToken


class FakeBackend:
    def __init__(self, *, events=None, result=None, error: Exception | None = None) -> None:
        self.events = list(events or [])
        self.result = result or SimpleNamespace(
            status="completed",
            expects_reply=False,
            metadata={},
        )
        self.error = error
        self.requests = []
        self.cancel_tokens = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        self.cancel_tokens.append(cancel_token)
        if self.error is not None:
            raise self.error
        if event_sink is not None:
            for event in self.events:
                await event_sink(event)
        return self.result


async def _collect(adapter: AssistantAgentAdapter, *, cancel: CancelToken | None = None):
    runtime_cancel = cancel or CancelToken()
    events = []
    async for event in adapter.run(
        session_id="session-1",
        turn_id="turn-1",
        run_id="run-1",
        user_text="hello",
        history=["one", "two"],
        cancel=runtime_cancel,
        user_id="user-1",
    ):
        events.append(event)
    return events


def _event(event_type: str, *, text=None, payload=None, display_only=False):
    return SimpleNamespace(
        type=event_type,
        text=text,
        payload=payload or {},
        display_only=display_only,
        content_type="text",
    )


def test_fake_backend_receives_realtime_request_mapping() -> None:
    backend = FakeBackend()
    adapter = AssistantAgentAdapter(backend=backend)

    asyncio.run(_collect(adapter))

    request = backend.requests[0]
    assert request.user_id == "user-1"
    assert request.session_id == "session-1"
    assert request.run_id == "run-1"
    assert request.turn_id == "turn-1"
    assert request.text == "hello"
    assert request.metadata == {"runtime": {"history": ["one", "two"]}}


def test_response_chunk_maps_to_stream_chunk() -> None:
    backend = FakeBackend(
        events=[
            _event(
                "response.chunk",
                text="hello",
                payload={"chunk_index": 0},
            )
        ]
    )
    adapter = AssistantAgentAdapter(backend=backend)

    events = asyncio.run(_collect(adapter))

    assert events[0].type == "stream.chunk"
    assert events[0].payload["text"] == "hello"
    assert events[0].payload["display_only"] is False
    assert events[0].payload["realtime"] == {"chunk_index": 0}


def test_tool_events_map_to_event_tool() -> None:
    backend = FakeBackend(
        events=[
            _event("tool.started", payload={"tool_name": "search"}),
            _event("tool.finished", payload={"tool_name": "search", "output_ref": "mock://result"}),
            _event("tool.failed", payload={"tool_name": "search", "error": {"message": "failed"}}),
        ]
    )
    adapter = AssistantAgentAdapter(backend=backend)

    events = asyncio.run(_collect(adapter))
    tool_events = [event for event in events if event.type == "event.tool"]

    assert [event.payload["phase"] for event in tool_events] == ["start", "result", "error"]
    assert [event.payload["name"] for event in tool_events] == ["search", "search", "search"]
    assert tool_events[1].payload["success"] is True
    assert tool_events[2].payload["success"] is False


def test_trace_events_map_to_event_trace() -> None:
    backend = FakeBackend(
        events=[
            _event("trace.decision", payload={"decision_trace": {"event": "decision"}}),
            _event("trace.observation", payload={"decision_trace": {"event": "observation"}}),
        ]
    )
    adapter = AssistantAgentAdapter(backend=backend)

    events = asyncio.run(_collect(adapter))
    trace_events = [event for event in events if event.type == "event.trace"]

    assert [event.payload["phase"] for event in trace_events] == ["decision", "observation"]
    assert all(event.payload["display_only"] is True for event in trace_events)


def test_cancel_token_is_bridged_to_backend() -> None:
    runtime_cancel = CancelToken()

    class CancelAwareBackend(FakeBackend):
        async def run_turn(self, request, *, event_sink=None, cancel_token=None):
            self.requests.append(request)
            self.cancel_tokens.append(cancel_token)
            assert cancel_token is not None
            assert cancel_token.is_cancelled() is False
            runtime_cancel.cancel()
            assert cancel_token.is_cancelled() is True
            return self.result

    backend = CancelAwareBackend()
    adapter = AssistantAgentAdapter(backend=backend)

    asyncio.run(_collect(adapter, cancel=runtime_cancel))

    assert backend.cancel_tokens


def test_result_expects_reply_is_returned_as_meta_event() -> None:
    backend = FakeBackend(
        result=SimpleNamespace(status="completed", expects_reply=True, metadata={})
    )
    adapter = AssistantAgentAdapter(backend=backend)

    events = asyncio.run(_collect(adapter))

    assert events[-1].type == "_meta"
    assert events[-1].meta == {"expects_reply": True}


def test_error_event_maps_to_event_error_without_duplicate_result_error() -> None:
    backend = FakeBackend(
        events=[_event("error", text="backend said no", payload={"error_type": "BackendError"})],
        result=SimpleNamespace(status="error", expects_reply=True, metadata={}),
    )
    adapter = AssistantAgentAdapter(backend=backend)

    events = asyncio.run(_collect(adapter))
    error_events = [event for event in events if event.type == "event.error"]

    assert len(error_events) == 1
    assert error_events[0].payload["message"] == "backend said no"
    assert error_events[0].payload["error_type"] == "BackendError"


def test_backend_error_result_maps_to_event_error() -> None:
    backend = FakeBackend(
        result=SimpleNamespace(
            status="error",
            expects_reply=True,
            metadata={"error_type": "RuntimeError", "error_message": "boom"},
        )
    )
    adapter = AssistantAgentAdapter(backend=backend)

    events = asyncio.run(_collect(adapter))
    error_events = [event for event in events if event.type == "event.error"]

    assert len(error_events) == 1
    assert error_events[0].payload["message"] == "boom"
    assert error_events[0].payload["error_type"] == "RuntimeError"


def test_backend_exception_maps_to_event_error() -> None:
    backend = FakeBackend(error=RuntimeError("exploded"))
    adapter = AssistantAgentAdapter(backend=backend)

    events = asyncio.run(_collect(adapter))

    assert events[0].type == "event.error"
    assert events[0].payload["message"] == "exploded"
    assert events[0].payload["error_type"] == "RuntimeError"
    assert events[-1].type == "_meta"
    assert events[-1].meta == {"expects_reply": True}


def test_response_final_is_not_streamed_as_duplicate_text() -> None:
    assert realtime_event_to_adapter_event(_event("response.final", text="final text")) is None


def test_adapter_does_not_import_openclaw_anthropic_loop() -> None:
    source = Path("src/openclaw_gateway_runtime/runtime/assistant_agent_adapter.py").read_text(
        encoding="utf-8"
    )

    assert "AnthropicSkillsAdapter" not in source
    assert "AnthropicAgentRuntime" not in source
    assert "anthropic_runtime" not in source
    assert "tool_use" not in source
