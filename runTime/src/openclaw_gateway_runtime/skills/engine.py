"""
Deterministic SKILL.md execution engine.

Parses structured ``## Steps`` YAML blocks from SKILL.md body and executes
them programmatically — no LLM round-trips per step.  Falls back gracefully
when a skill has no Steps section (returns ``None`` from ``parse_steps``).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Awaitable

import yaml  # PyYAML — already a project dependency

from ..runtime.openclaw_adapter import AdapterEvent, CancelToken
from ..infra.structured_log import log_event
from .executor import ScriptResult

if TYPE_CHECKING:
    from .registry import Skill, SkillsRegistry
    from ..agent_runtime.browser_client import BrowserManager


@dataclass
class StepDef:
    id: str
    action: str  # "exec" | "browser"
    command: str | None = None
    browser_action: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    params: list[str] = field(default_factory=list)
    defaults: dict[str, str] = field(default_factory=dict)
    timeout: int = 60
    on_error: str | None = None
    max_retries: int = 0
    # Which step group this belongs to (e.g. "search" or "detail")
    group: str | None = None


@dataclass
class StepResult:
    step_id: str
    success: bool
    output: Any = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_STEPS_HEADER_RE = re.compile(r"^##\s+Steps\b", re.IGNORECASE)
_YAML_FENCE_RE = re.compile(r"^```ya?ml\s*$", re.IGNORECASE)
_FENCE_END_RE = re.compile(r"^```\s*$")


def _extract_steps_yaml(body: str) -> str | None:
    """Return the raw YAML text inside the first ``## Steps`` fenced block."""
    lines = body.splitlines(keepends=True)
    in_section = False
    in_fence = False
    yaml_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not in_section:
            if _STEPS_HEADER_RE.match(stripped):
                in_section = True
            continue

        # Inside ## Steps section
        if stripped.startswith("## ") and not _STEPS_HEADER_RE.match(stripped):
            break  # next section

        if not in_fence:
            if _YAML_FENCE_RE.match(stripped):
                in_fence = True
            continue

        if _FENCE_END_RE.match(stripped):
            break

        yaml_lines.append(line)

    return "".join(yaml_lines).strip() or None


def parse_steps(skill_body: str) -> list[StepDef] | None:
    """Parse ``## Steps`` YAML from a SKILL.md body.  Returns *None* if absent."""
    raw = _extract_steps_yaml(skill_body)
    if raw is None:
        return None

    data = yaml.safe_load(raw)
    if not isinstance(data, list):
        return None

    steps: list[StepDef] = []
    for item in data:
        if not isinstance(item, dict) or "id" not in item:
            continue
        steps.append(StepDef(
            id=str(item["id"]),
            action=str(item.get("action", "exec")),
            command=item.get("command"),
            browser_action=item.get("browser_action"),
            args=item.get("args") or {},
            params=item.get("params") or [],
            defaults=item.get("defaults") or {},
            timeout=int(item.get("timeout", 60)),
            on_error=item.get("on_error"),
            max_retries=int(item.get("max_retries", 0)),
            group=item.get("group"),
        ))
    return steps if steps else None


# ---------------------------------------------------------------------------
# Variable resolution
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"\{(\w+)\}")
_REF_RE = re.compile(r"\$\{(\w+)\.([^}]+)\}")


def _deep_get(obj: Any, path: str) -> Any:
    """Traverse *obj* by dot/bracket path, e.g. ``result[0].url``."""
    parts = re.split(r"\.|\[(\d+)\]", path)
    cur = obj
    for p in parts:
        if p is None or p == "":
            continue
        if isinstance(cur, (list, tuple)):
            cur = cur[int(p)]
        elif isinstance(cur, dict):
            cur = cur.get(p, cur.get(int(p) if p.isdigit() else p))
        else:
            return None
    return cur


def resolve_vars(
    template: str,
    params: dict[str, str],
    context: dict[str, StepResult],
    defaults: dict[str, str] | None = None,
) -> str:
    """Replace ``{param}`` and ``${step_id.path}`` references in *template*."""
    defaults = defaults or {}

    def _param_sub(m: re.Match) -> str:
        key = m.group(1)
        return str(params.get(key, defaults.get(key, m.group(0))))

    def _ref_sub(m: re.Match) -> str:
        step_id = m.group(1)
        path = m.group(2)
        sr = context.get(step_id)
        if sr is None:
            return m.group(0)
        # Build a virtual object so paths like "result[0].url" or "output" work
        wrapper = {"result": sr.output, "output": sr.output}
        val = _deep_get(wrapper, path)
        if val is None:
            return m.group(0)
        return str(val) if not isinstance(val, str) else val

    result = _PARAM_RE.sub(_param_sub, template)
    result = _REF_RE.sub(_ref_sub, result)
    return result


def resolve_args(
    args: dict[str, Any],
    params: dict[str, str],
    context: dict[str, StepResult],
    defaults: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve variables in all string values of *args*."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            out[k] = resolve_vars(v, params, context, defaults)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------

class SkillEngine:
    """Deterministic SKILL.md step executor."""

    def __init__(
        self,
        skills: "SkillsRegistry",
    ) -> None:
        self._skills = skills

    def try_parse(self, skill: "Skill") -> list[StepDef] | None:
        return parse_steps(skill.body_md)

    async def execute(
        self,
        *,
        skill: "Skill",
        steps: list[StepDef],
        params: dict[str, str],
        session_id: str,
        cancel: CancelToken,
        group: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> AsyncIterator[AdapterEvent]:
        """Execute *steps* deterministically, yielding events.

        If *group* is given, only steps whose ``group`` matches are executed
        (plus their error handlers).  Steps with no group are always executed.
        """
        # Filter steps by group
        if group:
            active_steps = [s for s in steps if s.group == group or s.group is None]
        else:
            # If no group specified but steps have groups, default to the first group
            # (usually "search"). This prevents accidentally running all groups
            # (e.g. search + add_to_cart) when LLM forgets to specify action.
            groups_in_steps = [s.group for s in steps if s.group is not None]
            if groups_in_steps:
                first_group = groups_in_steps[0]
                active_steps = [s for s in steps if s.group == first_group or s.group is None]
            else:
                active_steps = list(steps)

        context: dict[str, StepResult] = {}
        error_handlers = {s.on_error: s for s in active_steps if s.on_error}

        for step in active_steps:
            if step.on_error:
                continue  # error handlers are invoked on demand, not sequentially
            if cancel.is_cancelled():
                return

            # Progress
            if on_progress:
                await on_progress(f"skill.{step.id}")

            yield AdapterEvent(
                "event.tool",
                {"name": f"skill.{step.id}", "phase": "start"},
            )

            # Execute step with periodic keepalive signals every 5 seconds
            _keepalive_phrases = [
                "在查了~\n", "还在跑~\n", "快出来了~\n", "再等等~\n",
                "正在搜~\n", "数据还没回来~\n", "网络有点慢~\n", "马上有结果~\n",
                "在处理~\n", "差不多了~\n", "还在加载~\n", "快好了~\n",
                "耐心等下~\n", "正在对比~\n", "信息快出来了~\n", "还在拉数据~\n",
                "系统在忙~\n", "就快好了~\n", "结果快出来了~\n", "最后一步了~\n",
            ]
            step_task = asyncio.ensure_future(
                self._exec_step(skill, step, params, context, session_id)
            )
            _ka_idx = 0
            while not step_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(step_task), timeout=20.0)
                except asyncio.TimeoutError:
                    if not step_task.done():
                        phrase = _keepalive_phrases[_ka_idx % len(_keepalive_phrases)]
                        _ka_idx += 1
                        yield AdapterEvent(
                            "stream.chunk",
                            {"text": phrase, "display_only": False, "content_type": "text"},
                        )
            result = step_task.result()
            context[step.id] = result
            log_event("engine", "step_done", session_id=session_id,
                      step=step.id, success=result.success,
                      error=result.error, output_len=len(str(result.output or "")))

            # Error handling with retry
            if not result.success and step.id in error_handlers:
                handler = error_handlers[step.id]
                for attempt in range(handler.max_retries):
                    if cancel.is_cancelled():
                        return
                    h_result = await self._exec_step(skill, handler, params, context, session_id)
                    if h_result.success:
                        # Retry the original step
                        result = await self._exec_step(skill, step, params, context, session_id)
                        context[step.id] = result
                        if result.success:
                            break

            yield AdapterEvent(
                "event.tool",
                {
                    "name": f"skill.{step.id}",
                    "phase": "result",
                    "success": result.success,
                    "output_preview": _preview(result.output),
                    "error": result.error,
                },
            )

            if not result.success:
                # Yield failure result so LLM knows the skill failed
                # Do NOT stream the raw error to the user — let LLM format it
                yield AdapterEvent("_engine_result", {"output": (
                    f"[SKILL FAILED] Step '{step.id}' failed: {result.error}\n"
                    f"Tell the user casually and warmly that it didn't work out. "
                    f"Do NOT use formal/robotic language. Keep it short (1-2 sentences). "
                    f"Suggest they can try manually on the platform. "
                    f"Do NOT start a new search or trigger any other skill."
                ), "context": {
                    k: {"success": v.success, "output": v.output, "error": v.error}
                    for k, v in context.items()
                }})
                return  # stop on unrecoverable error

        # Collect final output from all steps
        final_output = self._collect_output(active_steps, context)

        # If output contains pre-formatted product listings (with <link>/<pic> tags),
        # send the listing directly as a <detail> block to the client.
        # This bypasses LLM formatting and ensures <pic> tags are never omitted.
        if "<pic>" in final_output or "<link>" in final_output:
            # Extract the display portion (before ---internal---)
            display_part = final_output.split("\n---internal---")[0].strip()
            if display_part:
                # Find the [step_id] header and remove it
                lines = display_part.split("\n")
                content_lines = [l for l in lines if not l.startswith("[") or l[0:1].isdigit()]
                if not content_lines and lines:
                    content_lines = lines[1:]  # skip [step_id] header
                detail_content = "\n".join(content_lines).strip()
                if detail_content:
                    yield AdapterEvent("stream.chunk", {
                        "text": f"<detail>{detail_content}</detail>",
                        "display_only": True,
                        "content_type": "detail",
                    })

            # Strip <pic> and <link> from LLM output (detail already sent to client directly)
            _tag_strip = re.compile(r"\s*<(?:pic|link)>.*?</(?:pic|link)>")
            final_output = _tag_strip.sub("", final_output)

        yield AdapterEvent("_engine_result", {"output": final_output, "context": {
            k: {"success": v.success, "output": v.output, "error": v.error}
            for k, v in context.items()
        }})

    async def _exec_step(
        self,
        skill: "Skill",
        step: StepDef,
        params: dict[str, str],
        context: dict[str, StepResult],
        session_id: str,
    ) -> StepResult:
        try:
            if step.action == "exec":
                return await self._exec_shell(skill, step, params, context)
            elif step.action == "browser":
                return await self._exec_browser(skill, step, params, context, session_id)
            else:
                return StepResult(step.id, False, error=f"unknown action: {step.action}")
        except Exception as e:
            return StepResult(step.id, False, error=str(e))

    async def _exec_shell(
        self,
        skill: "Skill",
        step: StepDef,
        params: dict[str, str],
        context: dict[str, StepResult],
    ) -> StepResult:
        cmd = step.command
        if not cmd:
            return StepResult(step.id, False, error="no command specified")
        cmd = resolve_vars(cmd, params, context, step.defaults)
        # Built-in retry: retry once on transient failures (API errors, timeouts)
        max_attempts = 2
        res: ScriptResult | None = None
        for attempt in range(max_attempts):
            res = await asyncio.to_thread(
                self._skills.exec_command,
                command=cmd,
                cwd=str(skill.root_dir),
                timeout_s=step.timeout,
            )
            if res.exit_code == 0 and res.stdout and res.stdout.strip():
                # Heuristic: if output has fewer than 2 lines and no comma,
                # it's likely an API error message, not real data — retry.
                out = res.stdout.strip()
                lines = out.splitlines()
                if len(lines) >= 2 or "," in out:
                    return StepResult(step.id, True, output=res.stdout)
                # Single-line non-CSV output — possible transient API error
                log_event("engine", "shell_retry", step=step.id, attempt=attempt,
                          reason="suspicious_output", preview=out[:120])
            if attempt < max_attempts - 1:
                await asyncio.sleep(1.5)
        assert res is not None
        if res.exit_code != 0:
            return StepResult(step.id, False, output=res.stdout, error=res.stderr or f"exit {res.exit_code}")
        return StepResult(step.id, True, output=res.stdout)

    async def _exec_browser(
        self,
        skill: "Skill",
        step: StepDef,
        params: dict[str, str],
        context: dict[str, StepResult],
        session_id: str,
    ) -> StepResult:
        from ..agent_runtime.browser_client import BrowserManager

        bm = BrowserManager.get()
        action = step.browser_action
        if not action:
            return StepResult(step.id, False, error="no browser_action specified")

        args = resolve_args(step.args, params, context, step.defaults)

        txt: str
        if action == "navigate":
            txt = await bm.navigate(session_id, url=str(args.get("url", "")))
        elif action == "click":
            sel = args.get("selector")
            ref = args.get("ref")
            txt = await bm.click(
                session_id,
                selector=str(sel) if sel else None,
                ref=int(ref) if ref is not None else None,
            )
        elif action == "type":
            txt = await bm.type_text(
                session_id,
                text=str(args.get("text", "")),
                selector=str(args["selector"]) if args.get("selector") else None,
                ref=int(args["ref"]) if args.get("ref") is not None else None,
                clear_first=bool(args.get("clear_first")),
            )
        elif action == "evaluate":
            script = str(args.get("script", ""))
            # Support loading script from file (relative to skill root)
            script_file = args.get("script_file")
            if script_file:
                import pathlib
                sf = pathlib.Path(str(skill.root_dir)) / str(script_file)
                if sf.exists():
                    script = sf.read_text(encoding="utf-8")
            # Resolve variables in script content (e.g. INJECT_TITLE → keyword)
            script = resolve_vars(script, params, context, step.defaults)
            txt = await bm.evaluate(session_id, script=script)
        elif action == "set_cookies_file":
            txt = await bm.set_cookies_from_file(session_id, file_path=str(args.get("file_path", "")))
        elif action == "set_cookies":
            txt = await bm.set_cookies(session_id, cookies=args.get("cookies", []))
        elif action == "wait":
            sel = args.get("selector")
            ts = args.get("timeout_s")
            txt = await bm.wait(
                session_id,
                selector=str(sel) if sel else None,
                timeout_s=int(ts) if ts is not None else None,
            )
        elif action == "snapshot":
            txt = await bm.snapshot(session_id)
        elif action == "screenshot":
            txt = await bm.screenshot(session_id, full_page=bool(args.get("full_page")))
        else:
            return StepResult(step.id, False, error=f"unknown browser action: {action}")

        # Try to parse JSON output (e.g. from evaluate)
        output: Any = txt
        try:
            output = json.loads(txt)
        except (json.JSONDecodeError, TypeError):
            pass

        is_err = isinstance(txt, str) and any(
            txt.startswith(p) for p in (
                "navigate failed:", "click failed:", "type failed:",
                "evaluate failed:", "set_cookies failed:", "wait failed:",
                "snapshot failed:", "screenshot failed:",
            )
        )
        if is_err:
            return StepResult(step.id, False, output=output, error=txt)
        # Check for application-level error in JSON output (e.g. captcha detection)
        if isinstance(output, dict) and output.get("error"):
            return StepResult(step.id, False, output=output, error=str(output.get("message") or output["error"]))
        return StepResult(step.id, True, output=output)

    def _collect_output(
        self,
        steps: list[StepDef],
        context: dict[str, StepResult],
    ) -> str:
        """Build a summary of all step outputs for LLM formatting."""
        parts: list[str] = []
        for step in steps:
            if step.on_error:
                continue
            sr = context.get(step.id)
            if sr is None:
                continue
            out = sr.output
            if out is None:
                continue
            if isinstance(out, (dict, list)):
                out = json.dumps(out, ensure_ascii=False, indent=2)
            parts.append(f"[{step.id}]\n{out}")
        return "\n\n".join(parts)


def _preview(output: Any, max_len: int = 200) -> str:
    """Short preview of step output for event payload."""
    if output is None:
        return ""
    if isinstance(output, (dict, list)):
        s = json.dumps(output, ensure_ascii=False)
    else:
        s = str(output)
    return s[:max_len] + ("..." if len(s) > max_len else "")
