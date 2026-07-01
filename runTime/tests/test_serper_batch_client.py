import json
import os
import unittest
from unittest.mock import MagicMock, patch

from openclaw_gateway_runtime.agent_runtime.serper_batch_client import batch_web_search_queries


class SerperBatchClientTests(unittest.TestCase):
    def test_requires_key(self) -> None:
        os.environ.pop("SERPER_API_KEY", None)
        with self.assertRaises(ValueError):
            batch_web_search_queries(["a"])

    def test_batch_calls_serper(self) -> None:
        os.environ["SERPER_API_KEY"] = "test-key"
        try:
            with patch("urllib.request.urlopen") as m:
                mock_cm = MagicMock()
                mock_cm.read.return_value = json.dumps(
                    {"organic": [{"title": "t", "link": "https://x", "snippet": "s"}]}
                ).encode()
                m.return_value.__enter__.return_value = mock_cm
                m.return_value.__exit__.return_value = None
                out = batch_web_search_queries(["hello"])
            data = json.loads(out)
            self.assertIn("batch_web_search", data)
            self.assertEqual(len(data["batch_web_search"]), 1)
        finally:
            os.environ.pop("SERPER_API_KEY", None)


if __name__ == "__main__":
    unittest.main()
