import os

# unittest discover loads this module as top-level `test_*`; avoid stderr pipe backpressure from JSON logs.
os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")

import asyncio
import unittest

from openclaw_gateway_runtime.gateway import GatewayService
from openclaw_gateway_runtime.protocol import frame
from openclaw_gateway_runtime.runtime import RuntimeService
from openclaw_gateway_runtime.runtime.openclaw_adapter import StubEchoAdapter
from openclaw_gateway_runtime.transport import InMemoryDuplex


class CancelInterruptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.gateway = GatewayService()
        # Deterministic stub; do not inherit ANTHROPIC_/MINIMAX_ keys from the host env.
        self.runtime = RuntimeService(adapter=StubEchoAdapter())

    async def test_cancel_stops_stream_and_emits_cancelled(self) -> None:
        client_ep, gw_client_ep = InMemoryDuplex.create_pair()
        gw_runtime_ep, runtime_ep = InMemoryDuplex.create_pair()

        bridge_task = asyncio.create_task(
            self.gateway.bridge(client_id="c1", client_ep=gw_client_ep, runtime_ep=gw_runtime_ep)
        )
        runtime_task = asyncio.create_task(self.runtime.serve(runtime_ep))

        await client_ep.send(frame(type="message.user", session_id="s1", payload={"text": "abcdef"}))

        started = None
        chunks = 0
        cancelled_end = False

        async for f in client_ep:
            if f["type"] == "run.started":
                started = f["run_id"]
                await client_ep.send(frame(type="run.cancel", session_id="s1", run_id=started))
            elif f["type"] == "stream.chunk":
                chunks += 1
            elif f["type"] == "run.end":
                cancelled_end = f.get("reason") == "cancelled"
                break

        self.assertIsNotNone(started)
        self.assertTrue(cancelled_end)
        self.assertGreaterEqual(chunks, 0)

        await client_ep.close()
        await gw_runtime_ep.close()
        await runtime_ep.close()
        bridge_task.cancel()
        runtime_task.cancel()

    async def test_interrupt_cancels_previous_run_then_starts_new(self) -> None:
        client_ep, gw_client_ep = InMemoryDuplex.create_pair()
        gw_runtime_ep, runtime_ep = InMemoryDuplex.create_pair()

        bridge_task = asyncio.create_task(
            self.gateway.bridge(client_id="c1", client_ep=gw_client_ep, runtime_ep=gw_runtime_ep)
        )
        runtime_task = asyncio.create_task(self.runtime.serve(runtime_ep))

        await client_ep.send(frame(type="message.user", session_id="s1", payload={"text": "first"}))

        first_run = None
        saw_first_cancelled = False
        second_run = None

        async for f in client_ep:
            if f["type"] == "run.started" and first_run is None:
                first_run = f["run_id"]
                # Interrupt with a new message (same session).
                await client_ep.send(frame(type="message.user", session_id="s1", payload={"text": "second"}))
            elif f["type"] == "run.end" and f.get("run_id") == first_run:
                saw_first_cancelled = f.get("reason") == "cancelled"
            elif f["type"] == "run.started" and first_run is not None and second_run is None:
                second_run = f["run_id"]
            elif f["type"] == "run.end" and second_run is not None and f.get("run_id") == second_run:
                self.assertEqual(f.get("reason"), "completed")
                break

        self.assertIsNotNone(first_run)
        self.assertIsNotNone(second_run)
        self.assertNotEqual(first_run, second_run)
        self.assertTrue(saw_first_cancelled)

        await client_ep.close()
        await gw_runtime_ep.close()
        await runtime_ep.close()
        bridge_task.cancel()
        runtime_task.cancel()


if __name__ == "__main__":
    unittest.main()

