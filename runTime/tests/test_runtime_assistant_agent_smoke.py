from __future__ import annotations

import asyncio
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSISTANT_AGENT_SRC = PROJECT_ROOT / "assistant_agent" / "src"
if ASSISTANT_AGENT_SRC.exists():
    sys.path.insert(0, str(ASSISTANT_AGENT_SRC))


def _install_assistant_run_service_stub() -> None:
    """Avoid importing provider/graph dependencies; tests inject run_request."""

    module_name = "assistant_agent.services.assistant_run_service"
    if module_name in sys.modules:
        return

    module = types.ModuleType(module_name)

    def _unused_run_assistant_request(*args, **kwargs):
        raise AssertionError("smoke tests must inject run_request")

    module.run_assistant_request = _unused_run_assistant_request
    sys.modules[module_name] = module


_install_assistant_run_service_stub()

from assistant_agent.realtime.agent_graph_backend import AgentGraphRealtimeBackend
from assistant_agent.schemas.requests import AgentResponse
from openclaw_gateway_runtime.protocol import frame
from openclaw_gateway_runtime.runtime import RuntimeService
from openclaw_gateway_runtime.runtime.assistant_agent_adapter import AssistantAgentAdapter
from openclaw_gateway_runtime.transport import InMemoryDuplex


class RecordingAgentGraphRealtimeBackend(AgentGraphRealtimeBackend):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.run_turn_requests = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.run_turn_requests.append(request)
        return await super().run_turn(
            request,
            event_sink=event_sink,
            cancel_token=cancel_token,
        )


def _artifacts_for(user_request, *, message: str):
    return SimpleNamespace(
        state=SimpleNamespace(
            run_id="assistant-run-1",
            trace_id="trace-1",
            session_id=user_request.session_id,
            status="completed",
            response=AgentResponse(message=message),
        ),
        events=[],
    )


async def _close_runtime(client_ep, runtime_ep, runtime_task) -> None:
    await client_ep.close()
    await runtime_ep.close()
    runtime_task.cancel()
    await asyncio.gather(runtime_task, return_exceptions=True)


async def _collect_until_run_end(client_ep, *, timeout_s: float = 3.0):
    frames = []

    async def _read():
        async for received in client_ep:
            frames.append(received)
            if received["type"] == "run.end":
                return frames
        raise AssertionError("endpoint closed before run.end")

    return await asyncio.wait_for(_read(), timeout=timeout_s)


async def _wait_for_runtime_cancel(runtime: RuntimeService, session_id: str) -> None:
    deadline = asyncio.get_running_loop().time() + 2.0
    while asyncio.get_running_loop().time() < deadline:
        async with runtime._lock:
            active = runtime._active_by_session.get(session_id)
            if active is not None and active.cancel.is_cancelled():
                return
        await asyncio.sleep(0.01)
    raise AssertionError("runtime cancel token was not set")


class RuntimeAssistantAgentSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_user_streams_via_agent_graph_realtime_backend(self) -> None:
        run_request_calls = []

        def fake_run_assistant_request(
            user_request,
            *,
            event_sink=None,
            load_env=True,
            enable_conversation_history=True,
        ):
            run_request_calls.append(
                {
                    "request": user_request,
                    "event_sink": event_sink,
                    "load_env": load_env,
                    "enable_conversation_history": enable_conversation_history,
                }
            )
            return _artifacts_for(user_request, message="assistant smoke response")

        backend = RecordingAgentGraphRealtimeBackend(
            run_request=fake_run_assistant_request,
            load_env=False,
            enable_conversation_history=False,
        )
        runtime = RuntimeService(adapter=AssistantAgentAdapter(backend=backend))
        client_ep, runtime_ep = InMemoryDuplex.create_pair()
        runtime_task = asyncio.create_task(runtime.serve(runtime_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="smoke-session",
                    user_id="smoke-user",
                    payload={"text": "hello realtime", "turn_id": "turn-1"},
                )
            )

            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_runtime(client_ep, runtime_ep, runtime_task)

        frame_types = [received["type"] for received in frames]
        self.assertEqual(frame_types[0], "run.started")
        self.assertIn("stream.chunk", frame_types)
        self.assertEqual(frame_types[-1], "run.end")
        self.assertEqual(frames[-1]["reason"], "completed")

        stream_chunks = [
            received["payload"]["text"]
            for received in frames
            if received["type"] == "stream.chunk"
        ]
        self.assertEqual(stream_chunks, ["assistant smoke response"])

        self.assertEqual(len(backend.run_turn_requests), 1)
        self.assertEqual(backend.run_turn_requests[0].text, "hello realtime")
        self.assertEqual(len(run_request_calls), 1)
        self.assertEqual(run_request_calls[0]["request"].text, "hello realtime")
        self.assertFalse(run_request_calls[0]["load_env"])
        self.assertFalse(run_request_calls[0]["enable_conversation_history"])

    async def test_cancel_preserves_runtime_cancelled_run_end(self) -> None:
        run_request_started = threading.Event()
        release_run_request = threading.Event()
        run_request_calls = []

        def fake_run_assistant_request(
            user_request,
            *,
            event_sink=None,
            load_env=True,
            enable_conversation_history=True,
        ):
            run_request_calls.append(user_request)
            run_request_started.set()
            if not release_run_request.wait(timeout=3.0):
                raise TimeoutError("test did not release fake run_request")
            return _artifacts_for(user_request, message="should not stream after cancel")

        backend = RecordingAgentGraphRealtimeBackend(
            run_request=fake_run_assistant_request,
            load_env=False,
            enable_conversation_history=False,
        )
        runtime = RuntimeService(adapter=AssistantAgentAdapter(backend=backend))
        client_ep, runtime_ep = InMemoryDuplex.create_pair()
        runtime_task = asyncio.create_task(runtime.serve(runtime_ep))
        frames = []

        async def _read_cancel_flow():
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    started = await asyncio.wait_for(
                        asyncio.to_thread(run_request_started.wait),
                        timeout=2.0,
                    )
                    self.assertTrue(started)
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="cancel-session",
                            run_id=received["run_id"],
                        )
                    )
                    await _wait_for_runtime_cancel(runtime, "cancel-session")
                    release_run_request.set()
                if received["type"] == "run.end":
                    return frames
            raise AssertionError("endpoint closed before run.end")

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="cancel-session",
                    user_id="smoke-user",
                    payload={"text": "cancel realtime", "turn_id": "turn-cancel"},
                )
            )
            frames = await asyncio.wait_for(_read_cancel_flow(), timeout=4.0)
        finally:
            release_run_request.set()
            await _close_runtime(client_ep, runtime_ep, runtime_task)

        self.assertEqual(frames[0]["type"], "run.started")
        self.assertEqual(frames[-1]["type"], "run.end")
        self.assertEqual(frames[-1]["reason"], "cancelled")
        self.assertEqual(len(backend.run_turn_requests), 1)
        self.assertEqual(backend.run_turn_requests[0].text, "cancel realtime")
        self.assertEqual(len(run_request_calls), 1)


if __name__ == "__main__":
    unittest.main()
