from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncIterator, Dict, Optional

from ..infra.structured_log import log_event
from ..protocol import Frame
from ..transport import Endpoint, InMemoryDuplex
from .user_config import UserConfig, save_user_config


def _max_runtime_instances() -> int:
    try:
        return int((os.environ.get("MAX_RUNTIME_INSTANCES") or "20").strip())
    except ValueError:
        return 20


def _idle_timeout_s() -> float:
    try:
        return float((os.environ.get("RUNTIME_IDLE_TIMEOUT_S") or "300").strip())
    except ValueError:
        return 300.0


def _hangup_grace_s() -> float:
    """
    After a call.hangup, how many seconds to keep the RuntimeService alive
    before destroying it (in case the user calls back quickly).
    Defaults to 300 s (same as idle timeout).
    """
    try:
        return float((os.environ.get("RUNTIME_HANGUP_GRACE_S") or "300").strip())
    except ValueError:
        return 300.0


class _TouchableEndpoint(Endpoint):
    """
    Thin proxy around an Endpoint that bumps a timestamp on every frame
    sent or received, so RuntimeManager can evict idle instances.
    """

    def __init__(self, inner: Endpoint, touch_fn) -> None:  # type: ignore[type-arg]
        self._inner = inner
        self._touch = touch_fn

    async def send(self, frame: "Frame") -> None:
        self._touch()
        await self._inner.send(frame)

    async def __aiter__(self) -> AsyncIterator["Frame"]:
        async for f in self._inner:
            self._touch()
            yield f

    async def close(self) -> None:
        if hasattr(self._inner, "close"):
            await self._inner.close()  # type: ignore[attr-defined]


class _RuntimeEntry:
    """Holds a live RuntimeService instance and its paired Endpoint."""

    def __init__(self, user_id: str, user_config: UserConfig) -> None:
        self.user_id = user_id
        self.user_config = user_config
        self.last_active: float = time.monotonic()
        self.hung_up_at: Optional[float] = None  # set when call.hangup is received

        _raw_gateway_ep, self.runtime_ep = InMemoryDuplex.create_pair()
        # Wrap gateway side so every frame touch refreshes last_active
        self.gateway_ep: Endpoint = _TouchableEndpoint(
            _raw_gateway_ep, self._touch
        )
        self._task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    def _touch(self) -> None:
        self.last_active = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_active

    def hangup_seconds(self) -> Optional[float]:
        """Seconds elapsed since call.hangup; None if not yet hung up."""
        return (time.monotonic() - self.hung_up_at) if self.hung_up_at is not None else None

    def start(self) -> None:
        from ..runtime.runtime import RuntimeService

        service = RuntimeService(user_id=self.user_id, user_config=self.user_config)
        self._task = asyncio.create_task(
            self._run_guarded(service),
            name=f"runtime-{self.user_id}",
        )

    async def _run_guarded(self, service) -> None:  # type: ignore[no-untyped-def]
        try:
            await service.serve(self.runtime_ep)
        except Exception as exc:
            log_event(
                "runtime_manager",
                "runtime_crashed",
                user_id=self.user_id,
                error=str(exc),
            )

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


class RuntimeManager:
    """
    Manages per-user RuntimeService instances (in-process coroutine mode).

    This is the Demo-phase implementation: all RuntimeService instances share the
    Gateway process.  To switch to container-per-user, replace this class with a
    ContainerRuntimeManager that returns WsEndpoint objects instead.

    Idle instances (no frames for RUNTIME_IDLE_TIMEOUT_S seconds, default 300)
    are automatically reclaimed by a background reaper task.
    """

    _REAPER_INTERVAL_S = 30  # how often the reaper wakes up to scan

    def __init__(self) -> None:
        self._entries: Dict[str, _RuntimeEntry] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    def _ensure_reaper(self) -> None:
        """Start the idle-reaper background task if not already running."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._reaper_loop(), name="runtime-idle-reaper"
            )

    async def _reaper_loop(self) -> None:
        timeout = _idle_timeout_s()
        log_event(
            "runtime_manager",
            "reaper_started",
            idle_timeout_s=timeout,
            interval_s=self._REAPER_INTERVAL_S,
        )
        while True:
            await asyncio.sleep(self._REAPER_INTERVAL_S)
            idle_t = _idle_timeout_s()
            grace_t = _hangup_grace_s()
            evict: list[str] = []
            async with self._lock:
                for uid, entry in self._entries.items():
                    hung_s = entry.hangup_seconds()
                    if hung_s is not None:
                        # Already hung up: destroy after grace period with no new activity
                        if hung_s >= grace_t and entry.idle_seconds() >= grace_t:
                            evict.append(uid)
                    else:
                        # Normal idle eviction
                        if entry.idle_seconds() >= idle_t:
                            evict.append(uid)

            for uid in evict:
                entry = self._entries.get(uid)
                reason = "hangup_grace_expired" if (entry and entry.hung_up_at) else "idle_timeout"
                log_event(
                    "runtime_manager",
                    "idle_evict",
                    user_id=uid,
                    reason=reason,
                )
                await self.destroy(uid)

    async def get_or_create(self, user_id: str, user_config: UserConfig) -> Endpoint:
        """
        Return the Gateway-side Endpoint for *user_id*.
        Creates a new RuntimeService if none exists yet.
        """
        async with self._lock:
            if user_id in self._entries:
                entry = self._entries[user_id]
                # Always update user_config with latest values from the new connection
                if user_config:
                    for key in ("language", "user_info", "agent_profile", "tone"):
                        new_val = getattr(user_config, key, None)
                        if new_val is not None:
                            setattr(entry.user_config, key, new_val)
                if entry.hung_up_at is not None:
                    # User called back during grace period — clear hangup flag and resume
                    entry.hung_up_at = None
                    entry._touch()
                    log_event("runtime_manager", "runtime_resumed", user_id=user_id)
                else:
                    log_event("runtime_manager", "runtime_reused", user_id=user_id)
                return entry.gateway_ep

            max_inst = _max_runtime_instances()
            if len(self._entries) >= max_inst:
                raise RuntimeError(
                    f"runtime_limit_reached: max {max_inst} instances already running"
                )

            entry = _RuntimeEntry(user_id=user_id, user_config=user_config)
            entry.start()
            self._entries[user_id] = entry
            log_event(
                "runtime_manager",
                "runtime_created",
                user_id=user_id,
                active_count=len(self._entries),
            )
            self._ensure_reaper()
            return entry.gateway_ep

    async def mark_hangup(self, user_id: str) -> None:
        """
        Mark a user's RuntimeService as hung-up without destroying it immediately.
        The reaper will destroy it after RUNTIME_HANGUP_GRACE_S seconds of inactivity.
        If the user calls back before the grace period expires, get_or_create will
        clear the hung_up_at flag and resume the existing instance.
        """
        async with self._lock:
            entry = self._entries.get(user_id)
        if entry is not None and entry.hung_up_at is None:
            entry.hung_up_at = time.monotonic()
            log_event(
                "runtime_manager",
                "runtime_hangup_marked",
                user_id=user_id,
                grace_s=_hangup_grace_s(),
            )

    async def destroy(self, user_id: str) -> None:
        """
        Stop the RuntimeService for *user_id* and persist its config.
        Safe to call even if the user is not in the table.
        """
        async with self._lock:
            entry = self._entries.pop(user_id, None)
            active_count = len(self._entries)

        if entry is None:
            return

        entry.stop()
        try:
            await save_user_config(entry.user_config)
        except Exception as exc:
            log_event(
                "runtime_manager",
                "config_save_failed",
                user_id=user_id,
                error=str(exc),
            )
        log_event(
            "runtime_manager",
            "runtime_destroyed",
            user_id=user_id,
            active_count=active_count,
        )

    async def inject_config(self, user_id: str, key: str, value: str) -> bool:
        """
        Update a config field for *user_id* in-memory and persist to disk.
        Returns True if the user was found (online), False if only persisted to disk.
        """
        async with self._lock:
            entry = self._entries.get(user_id)

        if entry is not None:
            setattr(entry.user_config, key, value)
            try:
                await save_user_config(entry.user_config)
            except Exception as exc:
                log_event(
                    "runtime_manager",
                    "config_save_failed",
                    user_id=user_id,
                    error=str(exc),
                )
            log_event(
                "runtime_manager",
                "config_updated",
                user_id=user_id,
                key=key,
                online=True,
            )
            return True

        # User offline: persist to disk so it takes effect on next call
        from .user_config import UserConfig, load_user_config

        cfg = load_user_config(user_id)
        if hasattr(cfg, key):
            setattr(cfg, key, value)
            try:
                await save_user_config(cfg)
            except Exception as exc:
                log_event(
                    "runtime_manager",
                    "config_save_failed",
                    user_id=user_id,
                    error=str(exc),
                )
        log_event(
            "runtime_manager",
            "config_updated",
            user_id=user_id,
            key=key,
            online=False,
        )
        return False

    def get_user_config(self, user_id: str) -> Optional[UserConfig]:
        """Return the live UserConfig for *user_id* if online, else None."""
        entry = self._entries.get(user_id)
        return entry.user_config if entry else None
