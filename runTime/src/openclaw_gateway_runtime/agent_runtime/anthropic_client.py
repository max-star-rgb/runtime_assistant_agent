from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional


class AnthropicError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnthropicConfig:
    api_key: str
    model: str
    base_url: str = "https://api.anthropic.com"
    version: str = "2023-06-01"
    timeout_s: int = 120
    max_tokens: int = 1024


class AnthropicClient:
    def __init__(self, cfg: AnthropicConfig) -> None:
        self._cfg = cfg

    _PLACEHOLDER_KEYS = frozenset({"your_key_here", "changeme", "replace_me", "your_minimax_key_here", "your_qwen_key_here"})

    @classmethod
    def _is_placeholder_key(cls, s: str) -> bool:
        return s.strip().lower() in cls._PLACEHOLDER_KEYS

    @classmethod
    def _read_key(cls, env_var: str) -> str:
        v = (os.environ.get(env_var) or "").strip()
        return "" if (not v or cls._is_placeholder_key(v)) else v

    _PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
        "anthropic": {
            "key_env": "ANTHROPIC_API_KEY",
            "model": "claude-3-5-sonnet-latest",
            "base_url": "https://api.anthropic.com",
            "max_tokens": "1024",
        },
        "minimax": {
            "key_env": "MINIMAX_API_KEY",
            "model": "MiniMax-M2.7",
            "base_url_intl": "https://api.minimax.io/anthropic",
            "base_url_cn": "https://api.minimaxi.com/anthropic",
            "max_tokens": "1024",
        },
        "qwen": {
            "key_env": "QWEN_API_KEY",
            "model": "qwen3-coder-plus",
            "base_url_intl": "https://dashscope-intl.aliyuncs.com/apps/anthropic",
            "base_url_cn": "https://dashscope.aliyuncs.com/apps/anthropic",
            "max_tokens": "8192",
        },
    }

    @classmethod
    def from_env(cls) -> "AnthropicClient":
        provider = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
        if not provider:
            raise AnthropicError(
                "LLM_PROVIDER is not set. "
                "Set it to one of: anthropic, minimax, qwen. "
                "Then configure the corresponding API key and model."
            )
        if provider not in cls._PROVIDER_DEFAULTS:
            raise AnthropicError(
                f"Unknown LLM_PROVIDER={provider!r}. "
                f"Supported: {', '.join(sorted(cls._PROVIDER_DEFAULTS))}."
            )

        defaults = cls._PROVIDER_DEFAULTS[provider]
        prefix = provider.upper()

        key = cls._read_key(defaults["key_env"])
        if not key:
            raise AnthropicError(f"{defaults['key_env']} is not set (required for LLM_PROVIDER={provider}).")

        model = (os.environ.get(f"{prefix}_MODEL") or defaults["model"]).strip()

        if provider == "anthropic":
            base_url = (os.environ.get("ANTHROPIC_BASE_URL") or defaults["base_url"]).strip()
        else:
            region = (os.environ.get(f"{prefix}_REGION") or "").strip().upper()
            if region in {"CN", "CHINA", "ZH"}:
                default_base = defaults.get("base_url_cn", defaults.get("base_url", ""))
            else:
                default_base = defaults.get("base_url_intl", defaults.get("base_url", ""))
            base_url = (os.environ.get(f"{prefix}_BASE_URL") or default_base).strip()

        max_tokens = int(os.environ.get(f"{prefix}_MAX_TOKENS") or defaults["max_tokens"])
        timeout_s = int(os.environ.get(f"{prefix}_TIMEOUT_S") or "120")
        return cls(
            AnthropicConfig(
                api_key=key,
                model=model,
                base_url=base_url,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
            )
        )

    _BEARER_AUTH_HOSTS = ("minimax", "dashscope", "aliyuncs")

    def _uses_bearer_auth(self) -> bool:
        base = self._cfg.base_url.lower()
        return any(h in base for h in self._BEARER_AUTH_HOSTS)

    def _add_auth_headers(self, req: urllib.request.Request) -> None:
        if self._uses_bearer_auth():
            req.add_header("authorization", f"Bearer {self._cfg.api_key}")
        else:
            req.add_header("x-api-key", self._cfg.api_key)
        req.add_header("anthropic-version", self._cfg.version)

    def messages_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._cfg.base_url.rstrip('/')}/v1/messages"
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        self._add_auth_headers(req)

        try:
            with urllib.request.urlopen(req, timeout=self._cfg.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise AnthropicError(f"Anthropic request failed: HTTP {e.code}\n{raw}") from e
        except Exception as e:
            raise AnthropicError(f"Anthropic request failed: {e}") from e

        try:
            obj = json.loads(raw)
        except Exception as e:
            raise AnthropicError(f"Failed to parse Anthropic JSON: {e}\nRaw:\n{raw}") from e

        if not isinstance(obj, dict):
            raise AnthropicError("Anthropic response is not a JSON object.")
        return obj

    async def messages_create_stream(self, payload: Dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """
        Async generator of SSE event objects.
        Each yielded item is a dict parsed from `data:` JSON lines.
        """
        import asyncio

        url = f"{self._cfg.base_url.rstrip('/')}/v1/messages"
        payload = dict(payload)
        payload["stream"] = True
        body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("content-type", "application/json")
        req.add_header("accept", "text/event-stream")
        self._add_auth_headers(req)

        q2: "asyncio.Queue[object]" = asyncio.Queue()

        def _worker2() -> None:
            from .anthropic_sse import iter_sse_data_lines

            try:
                with urllib.request.urlopen(req, timeout=self._cfg.timeout_s) as resp:
                    for data in iter_sse_data_lines(resp):
                        if not data or data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue
                        q2.put_nowait(obj)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                q2.put_nowait(AnthropicError(f"Anthropic stream failed: HTTP {e.code}\n{raw}"))
            except Exception as e:  # noqa: BLE001
                q2.put_nowait(e)
            finally:
                q2.put_nowait(None)

        # Start worker in thread; yield from queue.
        asyncio.get_running_loop().run_in_executor(None, _worker2)
        while True:
            item = await q2.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise AnthropicError(f"Anthropic stream failed: {item}")
            if isinstance(item, dict):
                yield item

