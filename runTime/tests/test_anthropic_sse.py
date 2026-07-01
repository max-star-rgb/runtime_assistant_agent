import unittest

from openclaw_gateway_runtime.agent_runtime.anthropic_sse import AnthropicStreamBuilder


class AnthropicSseTests(unittest.TestCase):
    def test_builder_accumulates_text_deltas(self) -> None:
        b = AnthropicStreamBuilder()
        self.assertIsNone(b.feed({"type": "message_start", "message": {"id": "m1"}}))
        self.assertIsNone(b.feed({"type": "content_block_start", "content_block": {"type": "text", "text": ""}}))
        d1 = b.feed({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "he"}})
        d2 = b.feed({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "llo"}})
        self.assertEqual(d1, "he")
        self.assertEqual(d2, "llo")
        self.assertEqual(b.response["id"], "m1")
        self.assertEqual(b.response["content"][0]["text"], "hello")


if __name__ == "__main__":
    unittest.main()

