from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Optional

from ..infra.metrics import runtime_metrics
from ..infra.structured_log import log_event
from ..protocol import Frame, frame
from ..transport import Endpoint
from .assistant_agent_adapter import AssistantAgentAdapter
from .openclaw_adapter import (
    AnthropicSkillsAdapter,
    CancelToken,
    CliOpenClawAdapter,
    OpenClawAdapter,
    StubEchoAdapter,
)

if TYPE_CHECKING:
    from ..gateway.user_config import UserConfig


@dataclass
class ActiveRun:
    run_id: str
    turn_id: str
    cancel: CancelToken
    task: "asyncio.Task[None]"


def _select_runtime_adapter(*, user_config: Optional["UserConfig"] = None) -> OpenClawAdapter:
    env = os.environ
    requested = (
        (env.get("OPENCLAW_RUNTIME_ADAPTER") or "").strip().lower().replace("-", "_")
    )

    if requested:
        if requested == "assistant_agent":
            return AssistantAgentAdapter()
        if requested in {"anthropic", "anthropic_skills"}:
            return AnthropicSkillsAdapter(user_config=user_config)
        if requested in {"openclaw", "openclaw_cli", "cli"}:
            return CliOpenClawAdapter()
        if requested in {"stub", "stub_echo"}:
            return StubEchoAdapter()
        raise RuntimeError(
            "Unknown OPENCLAW_RUNTIME_ADAPTER="
            f"{env.get('OPENCLAW_RUNTIME_ADAPTER')!r}. "
            "Supported values: assistant_agent, anthropic, openclaw_cli, stub."
        )

    # Existing adapter selection when no explicit runtime adapter is configured.
    if (env.get("LLM_PROVIDER") or "").strip():
        return AnthropicSkillsAdapter(user_config=user_config)
    if env.get("OPENCLAW_REPO_PATH"):
        return CliOpenClawAdapter()
    return StubEchoAdapter()


class RuntimeService:
    """
    Runtime side of the Gateway<->Runtime stream.

    Responsibilities:
    - session management (in-memory for now)
    - run lifecycle and cancellation semantics
    - streaming events back to gateway
    - integrates an OpenClawAdapter (stub by default)
    """

    def __init__(
        self,
        *,
        user_id: str = "default",
        user_config: Optional["UserConfig"] = None,
        adapter: Optional[OpenClawAdapter] = None,
    ) -> None:
        self._user_id = user_id
        self._user_config = user_config
        # Adapter selection:
        # 1) explicit adapter injection
        # 2) OPENCLAW_RUNTIME_ADAPTER when set
        # 3) LLM_PROVIDER set → AnthropicSkillsAdapter (supports anthropic/minimax/qwen)
        # 4) OPENCLAW_REPO_PATH → CLI passthrough
        # 5) stub
        if adapter is not None:
            self._adapter = adapter
        else:
            self._adapter = _select_runtime_adapter(user_config=user_config)
        self._active_by_session: Dict[str, ActiveRun] = {}
        self._history_by_session: Dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def serve(self, ep: Endpoint) -> None:
        async for f in ep:
            t = f.get("type")
            if t == "message.user":
                await self._handle_user_message(ep, f)
            elif t == "run.cancel":
                await self._handle_cancel(ep, f)
            elif t == "ping":
                await ep.send(frame(type="pong"))
            else:
                await ep.send(
                    frame(
                        type="error",
                        error={"code": "unknown_frame", "message": f"unknown type: {t}"},
                    )
                )

    async def _handle_user_message(self, ep: Endpoint, f: Frame) -> None:
        session_id = f.get("session_id")
        payload = f.get("payload") or {}
        user_text = str(payload.get("text", ""))
        # user_id is carried on the frame (set by Gateway when routing)
        user_id: str = str(f.get("user_id") or self._user_id)

        if not session_id:
            await ep.send(frame(type="error", error={"code": "missing_session_id"}))
            return

        turn_id = str(payload.get("turn_id") or uuid.uuid4())
        run_id = str(payload.get("run_id") or uuid.uuid4())

        # Interrupt semantics: cancel current run in the same session, then start new.
        await self._interrupt_if_needed(session_id=session_id)

        async with self._lock:
            hist = self._history_by_session.setdefault(session_id, [])
            hist.append(user_text)
            history_snapshot = list(hist)

        cancel = CancelToken()

        async def _runner() -> None:
            # Register before any outbound frame: create_task may run this coroutine before the
            # outer function stores ActiveRun; otherwise run.cancel can race and miss the session.
            self_task = asyncio.current_task()
            assert self_task is not None
            async with self._lock:
                self._active_by_session[session_id] = ActiveRun(
                    run_id=run_id, turn_id=turn_id, cancel=cancel, task=self_task
                )
                runs_active = len(self._active_by_session)
            m = runtime_metrics.on_run_started()
            log_event(
                "runtime",
                "run_started",
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                runs_active=runs_active,
                **m,
            )

            end_reason = "error"
            expects_reply = True  # default: platform should wait for user reply
            try:
                await ep.send(frame(type="run.started", session_id=session_id, turn_id=turn_id, run_id=run_id))
                async for ev in self._adapter.run(
                    session_id=session_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    user_text=user_text,
                    history=history_snapshot,
                    cancel=cancel,
                    user_id=user_id,
                ):
                    if cancel.is_cancelled():
                        break
                    if ev.meta and "expects_reply" in ev.meta:
                        expects_reply = ev.meta["expects_reply"]
                        continue
                    await ep.send(frame(type=ev.type, session_id=session_id, turn_id=turn_id, run_id=run_id, payload=ev.payload))
                if cancel.is_cancelled():
                    end_reason = "cancelled"
                    await ep.send(frame(type="run.end", session_id=session_id, turn_id=turn_id, run_id=run_id, reason="cancelled", payload={"expects_reply": True}))
                else:
                    end_reason = "completed"
                    await ep.send(frame(type="run.end", session_id=session_id, turn_id=turn_id, run_id=run_id, reason="completed", payload={"expects_reply": expects_reply}))
            except Exception as e:  # noqa: BLE001 - boundary: convert to protocol error
                end_reason = "error"
                await ep.send(frame(type="run.end", session_id=session_id, turn_id=turn_id, run_id=run_id, reason="error", error={"message": str(e)}, payload={"expects_reply": True}))
            finally:
                async with self._lock:
                    cur = self._active_by_session.get(session_id)
                    if cur and cur.run_id == run_id:
                        self._active_by_session.pop(session_id, None)
                    runs_active = len(self._active_by_session)
                cnt = runtime_metrics.snapshot_counts()
                log_event(
                    "runtime",
                    "run_finished",
                    session_id=session_id,
                    run_id=run_id,
                    reason=end_reason,
                    expects_reply=expects_reply,
                    runs_active=runs_active,
                    **cnt,
                )

        asyncio.create_task(_runner())

    async def _interrupt_if_needed(self, *, session_id: str) -> None:
        async with self._lock:
            cur = self._active_by_session.get(session_id)
            if not cur:
                return
            cur.cancel.cancel()
            prev_run = cur.run_id
        m = runtime_metrics.on_cancel()
        async with self._lock:
            runs_active = len(self._active_by_session)
        log_event(
            "runtime",
            "run_interrupt",
            session_id=session_id,
            previous_run_id=prev_run,
            runs_active=runs_active,
            **m,
        )

    async def _handle_cancel(self, ep: Endpoint, f: Frame) -> None:
        run_id = f.get("run_id")
        session_id = f.get("session_id")

        log_sid: Optional[str] = None
        log_rid: Optional[str] = None
        did_cancel = False

        async with self._lock:
            if session_id:
                cur = self._active_by_session.get(session_id)
                if cur and (run_id is None or cur.run_id == run_id):
                    cur.cancel.cancel()
                    log_sid = str(session_id)
                    log_rid = str(cur.run_id)
                    did_cancel = True
            elif run_id:
                # Best-effort scan (only for in-memory single process)
                for sid, cur in list(self._active_by_session.items()):
                    if cur.run_id == run_id:
                        cur.cancel.cancel()
                        log_sid = str(sid)
                        log_rid = str(run_id)
                        did_cancel = True
                        break

        if did_cancel and log_rid is not None:
            m = runtime_metrics.on_cancel()
            async with self._lock:
                runs_active = len(self._active_by_session)
            log_event(
                "runtime",
                "run_cancel",
                session_id=log_sid,
                run_id=log_rid,
                runs_active=runs_active,
                **m,
            )
            return

        await ep.send(frame(type="error", error={"code": "run_not_found", "run_id": run_id, "session_id": session_id}))
