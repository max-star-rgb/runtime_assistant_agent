import os

os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")

import asyncio
import unittest

from openclaw_gateway_runtime.gateway import GatewayService
from openclaw_gateway_runtime.protocol import frame
from openclaw_gateway_runtime.runtime import RuntimeService
from openclaw_gateway_runtime.runtime.openclaw_adapter import StubEchoAdapter
from openclaw_gateway_runtime.transport import InMemoryDuplex


def _collect_text(chunks: list[dict]) -> str:
    return "".join(c.get("text", "") for c in chunks)


class MultiTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_turns_history_is_used(self) -> None:
        gateway = GatewayService()
        runtime = RuntimeService(adapter=StubEchoAdapter())

        client_ep, gw_client_ep = InMemoryDuplex.create_pair()
        gw_runtime_ep, runtime_ep = InMemoryDuplex.create_pair()

        bridge_task = asyncio.create_task(
            gateway.bridge(client_id="c1", client_ep=gw_client_ep, runtime_ep=gw_runtime_ep)
        )
        runtime_task = asyncio.create_task(runtime.serve(runtime_ep))

        async def one_turn(text: str) -> str:
            await client_ep.send(frame(type="message.user", session_id="s1", payload={"text": text}))
            chunks: list[dict] = []
            async for f in client_ep:
                if f["type"] == "stream.chunk":
                    chunks.append(f.get("payload") or {})
                if f["type"] == "run.end":
                    break
            return _collect_text(chunks)

        out1 = await one_turn("one")
        out2 = await one_turn("two")
        out3 = await one_turn("three")

        # Stub format: echo:<text>;history:<last3 joined by |>
        self.assertIn("history:one", out1)
        self.assertIn("history:one|two", out2)
        self.assertIn("history:one|two|three", out3)

        await client_ep.close()
        await gw_runtime_ep.close()
        await runtime_ep.close()
        bridge_task.cancel()
        runtime_task.cancel()


if __name__ == "__main__":
    unittest.main()

