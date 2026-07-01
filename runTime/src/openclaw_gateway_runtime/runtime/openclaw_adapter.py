from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class AdapterEvent:
    type: str
    payload: object | None = None
    meta: dict | None = None


class CancelToken:
    def __init__(self) -> None:
        self._evt = asyncio.Event()

    def cancel(self) -> None:
        self._evt.set()

    async def cancelled(self) -> None:
        await self._evt.wait()

    def is_cancelled(self) -> bool:
        return self._evt.is_set()


class OpenClawAdapter:
    """
    A thin seam for integrating the real OpenClaw runtime.

    In this repository we keep semantics (run cancellation, interrupt) stable while
    allowing the execution engine to be swapped from stub -> real OpenClaw.
    """

    async def run(
        self,
        *,
        session_id: str,
        turn_id: str,
        run_id: str,
        user_text: str,
        history: list[str],
        cancel: CancelToken,
        user_id: str = "default",
    ) -> AsyncIterator[AdapterEvent]:
        raise NotImplementedError()


class StubEchoAdapter(OpenClawAdapter):
    """
    A deterministic stub that streams characters and supports cancellation.
    """

    def __init__(self, *, delay_s: float = 0.05) -> None:
        self._delay_s = delay_s

    async def run(
        self,
        *,
        session_id: str,
        turn_id: str,
        run_id: str,
        user_text: str,
        history: list[str],
        cancel: CancelToken,
        user_id: str = "default",
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent("event.skill", {"name": "stub.echo", "phase": "start"})

        # Include a compact history summary so multi-turn tests can assert context works.
        # Format intentionally stable for tests.
        hist = "|".join(history[-3:])  # last 3 user messages
        out = f"echo:{user_text};history:{hist}"
        for ch in out:
            if cancel.is_cancelled():
                return
            await asyncio.sleep(self._delay_s)
            yield AdapterEvent("stream.chunk", {"text": ch})

        yield AdapterEvent("event.skill", {"name": "stub.echo", "phase": "end"})


class CliOpenClawAdapter(OpenClawAdapter):
    """
    Execute OpenClaw via its Node CLI (`openclaw.mjs`) to preserve core agent+skills behavior.

    Requirements (not enforced by this repo):
    - Node.js >= 22.12 (as required by OpenClaw's `openclaw.mjs`)
    - An OpenClaw source tree with built `dist/` output (or an installed package that ships dist)

    Configuration:
    - OPENCLAW_REPO_PATH: path to the OpenClaw repo root (must contain `openclaw.mjs`)
    - OPENCLAW_ARGS (optional): extra CLI args appended (string, split on whitespace)
    """

    def __init__(self, *, repo_path: Optional[str] = None) -> None:
        self._repo_path = repo_path or os.environ.get("OPENCLAW_REPO_PATH") or ""

    def _resolve_command(self, *, session_id: str, user_text: str) -> list[str]:
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js not found on PATH (required to run OpenClaw).")
        if not self._repo_path:
            raise RuntimeError("OPENCLAW_REPO_PATH is not set.")

        openclaw_mjs = os.path.join(self._repo_path, "openclaw.mjs")
        if not os.path.exists(openclaw_mjs):
            raise RuntimeError(f"openclaw.mjs not found at: {openclaw_mjs}")

        extra = os.environ.get("OPENCLAW_ARGS", "").strip().split() if os.environ.get("OPENCLAW_ARGS") else []

        # We use --local to run embedded agent (no OpenClaw gateway daemon dependency).
        # --json is used for a stable machine-readable result.
        return [
            node,
            openclaw_mjs,
            "agent",
            "--local",
            "--session-id",
            session_id,
            "--message",
            user_text,
            "--json",
            *extra,
        ]

    async def run(
        self,
        *,
        session_id: str,
        turn_id: str,
        run_id: str,
        user_text: str,
        history: list[str],
        cancel: CancelToken,
        user_id: str = "default",
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent("event.skill", {"name": "openclaw.cli", "phase": "start"})

        cmd = self._resolve_command(session_id=session_id, user_text=user_text)

        # Use a subprocess so cancellation can terminate the run.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self._repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _wait_cancel() -> None:
            await cancel.cancelled()
            if proc.returncode is None:
                proc.terminate()

        cancel_task = asyncio.create_task(_wait_cancel())

        try:
            assert proc.stdout is not None
            assert proc.stderr is not None

            # Stream stdout as chunks; OpenClaw CLI prints JSON at end, but we still forward
            # incremental output to preserve some streaming behavior.
            while True:
                if cancel.is_cancelled():
                    break
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                if text:
                    yield AdapterEvent("stream.chunk", {"text": text})

            rc = await proc.wait()
            if cancel.is_cancelled():
                return

            if rc != 0:
                err = (await proc.stderr.read()).decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenClaw CLI failed (exit {rc}). stderr:\n{err}".strip())

        finally:
            cancel_task.cancel()

        yield AdapterEvent("event.skill", {"name": "openclaw.cli", "phase": "end"})


class AnthropicSkillsAdapter(OpenClawAdapter):
    """
    Run a turn using Anthropic Messages API with tool_use, where tools are derived from
    OpenClaw/Anthropic-style skills directories.

    Enabled when ANTHROPIC_API_KEY or MINIMAX_API_KEY is set (Anthropic-compatible Messages API).
    """

    def __init__(self, *, user_config: Optional[object] = None) -> None:
        from ..agent_runtime.anthropic_client import AnthropicClient
        from ..agent_runtime.anthropic_runtime import AnthropicAgentRuntime
        from ..skills.executor import SkillsExecutor
        from ..skills.registry import SkillsRegistry

        self._rt = AnthropicAgentRuntime(
            client=AnthropicClient.from_env(),
            skills=SkillsRegistry.from_env(),
            executor=SkillsExecutor(),
            user_config=user_config,
        )

    async def run(
        self,
        *,
        session_id: str,
        turn_id: str,
        run_id: str,
        user_text: str,
        history: list[str],
        cancel: CancelToken,
        user_id: str = "default",
    ) -> AsyncIterator[AdapterEvent]:
        yield AdapterEvent("event.skill", {"name": "anthropic.runtime", "phase": "start"})
        async for ev in self._rt.run_turn(session_id=session_id, user_text=user_text, cancel=cancel, user_id=user_id):
            yield ev
        yield AdapterEvent("event.skill", {"name": "anthropic.runtime", "phase": "end"})

