import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")

from openclaw_gateway_runtime.agent_runtime.ddg_search_client import (
    _cache,
    _cache_key,
    _site_name,
    web_search,
)


class SiteNameTests(unittest.TestCase):
    def test_extracts_domain(self) -> None:
        self.assertEqual(_site_name("https://www.example.com/path"), "example")

    def test_short_host(self) -> None:
        self.assertEqual(_site_name("https://localhost/x"), "")

    def test_invalid_url(self) -> None:
        self.assertEqual(_site_name(""), "")


class CacheKeyTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        k1 = _cache_key("q", 5, "wt-wt", "moderate")
        k2 = _cache_key("q", 5, "wt-wt", "moderate")
        self.assertEqual(k1, k2)

    def test_differs_on_query(self) -> None:
        k1 = _cache_key("a", 5, "wt-wt", "moderate")
        k2 = _cache_key("b", 5, "wt-wt", "moderate")
        self.assertNotEqual(k1, k2)


class WebSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        _cache.clear()

    def test_empty_query_raises(self) -> None:
        with self.assertRaises(ValueError):
            web_search(query="   ")

    @patch("openclaw_gateway_runtime.agent_runtime.ddg_search_client.DDGS")
    def test_returns_json(self, mock_ddgs_cls: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.text.return_value = [
            {"title": "Result 1", "href": "https://example.com/1", "body": "snippet 1"},
            {"title": "Result 2", "href": "https://shop.example.com/2", "body": "snippet 2"},
        ]
        mock_ddgs_cls.return_value = mock_instance

        result = web_search(query="test product")
        data = json.loads(result)
        self.assertEqual(data["provider"], "ddgs")
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["results"][0]["title"], "Result 1")
        self.assertEqual(data["results"][0]["url"], "https://example.com/1")

    @patch("openclaw_gateway_runtime.agent_runtime.ddg_search_client.DDGS")
    def test_cache_hit(self, mock_ddgs_cls: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.text.return_value = [
            {"title": "T", "href": "https://x.com", "body": "b"},
        ]
        mock_ddgs_cls.return_value = mock_instance

        r1 = web_search(query="cached query")
        r2 = web_search(query="cached query")
        d2 = json.loads(r2)
        self.assertTrue(d2.get("cached"))
        # DDGS should only be called once
        self.assertEqual(mock_ddgs_cls.call_count, 1)

    @patch("openclaw_gateway_runtime.agent_runtime.ddg_search_client.DDGS")
    def test_count_clamp(self, mock_ddgs_cls: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.text.return_value = []
        mock_ddgs_cls.return_value = mock_instance

        web_search(query="test", count=100)
        _, kwargs = mock_instance.text.call_args
        self.assertEqual(kwargs["max_results"], 20)

    @patch("openclaw_gateway_runtime.agent_runtime.ddg_search_client.DDGS")
    def test_exception_wrapped(self, mock_ddgs_cls: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.text.side_effect = Exception("network error")
        mock_ddgs_cls.return_value = mock_instance

        with self.assertRaises(RuntimeError):
            web_search(query="fail")


if __name__ == "__main__":
    unittest.main()
