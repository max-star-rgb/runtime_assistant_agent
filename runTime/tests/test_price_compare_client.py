import json
import os
import unittest
from unittest.mock import MagicMock, patch

from openclaw_gateway_runtime.agent_runtime.price_compare_client import (
    call_price_compare_service,
)


class PriceCompareClientTests(unittest.TestCase):
    def test_normalize_platforms_defaults(self) -> None:
        os.environ["PRICE_COMPARE_SERVICE_URL"] = "https://example.com/compare"
        try:
            with patch("urllib.request.urlopen") as m:
                mock_cm = MagicMock()
                mock_cm.read.return_value = b'{"ok":true}'
                m.return_value.__enter__.return_value = mock_cm
                m.return_value.__exit__.return_value = None
                call_price_compare_service(query="test", platforms=None)
                req = m.call_args[0][0]
                body = json.loads(req.data.decode("utf-8"))
                self.assertEqual(body["platforms"], ["jd", "taobao", "pdd"])
        finally:
            os.environ.pop("PRICE_COMPARE_SERVICE_URL", None)

    def test_raises_without_url(self) -> None:
        os.environ.pop("PRICE_COMPARE_SERVICE_URL", None)
        with self.assertRaises(ValueError):
            call_price_compare_service(query="x")


if __name__ == "__main__":
    unittest.main()
