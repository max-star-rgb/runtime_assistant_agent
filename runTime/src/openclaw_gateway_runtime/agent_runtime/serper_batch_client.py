"""
Batch web search via Serper (https://serper.dev) — Google search results as JSON.

Enable with env SERPER_API_KEY. This is a fallback when you have no custom PRICE_COMPARE_SERVICE_URL:
returns titles/links/snippets, not structured e-commerce prices.

Env:
  SERPER_API_KEY     — required for batch_web_search tool
  SERPER_TIMEOUT_S   — per-request timeout (default 20)
  SERPER_MAX_QUERIES — max queries per batch (default 5)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def batch_web_search_queries(queries: list[str]) -> str:
    """
    Run up to N independent Serper searches; return one JSON string for the model.
    """
    key = (os.environ.get("SERPER_API_KEY") or "").strip()
    if not key:
        raise ValueError("SERPER_API_KEY is not set")

    max_q = int(os.environ.get("SERPER_MAX_QUERIES") or "5")
    max_q = max(1, min(max_q, 10))
    timeout_s = int(os.environ.get("SERPER_TIMEOUT_S") or "20")

    cleaned: list[str] = []
    for q in queries:
        s = str(q).strip()
        if s and s not in cleaned:
            cleaned.append(s)
        if len(cleaned) >= max_q:
            break

    if not cleaned:
        raise ValueError("queries is empty")

    url = "https://google.serper.dev/search"
    out: list[dict[str, object]] = []

    for q in cleaned:
        body = json.dumps({"q": q}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("content-type", "application/json; charset=utf-8")
        req.add_header("X-API-KEY", key)

        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                parsed = {"raw": raw}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            parsed = {"error": f"HTTP {e.code}", "body": err_body[:2000]}
        except Exception as e:
            parsed = {"error": str(e)}

        out.append({"query": q, "search": parsed})

    return json.dumps({"batch_web_search": out}, ensure_ascii=False, indent=2)
