"""
Pluggable memory client for the agent runtime.

Supports two backends:
  1. **External REST service** — when ``MEMORY_SERVICE_URL`` is set.
     Any backend (Redis, SQLite, vector DB, cloud) can implement the API.
  2. **Local JSON files** — default fallback when no service is configured.
     Stores per-user JSON files in ``MEMORY_LOCAL_DIR`` (or system temp).

REST API contract:
  POST /memory/save   {user_id, session_id, entries: [{type, content, importance}]}
  GET  /memory/recall  ?user_id=...&query=...&limit=10
       -> {memories: [{content, type, created_at, relevance}]}
  DELETE /memory/forget ?user_id=...&before=...
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def _cfg_service_url() -> str:
    return (os.environ.get("MEMORY_SERVICE_URL") or "").strip().rstrip("/")


def _cfg_local_dir() -> Path:
    raw = (os.environ.get("MEMORY_LOCAL_DIR") or "").strip()
    if raw:
        return Path(raw)
    import tempfile
    return Path(tempfile.gettempdir()) / "runtime_memory"


def _cfg_recall_limit() -> int:
    return int(os.environ.get("MEMORY_RECALL_LIMIT") or "10")


# -----------------------------------------------------------------------
# Data types
# -----------------------------------------------------------------------

class MemoryEntry:
    __slots__ = ("type", "content", "importance", "created_at")

    def __init__(
        self,
        content: str,
        type: str = "fact",
        importance: float = 0.5,
        created_at: str | None = None,
    ):
        self.type = type
        self.content = content
        self.importance = importance
        self.created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%S")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "content": self.content,
            "importance": self.importance,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryEntry":
        return cls(
            content=str(d.get("content", "")),
            type=str(d.get("type", "fact")),
            importance=float(d.get("importance", 0.5)),
            created_at=d.get("created_at"),
        )


# -----------------------------------------------------------------------
# Local JSON file backend
# -----------------------------------------------------------------------

class _LocalStore:
    """Simple per-user JSON file storage."""

    def __init__(self, base_dir: Path):
        self._dir = base_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, user_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self._dir / f"{safe}.json"

    def _load(self, user_id: str) -> list[dict[str, Any]]:
        p = self._path(user_id)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self, user_id: str, entries: list[dict[str, Any]]) -> None:
        p = self._path(user_id)
        p.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    def save(self, user_id: str, new_entries: list[MemoryEntry]) -> int:
        existing = self._load(user_id)
        for e in new_entries:
            existing.append(e.to_dict())
        self._save(user_id, existing)
        return len(new_entries)

    def recall(self, user_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        all_entries = self._load(user_id)
        if not query:
            return all_entries[-limit:]
        # Simple keyword relevance: count how many query chars appear in content
        query_lower = query.lower()
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in all_entries:
            content_lower = entry.get("content", "").lower()
            score = sum(1 for kw in query_lower.split() if kw in content_lower)
            if score > 0:
                entry_copy = dict(entry, relevance=round(score / max(len(query_lower.split()), 1), 2))
                scored.append((score, entry_copy))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [s[1] for s in scored[:limit]]
        if not results:
            return all_entries[-limit:]
        return results

    def forget(self, user_id: str, before: str | None = None) -> int:
        if before is None:
            p = self._path(user_id)
            if p.exists():
                entries = self._load(user_id)
                p.unlink()
                return len(entries)
            return 0
        entries = self._load(user_id)
        kept = [e for e in entries if (e.get("created_at") or "") >= before]
        removed = len(entries) - len(kept)
        self._save(user_id, kept)
        return removed


# -----------------------------------------------------------------------
# REST backend
# -----------------------------------------------------------------------

class _RemoteStore:
    """Calls an external REST memory service."""

    def __init__(self, base_url: str):
        self._url = base_url
        self._timeout = int(os.environ.get("MEMORY_SERVICE_TIMEOUT_S") or "10")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self._url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        qs = urllib.parse.urlencode(params)
        url = f"{self._url}{path}?{qs}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _delete(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        qs = urllib.parse.urlencode(params)
        url = f"{self._url}{path}?{qs}"
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def save(self, user_id: str, session_id: str, new_entries: list[MemoryEntry]) -> int:
        body = {
            "user_id": user_id,
            "session_id": session_id,
            "entries": [e.to_dict() for e in new_entries],
        }
        resp = self._post("/memory/save", body)
        return resp.get("saved", len(new_entries))

    def recall(self, user_id: str, query: str, limit: int) -> list[dict[str, Any]]:
        params = {"user_id": user_id, "query": query, "limit": str(limit)}
        resp = self._get("/memory/recall", params)
        return resp.get("memories", [])

    def forget(self, user_id: str, before: str | None = None) -> int:
        params: dict[str, str] = {"user_id": user_id}
        if before:
            params["before"] = before
        resp = self._delete("/memory/forget", params)
        return resp.get("deleted", 0)


# -----------------------------------------------------------------------
# Unified MemoryClient
# -----------------------------------------------------------------------

class MemoryClient:
    """
    Unified memory interface. Automatically selects backend based on config.
    Thread-safe for sync operations; async callers should use asyncio.to_thread.
    """

    def __init__(self) -> None:
        url = _cfg_service_url()
        if url:
            self._remote = _RemoteStore(url)
            self._local: _LocalStore | None = None
        else:
            self._remote = None
            self._local = _LocalStore(_cfg_local_dir())

    @property
    def backend_name(self) -> str:
        return "remote" if self._remote else "local"

    def save(
        self,
        user_id: str,
        entries: list[MemoryEntry],
        session_id: str = "",
    ) -> int:
        if not entries:
            return 0
        try:
            if self._remote:
                return self._remote.save(user_id, session_id, entries)
            assert self._local is not None
            return self._local.save(user_id, entries)
        except Exception:
            return 0

    def recall(
        self,
        user_id: str,
        query: str = "",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit is None:
            limit = _cfg_recall_limit()
        try:
            if self._remote:
                return self._remote.recall(user_id, query, limit)
            assert self._local is not None
            return self._local.recall(user_id, query, limit)
        except Exception:
            return []

    def forget(self, user_id: str, before: str | None = None) -> int:
        try:
            if self._remote:
                return self._remote.forget(user_id, before)
            assert self._local is not None
            return self._local.forget(user_id, before)
        except Exception:
            return 0


# Singleton
_client: MemoryClient | None = None


def get_memory_client() -> MemoryClient:
    global _client
    if _client is None:
        _client = MemoryClient()
    return _client
