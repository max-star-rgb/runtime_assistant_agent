from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")

import openclaw_gateway_runtime.runtime.assistant_agent_adapter as assistant_agent_module
from openclaw_gateway_runtime.protocol import frame
from openclaw_gateway_runtime.runtime import RuntimeService
from openclaw_gateway_runtime.runtime.assistant_agent_adapter import AssistantAgentAdapter
from openclaw_gateway_runtime.runtime.openclaw_adapter import StubEchoAdapter
from openclaw_gateway_runtime.transport import InMemoryDuplex


@contextmanager
def _runtime_adapter_env(**updates: str):
    keys = ("OPENCLAW_RUNTIME_ADAPTER", "LLM_PROVIDER", "OPENCLAW_REPO_PATH")
    old_values = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update(updates)
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _realtime_event(event_type: str, *, text: str = "", payload=None):
    return SimpleNamespace(
        type=event_type,
        text=text,
        payload=payload or {},
        display_only=False,
        content_type="text",
    )


class FakeRealtimeBackend:
    def __init__(self, *, events=None) -> None:
        self.events = list(events or [])
        self.requests = []
        self.cancel_tokens = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        self.cancel_tokens.append(cancel_token)
        if event_sink is not None:
            for event in self.events:
                await event_sink(event)
        return SimpleNamespace(status="completed", expects_reply=False, metadata={})


class CancelAwareRealtimeBackend:
    def __init__(self) -> None:
        self.requests = []
        self.cancel_tokens = []
        self.cancelled_seen = asyncio.Event()

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        self.cancel_tokens.append(cancel_token)
        assert cancel_token is not None
        if event_sink is not None:
            await event_sink(
                _realtime_event("response.chunk", text="assistant-before-cancel")
            )
        await cancel_token.cancelled()
        self.cancelled_seen.set()
        return SimpleNamespace(status="completed", expects_reply=False, metadata={})


async def _collect_until_run_end(client_ep, *, timeout_s: float = 2.0):
    frames = []

    async def _read():
        async for received in client_ep:
            frames.append(received)
            if received["type"] == "run.end":
                return frames
        raise AssertionError("endpoint closed before run.end")

    return await asyncio.wait_for(_read(), timeout=timeout_s)


async def _run_message_user_with_backend(backend):
    with _runtime_adapter_env(OPENCLAW_RUNTIME_ADAPTER="assistant_agent"), patch.object(
        assistant_agent_module,
        "_create_default_backend",
        return_value=backend,
    ):
        runtime = RuntimeService()
        client_ep, runtime_ep = InMemoryDuplex.create_pair()
        runtime_task = asyncio.create_task(runtime.serve(runtime_ep))
        try:
            await client_ep.send(
                frame(type="message.user", session_id="s1", payload={"text": "hello"})
            )
            frames = await _collect_until_run_end(client_ep)
            return frames, backend, runtime._adapter
        finally:
            await client_ep.close()
            await runtime_ep.close()
            runtime_task.cancel()
            await asyncio.gather(runtime_task, return_exceptions=True)


async def _run_cancel_with_backend(backend):
    with _runtime_adapter_env(OPENCLAW_RUNTIME_ADAPTER="assistant_agent"), patch.object(
        assistant_agent_module,
        "_create_default_backend",
        return_value=backend,
    ):
        runtime = RuntimeService()
        client_ep, runtime_ep = InMemoryDuplex.create_pair()
        runtime_task = asyncio.create_task(runtime.serve(runtime_ep))
        frames = []

        async def _read_and_cancel():
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="s1",
                            run_id=received["run_id"],
                        )
                    )
                if received["type"] == "run.end":
                    return frames
            raise AssertionError("endpoint closed before run.end")

        try:
            await client_ep.send(
                frame(type="message.user", session_id="s1", payload={"text": "cancel me"})
            )
            frames = await asyncio.wait_for(_read_and_cancel(), timeout=2.0)
            await asyncio.wait_for(backend.cancelled_seen.wait(), timeout=1.0)
            return frames, backend, runtime._adapter
        finally:
            await client_ep.close()
            await runtime_ep.close()
            runtime_task.cancel()
            await asyncio.gather(runtime_task, return_exceptions=True)


def test_default_config_still_uses_stub_adapter() -> None:
    with _runtime_adapter_env():
        runtime = RuntimeService()

    assert isinstance(runtime._adapter, StubEchoAdapter)


def test_configured_assistant_agent_uses_assistant_agent_adapter() -> None:
    with _runtime_adapter_env(OPENCLAW_RUNTIME_ADAPTER="assistant_agent"):
        runtime = RuntimeService()

    assert isinstance(runtime._adapter, AssistantAgentAdapter)


def test_message_user_streams_through_assistant_agent_adapter() -> None:
    backend = FakeRealtimeBackend(
        events=[
            _realtime_event(
                "response.chunk",
                text="assistant stream",
                payload={"chunk_index": 0},
            )
        ]
    )

    frames, used_backend, adapter = asyncio.run(_run_message_user_with_backend(backend))

    assert isinstance(adapter, AssistantAgentAdapter)
    assert used_backend.requests
    assert [received["type"] for received in frames] == [
        "run.started",
        "stream.chunk",
        "run.end",
    ]
    chunk = frames[1]["payload"]
    assert chunk["text"] == "assistant stream"
    assert chunk["realtime"] == {"chunk_index": 0}
    assert frames[-1]["reason"] == "completed"
    assert frames[-1]["payload"]["expects_reply"] is False


def test_cancel_uses_assistant_agent_adapter_without_fallback() -> None:
    backend = CancelAwareRealtimeBackend()

    frames, used_backend, adapter = asyncio.run(_run_cancel_with_backend(backend))

    assert isinstance(adapter, AssistantAgentAdapter)
    assert used_backend.requests
    assert used_backend.cancelled_seen.is_set()
    assert frames[-1]["type"] == "run.end"
    assert frames[-1]["reason"] == "cancelled"
    assert frames[-1]["payload"]["expects_reply"] is True
