import os

os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")

import asyncio
import unittest

from openclaw_gateway_runtime.gateway import GatewayService
from openclaw_gateway_runtime.protocol import frame
from openclaw_gateway_runtime.runtime import RuntimeService
from openclaw_gateway_runtime.runtime.openclaw_adapter import StubEchoAdapter
from openclaw_gateway_runtime.transport import InMemoryDuplex


class ExpectsReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_end_carries_expects_reply_true_by_default(self) -> None:
        """StubEchoAdapter yields no meta event, so expects_reply defaults to True."""
        gateway = GatewayService()
        runtime = RuntimeService(adapter=StubEchoAdapter())

        client_ep, gw_client_ep = InMemoryDuplex.create_pair()
        gw_runtime_ep, runtime_ep = InMemoryDuplex.create_pair()

        bridge_task = asyncio.create_task(
            gateway.bridge(client_id="c1", client_ep=gw_client_ep, runtime_ep=gw_runtime_ep)
        )
        runtime_task = asyncio.create_task(runtime.serve(runtime_ep))

        await client_ep.send(frame(type="message.user", session_id="s1", payload={"text": "hello"}))

        run_end_frame = None
        async for f in client_ep:
            if f["type"] == "run.end":
                run_end_frame = f
                break

        self.assertIsNotNone(run_end_frame)
        self.assertEqual(run_end_frame["reason"], "completed")
        payload = run_end_frame.get("payload", {})
        self.assertIn("expects_reply", payload)
        self.assertTrue(payload["expects_reply"])

        await client_ep.close()
        await gw_runtime_ep.close()
        await runtime_ep.close()
        bridge_task.cancel()
        runtime_task.cancel()


if __name__ == "__main__":
    unittest.main()
