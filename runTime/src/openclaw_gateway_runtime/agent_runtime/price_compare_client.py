"""
Call an operator-hosted price-compare HTTP API (JSON POST).

Configure:
  PRICE_COMPARE_SERVICE_URL — full URL to POST (required).
  PRICE_COMPARE_API_KEY   — optional Bearer token.
  PRICE_COMPARE_TIMEOUT_S — default 45.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_ALLOWED_PLATFORMS = frozenset({"jd", "taobao", "pdd"})


def _normalize_platforms(platforms: list[str] | None) -> list[str]:
    if not platforms:
        return ["jd", "taobao", "pdd"]
    out: list[str] = []
    for p in platforms:
        s = str(p).strip().lower()
        if s in _ALLOWED_PLATFORMS and s not in out:
            out.append(s)
    return out if out else ["jd", "taobao", "pdd"]


def call_price_compare_service(*, query: str, platforms: list[str] | None = None) -> str:
    """
    POST JSON body to PRICE_COMPARE_SERVICE_URL; return response body as UTF-8 text.

    Expected JSON shape (example — your service defines the contract):
      {
        "query": "...",
        "offers": [
          {
            "platform": "jd",
            "title": "...",
            "price": "99.00",
            "currency": "CNY",
            "landing_url": "https://...",
            "note": "open in app to add to cart"
          }
        ]
      }
    """
    url = (os.environ.get("PRICE_COMPARE_SERVICE_URL") or "").strip()
    if not url:
        raise ValueError("PRICE_COMPARE_SERVICE_URL is not set")
    q = query.strip()
    if not q:
        raise ValueError("query is empty")

    timeout_s = int(os.environ.get("PRICE_COMPARE_TIMEOUT_S") or "45")
    api_key = (os.environ.get("PRICE_COMPARE_API_KEY") or "").strip()

    body_obj = {
        "query": q,
        "platforms": _normalize_platforms(platforms),
    }
    data = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("content-type", "application/json; charset=utf-8")
    if api_key:
        req.add_header("authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"price_compare HTTP {e.code}: {raw}") from e
    except Exception as e:
        raise RuntimeError(f"price_compare request failed: {e}") from e

    return raw.strip()
