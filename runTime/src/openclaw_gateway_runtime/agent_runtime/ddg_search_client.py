"""
Web search via the `ddgs` library (metasearch across DuckDuckGo, Bing, Brave, Google, etc.).

Replaces the old HTML-scraping approach which was frequently blocked by bot detection.
The `backend="auto"` setting lets ddgs automatically pick a working search engine.

Env (all optional):
  WEB_SEARCH_TIMEOUT_S  — HTTP timeout per request (default 10)
  WEB_SEARCH_CACHE_TTL  — in-memory cache TTL in seconds (default 300)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from ddgs import DDGS

_DEFAULT_TIMEOUT_S = 10
_DEFAULT_CACHE_TTL_S = 300
_DEFAULT_COUNT = 5

# In-memory cache: key -> (payload_dict, expires_at)
_cache: dict[str, tuple[dict[str, Any], float]] = {}


def _cache_key(query: str, count: int, region: str, safe: str) -> str:
    return json.dumps(
        {"provider": "ddgs", "query": query, "count": count, "region": region, "safe": safe},
        sort_keys=True,
    )


def _site_name(url: str) -> str:
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        parts = host.split(".")
        if len(parts) >= 2:
            return parts[-2]
    except Exception:
        pass
    return ""


def web_search(
    *,
    query: str,
    count: int | None = None,
    region: str | None = None,
    safe_search: str | None = None,
) -> str:
    """
    Run a web search and return JSON results.

    Interface is unchanged from the old HTML-scraping version so
    anthropic_runtime.py requires zero changes.
    """
    if not query.strip():
        raise ValueError("query is empty")

    count = max(1, min(count or _DEFAULT_COUNT, 20))
    region = (region or "").strip() or "cn-zh"
    safe_map = {"strict": "on", "moderate": "moderate", "off": "off"}
    safe = safe_map.get(safe_search or "", "moderate")
    timeout_s = int(os.environ.get("WEB_SEARCH_TIMEOUT_S") or str(_DEFAULT_TIMEOUT_S))
    cache_ttl = int(os.environ.get("WEB_SEARCH_CACHE_TTL") or str(_DEFAULT_CACHE_TTL_S))

    ck = _cache_key(query, count, region, safe)
    now = time.monotonic()
    cached = _cache.get(ck)
    if cached and cached[1] > now:
        return json.dumps({**cached[0], "cached": True}, ensure_ascii=False, indent=2)

    try:
        results_raw = DDGS(timeout=timeout_s).text(
            query,
            region=region,
            safesearch=safe,
            max_results=count,
            backend="auto",
        )
    except Exception as e:
        raise RuntimeError(f"web search failed: {e}") from e

    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": r.get("body", ""),
            **({"siteName": _site_name(r.get("href", ""))} if _site_name(r.get("href", "")) else {}),
        }
        for r in (results_raw or [])
    ]

    payload: dict[str, Any] = {
        "query": query,
        "provider": "ddgs",
        "count": len(results),
        "results": results,
    }

    _cache[ck] = (payload, now + cache_ttl)

    return json.dumps(payload, ensure_ascii=False, indent=2)
