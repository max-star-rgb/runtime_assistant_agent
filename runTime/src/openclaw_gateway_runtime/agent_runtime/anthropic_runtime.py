from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict

from ..runtime.openclaw_adapter import AdapterEvent, CancelToken
from ..infra.structured_log import log_event
from ..skills.engine import SkillEngine, StepDef
from ..skills.executor import SkillsExecutor
from ..skills.registry import SkillsRegistry
from .anthropic_client import AnthropicClient, AnthropicError
from .anthropic_sse import AnthropicStreamBuilder
from .ddg_search_client import web_search as ddg_web_search
from .memory_client import MemoryEntry, get_memory_client
from .price_compare_client import call_price_compare_service
from .serper_batch_client import batch_web_search_queries


# ---------------------------------------------------------------------------
# DetailTagParser: streaming parser for <detail>...</detail> and <link>...</link>
# ---------------------------------------------------------------------------

_OPEN_TAG = "<detail>"
_CLOSE_TAG = "</detail>"
_LINK_OPEN = "<link>"
_LINK_CLOSE = "</link>"
_PIC_OPEN = "<pic>"
_PIC_CLOSE = "</pic>"

# Hallucinated tokens that some models emit — strip before processing
_HALLUCINATED_TOKENS = re.compile(
    r"<end_turn[^>]*/>"          # <end_turn expects_reply="true" />
    r"|</?end_turn[^>]*>"        # <end_turn> or </end_turn> with any attrs
    r"|<\|end\|>"
    r"|<\|im_end\|>"
    r"|end_turn\([^)]*\)"        # end_turn(expects_reply=true)
    r"|\[?\{\"id\":\"call_[^}]+\}[,\]\s]*"  # leaked function call JSON
)

# Tag definitions: (open_tag, close_tag, display_only, content_type, strip_tags)
# strip_tags=True: remove the tags, emit only inner content
# strip_tags=False: keep tags in output text
_TAGS = [
    (_OPEN_TAG, _CLOSE_TAG, True, "detail", False),
    (_LINK_OPEN, _LINK_CLOSE, True, "link", False),
    (_PIC_OPEN, _PIC_CLOSE, True, "pic", False),
]


class DetailTagParser:
    """
    Stateful streaming parser that splits text into (chunk, display_only, content_type) tuples.

    - Text outside any tags: display_only=False, content_type="text" (sent to TTS)
    - Text inside <detail>...</detail>: display_only=True, content_type="detail"
    - Text inside <link>...</link>: display_only=True, content_type="link"

    Handles tags split across multiple streaming chunks via an internal buffer.
    """

    def __init__(self) -> None:
        self._state: str = "text"  # "text" | "detail" | "link"
        self._buf = ""
        self._strip_tags = True

    def feed(self, text: str) -> list[tuple[str, bool, str]]:
        """Feed a streaming chunk; return list of (text, display_only, content_type) segments."""
        # Strip hallucinated stop tokens before buffering
        text = _HALLUCINATED_TOKENS.sub("", text)
        if not text:
            return []
        self._buf += text
        segments: list[tuple[str, bool, str]] = []

        while self._buf:
            if self._state != "text":
                # Inside a tag — look for the matching close tag
                close_tag = next(ct for ot, ct, _, ctype, _ in _TAGS if ctype == self._state)
                strip = self._strip_tags
                idx = self._buf.find(close_tag)
                if idx != -1:
                    inner = self._buf[:idx]
                    if strip:
                        if inner:
                            segments.append((inner, True, self._state))
                    else:
                        # Keep close tag in output
                        segments.append((inner + close_tag, True, self._state))
                    self._buf = self._buf[idx + len(close_tag):]
                    self._state = "text"
                    self._strip_tags = True
                else:
                    safe, held = self._split_partial(self._buf, close_tag)
                    if safe:
                        segments.append((safe, True, self._state))
                    self._buf = held
                    break
            else:
                # Outside tags — look for the nearest opening tag
                best_idx = len(self._buf)
                best_tag = None
                for open_tag, _, _, content_type, strip in _TAGS:
                    idx = self._buf.find(open_tag)
                    if idx != -1 and idx < best_idx:
                        best_idx = idx
                        best_tag = (open_tag, content_type, strip)

                if best_tag is not None:
                    if best_idx > 0:
                        segments.append((self._buf[:best_idx], False, "text"))
                    self._strip_tags = best_tag[2]
                    if self._strip_tags:
                        # Remove open tag from output
                        self._buf = self._buf[best_idx + len(best_tag[0]):]
                    else:
                        # Keep open tag in output — it will be part of the next segment
                        self._buf = self._buf[best_idx:]
                        # Emit the open tag as start of the link segment
                        self._buf = self._buf[len(best_tag[0]):]
                        segments.append((best_tag[0], True, best_tag[1]))
                    self._state = best_tag[1]
                else:
                    # No open tag found — check for partial tags at the end
                    safe = self._buf
                    held = ""
                    for open_tag, _, _, _, _ in _TAGS:
                        s, h = self._split_partial(safe, open_tag)
                        if h:
                            safe = s
                            held = h
                            break
                    if safe:
                        segments.append((safe, False, "text"))
                    self._buf = held
                    break

        return segments

    def flush(self) -> list[tuple[str, bool, str]]:
        """Flush remaining buffer (call at end of stream)."""
        if not self._buf:
            return []
        display_only = self._state != "text"
        seg = [(self._buf, display_only, self._state)]
        self._buf = ""
        return seg

    @staticmethod
    def _split_partial(text: str, tag: str) -> tuple[str, str]:
        """
        If *text* ends with a prefix of *tag*, hold that suffix back.
        Returns (safe_to_emit, held_back).
        """
        for i in range(1, min(len(tag), len(text)) + 1):
            if tag.startswith(text[-i:]):
                return text[:-i], text[-i:]
        return text, ""


# ---------------------------------------------------------------------------
# SentenceBuffer: accumulate TTS text and split on punctuation boundaries
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[，。！？,!\?\n])(?![a-zA-Z0-9:/._\-])")


class SentenceBuffer:
    """
    Buffers incoming text segments and splits on sentence-ending punctuation
    so that each emitted chunk is a complete clause for TTS.

    Only buffers content_type="text" with display_only=False (TTS-bound text).
    Other types (detail, link) are passed through immediately.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._display_only = False
        self._content_type = "text"

    def feed(self, text: str, display_only: bool, content_type: str) -> list[tuple[str, bool, str]]:
        # Flush any buffered text from a different mode before switching
        if display_only != self._display_only or content_type != self._content_type:
            out = self._flush_buf()
            self._display_only = display_only
            self._content_type = content_type
        else:
            out = []

        self._buf += text

        # Links, pics, and detail: buffer entirely, emit only on flush (when type switches or stream ends)
        if content_type in ("link", "pic", "detail"):
            return out

        out.extend(self._try_split())
        return out

    def flush(self) -> list[tuple[str, bool, str]]:
        """Flush remaining buffer at end of stream."""
        return self._flush_buf()

    def _try_split(self) -> list[tuple[str, bool, str]]:
        parts = _SENTENCE_SPLIT_RE.split(self._buf)
        if len(parts) <= 1:
            return []  # no punctuation found yet, keep buffering
        out: list[tuple[str, bool, str]] = []
        for part in parts[:-1]:
            if part:
                out.append((part, self._display_only, self._content_type))
        self._buf = parts[-1]
        return out

    def _flush_buf(self) -> list[tuple[str, bool, str]]:
        if not self._buf:
            return []
        out = [(self._buf, self._display_only, self._content_type)]
        self._buf = ""
        return out


def _price_compare_tool_enabled() -> bool:
    return bool((os.environ.get("PRICE_COMPARE_SERVICE_URL") or "").strip())


def _batch_web_search_tool_enabled() -> bool:
    return bool((os.environ.get("SERPER_API_KEY") or "").strip())


def _iter_assistant_text(content: Any) -> str:
    """
    Extract best-effort assistant text from Anthropic `content` blocks.
    """
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _find_tool_uses(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append(block)
    return out


@dataclass
class SessionState:
    # Anthropic messages list; we keep it minimal and deterministic.
    messages: list[dict[str, Any]]
    # Skill-lock: persists across turns until the skill workflow completes.
    active_skill: str | None = None
    # Whether any tool was invoked during the most recent run_turn.
    # Used to decide whether the skill lock should persist into the next turn:
    # a tool-free turn means the skill workflow is done.
    tool_used_this_turn: bool = False
    # User identifier for cross-session memory.
    user_id: str = "default"
    # Whether long-term memory has been loaded for the current turn.
    memory_loaded: bool = False


class AnthropicAgentRuntime:
    """
    OpenClaw-like skills experience on top of Anthropic tool_use loop:
    - embed an <available_skills> block in system prompt (progressive disclosure)
    - tools are generic (read_file, list_dir, grep, http_get, exec), not one tool per skill
    - call /v1/messages
    - execute tool_use blocks (read_file / exec)
    - send tool_result blocks back and continue (multi-step)

    Notes:
    - We do NOT use SSE streaming yet; instead we emit chunks by slicing final text.
    - Cancellation is checked between network calls and tool executions.
    """

    def __init__(self, *, client: AnthropicClient, skills: SkillsRegistry, executor: SkillsExecutor, user_config: object = None) -> None:
        self._client = client
        self._skills = skills
        self._executor = executor
        self._user_config = user_config  # Optional[UserConfig] from gateway
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()
        self._engine = SkillEngine(skills)

    # ------------------------------------------------------------------
    # Skill-lock helpers
    # ------------------------------------------------------------------

    def _detect_skill_from_path(self, file_path: str) -> str | None:
        """If *file_path* is a registered skill's SKILL.md, return the skill id; else None."""
        from pathlib import Path

        p = Path(file_path)
        if p.name != "SKILL.md":
            return None
        for skill in self._skills.list():
            try:
                skill_md_path = (skill.root_dir / "SKILL.md").resolve()
                candidate = p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
                if candidate == skill_md_path:
                    return skill.id
            except (OSError, ValueError):
                continue
        return None

    def _check_skill_lock(self, session_id: str, target_skill: str) -> str | None:
        """Return an error message if another skill is already active, else None."""
        state = self._sessions.get(session_id)
        if state is None or state.active_skill is None:
            return None
        if state.active_skill == target_skill:
            return None
        return (
            f"Skill lock: skill '{state.active_skill}' is currently active. "
            f"You MUST complete it before switching to '{target_skill}'. "
            f"Continue using the current skill's instructions."
        )

    def _set_active_skill(self, session_id: str, skill_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is not None and state.active_skill is None:
            state.active_skill = skill_id

    def _system_prompt(
        self,
        session_id: str | None = None,
        memories: list[dict] | None = None,
    ) -> str:
        """
        OpenClaw-style progressive disclosure:
        - model sees a curated list of skills (name/description/location/baseDir)
        - when needed, it reads SKILL.md (and references) via read_file tool
        - it executes scripts via exec tool, typically using {baseDir} from SKILL.md

        If a skill is currently active (cross-turn lock), appends a context
        reminder so the model knows to continue with that skill.

        If *memories* are provided (from long-term memory store), they are
        appended in a <user_memory> block so the model can leverage them.
        """
        skills_prompt = self._skills.format_available_skills_xml()
        lines: list[str] = []
        if _price_compare_tool_enabled():
            lines.append(
                "When the user wants cross-platform (JD / Taobao / Pinduoduo) price comparison, use tool "
                "`price_compare` with a clear product query. Reply with each offer's landing_url; the user opens "
                "those links in the official apps to add to cart (no automated cart on servers)."
            )
        lines.append(
            "Use `web_search` (DuckDuckGo, no key) for general web lookups (weather, news, etc.)."
        )
        lines.append(
            "Use `browser` for tasks that require interacting with web pages: "
            "navigate to a URL, then call snapshot to get an accessibility tree with [ref] numbers, "
            "then use click/type/select with those refs. "
            "Prefer web_search or web_fetch for simple lookups; use browser only when page interaction is needed.\n"
            "IMPORTANT: For shopping, price comparison, or product search tasks, ALWAYS use the relevant skill first "
            "(e.g. taobao skill for product search/comparison). Only use browser as a fallback if the skill fails or "
            "for actions the skill cannot perform (e.g. adding to cart with cookies).\n"
            "Browser best practices:\n"
            "- NEVER click '反馈','客服','举报','帮助' links.\n"
            "- For e-commerce, use `evaluate` to extract product data, then `navigate` to product URL.\n"
            "  JD search: `() => [...document.querySelectorAll('[data-sku]')].slice(0,5).map(el => "
            "({title: el.querySelector('[title]')?.getAttribute('title'), "
            "url: 'https://item.jd.com/' + el.getAttribute('data-sku') + '.html'}))`\n"
            "  JD add to cart: click selector `#add-to-cart`\n"
            "  JD cart page: navigate to `https://cart.jd.com/cart.action`"
        )
        if _batch_web_search_tool_enabled():
            lines.append(
                "You may also use `batch_web_search` (Serper) for multi-query batch searches."
            )
        pc_hint = ("\n".join(lines) + "\n") if lines else ""

        # Load prompt template from file
        extra_tools = ""
        if _price_compare_tool_enabled():
            extra_tools += ", price_compare"
        if _batch_web_search_tool_enabled():
            extra_tools += ", batch_web_search"

        import pathlib
        prompts_dir = pathlib.Path(__file__).parent / "prompts"
        # Language priority: user_config.language (from call.incoming) > env PROMPT_LANG > default "zh"
        lang = os.environ.get("PROMPT_LANG", "zh").strip().lower()
        if self._user_config is not None and hasattr(self._user_config, "language"):
            cfg_lang = str(getattr(self._user_config, "language", "")).strip().lower()
            if cfg_lang:
                # Normalize: "zh-cn" / "zh" → "zh", "en-us" / "en" → "en"
                lang = cfg_lang.split("-")[0] or lang
        template_file = prompts_dir / f"system_{lang}.md"
        if not template_file.exists():
            template_file = prompts_dir / "system_zh.md"
        log_event("runtime", "prompt_lang", lang=lang, source="user_config" if (self._user_config and hasattr(self._user_config, "language")) else "env")

        template = template_file.read_text(encoding="utf-8")
        prompt = template.format(
            pc_hint=pc_hint,
            extra_tools=extra_tools,
            skills_prompt=skills_prompt,
        )

        # Inject active-skill context reminder for cross-turn continuity.
        if session_id:
            state = self._sessions.get(session_id)
            if state and state.active_skill:
                skill = self._skills.get(state.active_skill)
                if skill:
                    prompt += (
                        f"\n[Active skill: {skill.id}] You are currently in the middle of "
                        f"executing this skill. Continue following its instructions. "
                        f"Its SKILL.md is at: {skill.root_dir / 'SKILL.md'} "
                        f"(baseDir: {skill.root_dir}). "
                        f"Do NOT start a different skill.\n"
                    )

        # Inject long-term memories retrieved from the memory store.
        if memories:
            prompt += "\n<user_memory>\n"
            for m in memories:
                prompt += f"- [{m.get('type', 'fact')}] {m.get('content', '')}\n"
            prompt += "</user_memory>\n"

        # Inject user config (language, user_info) from telephony scenario.
        if self._user_config is not None:
            cfg = self._user_config
            lang = getattr(cfg, "language", None)
            user_info = getattr(cfg, "user_info", None)
            tone = getattr(cfg, "tone", None)
            parts: list[str] = []
            if lang and lang != "zh-CN":
                parts.append(f"IMPORTANT: Respond in language '{lang}' (user preference).")
            if user_info:
                parts.append(f"User info: {user_info}")
            if tone and tone != "friendly":
                parts.append(f"Tone: {tone}")
            if parts:
                prompt += "\n<user_config>\n" + "\n".join(parts) + "\n</user_config>\n"

        return prompt

    def _tools(self, *, user_id: str = "default") -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {
                "name": "read",
                "description": "Read a text file. Use offset/limit for partial reads on large files.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute or workspace-relative path."},
                        "offset": {"type": "integer", "description": "1-based line to start from.", "minimum": 1},
                        "limit": {"type": "integer", "description": "Max lines to return.", "minimum": 1},
                    },
                    "required": ["file_path"],
                },
            },
            {
                "name": "write",
                "description": "Create or overwrite a file with the given content.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute or workspace-relative path."},
                        "content": {"type": "string", "description": "Full file content to write."},
                    },
                    "required": ["file_path", "content"],
                },
            },
            {
                "name": "edit",
                "description": "Replace one unique occurrence of old_string with new_string in a file.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Absolute or workspace-relative path."},
                        "old_string": {"type": "string", "description": "Exact text to find (must be unique)."},
                        "new_string": {"type": "string", "description": "Replacement text."},
                    },
                    "required": ["file_path", "old_string", "new_string"],
                },
            },
            {
                "name": "list_dir",
                "description": "List files and subdirectories in one folder (non-recursive).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_entries": {"type": "integer", "minimum": 1, "default": 200},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "grep",
                "description": "Regex search in a file or recursively under a directory.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                        "max_matches": {"type": "integer", "minimum": 1, "default": 80},
                    },
                    "required": ["pattern", "path"],
                },
            },
            {
                "name": "web_fetch",
                "description": "HTTP GET a public URL; returns body as text.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_bytes": {"type": "integer", "minimum": 1, "default": 500000},
                        "timeout_s": {"type": "integer", "minimum": 1, "default": 30},
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "web_search",
                "description": (
                    "Search the web using DuckDuckGo. Returns titles, URLs, and snippets. "
                    "No API key required. Use site: operators for platform-specific results."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query string."},
                        "count": {
                            "type": "integer",
                            "description": "Number of results (1-20, default 5).",
                            "minimum": 1,
                            "maximum": 20,
                        },
                        "region": {
                            "type": "string",
                            "description": "Optional DuckDuckGo region code (e.g. us-en, cn-zh).",
                        },
                        "safeSearch": {
                            "type": "string",
                            "enum": ["strict", "moderate", "off"],
                            "description": "SafeSearch level (default moderate).",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "exec",
                "description": "Run a shell command. workdir must be under allowed roots.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to execute."},
                        "workdir": {"type": "string", "description": "Working directory (default: repo root)."},
                        "timeout": {"type": "integer", "minimum": 1, "default": 60, "description": "Timeout in seconds."},
                    },
                    "required": ["command"],
                },
            },
        ]
        tools.append(
            {
                "name": "browser",
                "description": (
                    "Control a browser. Actions: navigate, snapshot, screenshot, click, type, select, "
                    "set_cookies, set_cookies_file, get_cookies, wait, evaluate. "
                    "Use evaluate to run JS and extract structured data (e.g. product lists) — "
                    "more reliable than snapshot+click for e-commerce pages."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["navigate", "snapshot", "screenshot", "click", "type", "select",
                                     "set_cookies", "set_cookies_file", "get_cookies", "wait", "evaluate"],
                            "description": "The browser action to perform.",
                        },
                        "url": {
                            "type": "string",
                            "description": "(navigate) URL to open.",
                        },
                        "wait_until": {
                            "type": "string",
                            "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                            "description": "(navigate) When to consider navigation done. Default: load.",
                        },
                        "selector": {
                            "type": "string",
                            "description": "(click/type/select/wait) CSS selector.",
                        },
                        "ref": {
                            "type": "integer",
                            "description": "(click/type/select) Ref number from a previous snapshot.",
                        },
                        "text": {
                            "type": "string",
                            "description": "(type) Text to enter.",
                        },
                        "clear_first": {
                            "type": "boolean",
                            "description": "(type) Clear the field before typing. Default: false.",
                        },
                        "full_page": {
                            "type": "boolean",
                            "description": "(screenshot) Capture full scrollable page. Default: false.",
                        },
                        "value": {
                            "type": "string",
                            "description": "(select) Option value to select.",
                        },
                        "label": {
                            "type": "string",
                            "description": "(select) Option label to select.",
                        },
                        "cookies": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "(set_cookies) Array of cookie objects: {name, value, domain, path?, ...}.",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "(set_cookies_file) Path to a cookies JSON file.",
                        },
                        "timeout_s": {
                            "type": "integer",
                            "description": "(wait) Timeout in seconds. Default: 10.",
                        },
                        "state": {
                            "type": "string",
                            "enum": ["visible", "hidden", "attached", "detached"],
                            "description": "(wait) Element state to wait for. Default: visible.",
                        },
                        "script": {
                            "type": "string",
                            "description": "(evaluate) JavaScript to run in page. Return value is JSON-serialized.",
                        },
                    },
                    "required": ["action"],
                },
            }
        )
        if _price_compare_tool_enabled():
            tools.append(
                {
                    "name": "price_compare",
                    "description": (
                        "Call your cloud price-compare API: returns JSON with offers and landing_url per platform "
                        "(JD / Taobao / PDD). User opens links in official apps to add to cart."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Product name, model, or keywords to search.",
                            },
                            "platforms": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["jd", "taobao", "pdd"]},
                                "description": "Optional; default all three.",
                            },
                        },
                        "required": ["query"],
                    },
                }
            )
        if _batch_web_search_tool_enabled():
            tools.append(
                {
                    "name": "batch_web_search",
                    "description": (
                        "Run several Google search queries via Serper (requires SERPER_API_KEY). "
                        "Returns JSON with organic results per query. Use for shopping research when no "
                        "price_compare API exists; prefer queries like 'site:jd.com 商品名' — not structured prices."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "queries": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "1–5 search strings (e.g. site:jd.com + product name).",
                            },
                        },
                        "required": ["queries"],
                    },
                }
            )
        # update_user_config: built-in tool for telephony hot-config (only when user is identified)
        if user_id and user_id != "default":
            tools.append(
                {
                    "name": "update_user_config",
                    "description": (
                        "Update a user preference or Agent behavior setting during the call. "
                        "Call this when the user explicitly asks to change language, tone, or other settings "
                        "(e.g. 'please switch to English', 'be more formal'). "
                        "Changes are persisted and take effect immediately and on future calls."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": "Config key to update: language | tone | agent_profile | user_info",
                            },
                            "value": {
                                "type": "string",
                                "description": "New value for the config key.",
                            },
                        },
                        "required": ["key", "value"],
                    },
                }
            )
        tools.append(
            {
                "name": "run_skill",
                "description": (
                    "Execute a skill action deterministically using its engine steps. "
                    "Use this ONLY after reading a SKILL.md that contains '## Steps'. "
                    "Extract the required parameters from the user's message and pass them here. "
                    "Use 'action' to select which step group to run (e.g. 'search' or 'add_to_cart')."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "description": "The skill name (e.g. 'taobao').",
                        },
                        "action": {
                            "type": "string",
                            "description": "Which action/step group to run (e.g. 'search', 'add_to_cart'). Omit to run all steps.",
                        },
                        "params": {
                            "type": "object",
                            "description": "Parameters extracted from user message. Keys must match the skill's declared params (e.g. keyword, source, goodsId).",
                        },
                    },
                    "required": ["skill_id", "params"],
                },
            }
        )
        tools.append(
            {
                "name": "end_turn",
                "description": (
                    "Signal whether the conversation turn is finished and the user does NOT need to reply. "
                    "You MUST call this tool with expects_reply=false when: "
                    "the user says thanks/goodbye/OK, you finished a task and are reporting the result, "
                    "or your response is a one-way notification. "
                    "Only set expects_reply=true if you are asking the user a question or need their input to continue."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "expects_reply": {
                            "type": "boolean",
                            "description": (
                                "false when the user does NOT need to reply "
                                "(thanks, goodbye, task completed, confirmation). "
                                "true only when you are asking a question or waiting for user input."
                            ),
                        },
                    },
                    "required": ["expects_reply"],
                },
            }
        )
        return tools

    MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS") or "25")
    HARD_MAX_TOOL_RESULT_CHARS = 400_000

    @staticmethod
    def _truncate_tool_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n...[truncated to {limit} chars]"

    # ------------------------------------------------------------------
    # Memory helpers
    # ------------------------------------------------------------------

    _MEMORY_PATTERNS: list[tuple[str, str]] = [
        ("preference", r"(?:我(?:喜欢|偏好|想要|习惯|常用|爱用|讨厌|不喜欢)|偏好[:：]|喜好[:：])(.{2,60})"),
        ("fact", r"(?:我(?:叫|是|住在|来自|在|的(?:名字|地址|电话|邮箱)))\s*(.{2,60})"),
        ("fact", r"(?:我的(?:预算|地址|手机号|邮箱|账号))(?:是|为|[:：])\s*(.{2,60})"),
        ("event", r"(?:我(?:刚|已经|昨天|今天|上次|之前)(?:买|购|下单|收到|退|换))(.{2,60})"),
    ]

    @staticmethod
    def _extract_memory_entries(text: str) -> list[MemoryEntry]:
        """Extract memorable facts/preferences/events from text using patterns."""
        import re
        entries: list[MemoryEntry] = []
        seen: set[str] = set()
        for mem_type, pattern in AnthropicAgentRuntime._MEMORY_PATTERNS:
            for m in re.finditer(pattern, text):
                content = m.group(0).strip()
                if content and content not in seen:
                    seen.add(content)
                    entries.append(MemoryEntry(
                        content=content,
                        type=mem_type,
                        importance=0.7,
                    ))
        return entries

    MEMORY_MAX_MESSAGES = int(os.environ.get("MEMORY_MAX_MESSAGES") or "20")

    @staticmethod
    def _compress_messages(messages: list[dict[str, Any]], threshold: int) -> list[dict[str, Any]]:
        """
        When the message list exceeds *threshold*, summarise the oldest half
        into a single system message and keep the recent half intact.
        """
        if len(messages) <= threshold:
            return messages
        mid = len(messages) // 2
        old_half = messages[:mid]
        recent_half = messages[mid:]
        parts: list[str] = []
        for msg in old_half:
            role = msg.get("role", "?")
            body = msg.get("content", "")
            if isinstance(body, str):
                text = body[:200]
            elif isinstance(body, list):
                text = _iter_assistant_text(body)[:200]
            else:
                text = str(body)[:200]
            parts.append(f"[{role}] {text}")
        summary_text = (
            "[Earlier conversation summary]\n" + "\n".join(parts)
        )
        summary_msg: dict[str, Any] = {
            "role": "user",
            "content": [
                {"type": "text", "text": summary_text},
            ],
        }
        return [summary_msg] + recent_half

    async     def run_turn(
        self,
        *,
        session_id: str,
        user_text: str,
        cancel: CancelToken,
        user_id: str = "default",
    ) -> AsyncIterator[AdapterEvent]:
        self._skills.refresh()
        tools = self._tools(user_id=user_id)

        log_event("runtime", "user_input", user_id=user_id, session_id=session_id, text=user_text[:200])

        # --- Memory: recall long-term memories for this user ---
        memories: list[dict] = []
        async with self._lock:
            state = self._sessions.setdefault(session_id, SessionState(messages=[]))
            state.user_id = user_id
        try:
            mc = get_memory_client()
            memories = await asyncio.to_thread(
                mc.recall, state.user_id, query=user_text
            )
        except Exception:
            pass

        system = self._system_prompt(session_id, memories=memories)

        async with self._lock:
            state = self._sessions[session_id]
            state.messages.append({"role": "user", "content": user_text})
            state.memory_loaded = True
            if not state.tool_used_this_turn:
                state.active_skill = None
            state.tool_used_this_turn = False
            # Compress old messages if the list exceeds the threshold.
            state.messages = self._compress_messages(
                state.messages, self.MEMORY_MAX_MESSAGES
            )
            messages_snapshot = list(state.messages)

        expects_reply = False  # default: conversation turn is complete
        for _round in range(self.MAX_TOOL_ROUNDS + 1):
            if cancel.is_cancelled():
                return

            builder = AnthropicStreamBuilder()
            payload = {
                "model": self._client._cfg.model,  # noqa: SLF001
                "max_tokens": self._client._cfg.max_tokens,  # noqa: SLF001
                "system": system,
                "messages": messages_snapshot,
                "tools": tools,
            }
            log_event("runtime", "llm_request", user_id=user_id, session_id=session_id,
                      round=_round, model=payload["model"], msg_count=len(messages_snapshot))
            detail_parser = DetailTagParser()
            sentence_buf = SentenceBuffer()
            async for ev in self._client.messages_create_stream(payload):
                if cancel.is_cancelled():
                    for out_text, out_do, out_type in sentence_buf.flush():
                        if out_text:
                            yield AdapterEvent("stream.chunk", {
                                "text": out_text, "display_only": out_do, "content_type": out_type,
                            })
                    return
                delta = builder.feed(ev)
                if delta:
                    for seg_text, seg_display_only, seg_type in detail_parser.feed(delta):
                        if seg_text:
                            for out_text, out_do, out_type in sentence_buf.feed(seg_text, seg_display_only, seg_type):
                                if out_text:
                                    yield AdapterEvent("stream.chunk", {
                                        "text": out_text,
                                        "display_only": out_do,
                                        "content_type": out_type,
                                    })

            # Flush remaining from detail_parser then sentence_buf
            for seg_text, seg_display_only, seg_type in detail_parser.flush():
                if seg_text:
                    for out_text, out_do, out_type in sentence_buf.feed(seg_text, seg_display_only, seg_type):
                        if out_text:
                            yield AdapterEvent("stream.chunk", {
                                "text": out_text,
                                "display_only": out_do,
                                "content_type": out_type,
                            })
            for out_text, out_do, out_type in sentence_buf.flush():
                if out_text:
                    yield AdapterEvent("stream.chunk", {
                        "text": out_text,
                        "display_only": out_do,
                        "content_type": out_type,
                    })

            resp = builder.response
            content = resp.get("content")
            stop_reason = resp.get("stop_reason")
            tool_uses = _find_tool_uses(content)

            # Log LLM response summary
            text_blocks = [b.get("text", "") for b in (content or []) if b.get("type") == "text"]
            llm_text = "".join(text_blocks)[:200]
            tool_names = [tu.get("name") for tu in tool_uses]
            log_event("runtime", "llm_response", user_id=user_id, session_id=session_id,
                      round=_round, stop_reason=stop_reason, tools_called=tool_names,
                      text_preview=llm_text)

            # Intercept end_turn tool: extract expects_reply and remove from real tool calls.
            end_turn_uses = [tu for tu in tool_uses if tu.get("name") == "end_turn"]
            real_tool_uses = [tu for tu in tool_uses if tu.get("name") != "end_turn"]
            if end_turn_uses:
                inp = end_turn_uses[-1].get("input") or {}
                expects_reply = inp.get("expects_reply", True)
                # When expects_reply=false, release skill lock (conversation topic changed)
                if not expects_reply:
                    async with self._lock:
                        st = self._sessions.get(session_id)
                        if st:
                            st.active_skill = None
                # When expects_reply=true AND there are other tool calls, the model
                # is trying to confirm intent and execute simultaneously. Drop the
                # tool calls — we must wait for user confirmation first.
                if expects_reply and real_tool_uses:
                    log_event("runtime", "drop_tools_for_confirm", session_id=session_id,
                              dropped=[tu.get("name") for tu in real_tool_uses])
                    real_tool_uses = []

            # Guard: on the first round, if the model output a QUESTION (ends with ?)
            # AND skill-related tool calls but no active_skill yet, it's trying to
            # confirm intent and execute in the same turn. Force it to stop and wait.
            if _round == 0 and real_tool_uses and text_blocks and any(t.strip() for t in text_blocks):
                reply_stripped = llm_text.rstrip()
                is_question = reply_stripped.endswith("？") or reply_stripped.endswith("?")
                if is_question:
                    async with self._lock:
                        st = self._sessions.get(session_id)
                        has_active_skill = bool(st and st.active_skill)
                    if not has_active_skill:
                        skill_tool_names = {"read", "run_skill"}
                        has_skill_tool = any(tu.get("name") in skill_tool_names for tu in real_tool_uses)
                        if has_skill_tool:
                            log_event("runtime", "force_confirm_wait", session_id=session_id,
                                      dropped=[tu.get("name") for tu in real_tool_uses],
                                      text_preview=llm_text[:80])
                            real_tool_uses = []
                            expects_reply = True

            if not real_tool_uses:
                # Retry once if the model skipped tool calls on the first round
                # (some models like Qwen occasionally ignore tool instructions).
                # But do NOT retry if the model intentionally stopped to confirm
                # intent (output text + end_turn with no tools = confirmation turn).
                has_text_output = any(t.strip() for t in text_blocks)
                if (_round == 0 and not state.tool_used_this_turn
                        and self._skills.list() and not has_text_output):
                    async with self._lock:
                        st = self._sessions[session_id]
                        st.messages.append({"role": "assistant", "content": content})
                        st.messages.append({
                            "role": "user",
                            "content": "You have matching skills available. Please use the `read` tool to read the relevant SKILL.md and proceed.",
                        })
                        messages_snapshot = list(st.messages)
                    continue

                # Empty response fallback: if LLM returned nothing useful, tell user
                if not has_text_output and not end_turn_uses:
                    yield AdapterEvent("stream.chunk", {
                        "text": "不好意思，没反应过来，你再说一次？\n",
                        "display_only": False,
                        "content_type": "text",
                    })
                    expects_reply = True

                # If the model didn't call end_turn, infer expects_reply from
                # whether the reply ends with a question mark.
                if not end_turn_uses and has_text_output:
                    # Strip hallucinated tokens before checking question mark
                    reply_text = _HALLUCINATED_TOKENS.sub("", llm_text).rstrip()
                    if reply_text.endswith("？") or reply_text.endswith("?"):
                        expects_reply = True

                async with self._lock:
                    st = self._sessions[session_id]
                    st.messages.append(
                        {"role": "assistant", "content": content}
                    )
                    st.tool_used_this_turn = False
                    st.memory_loaded = False
                    # While a skill is active, always expects_reply=true
                    # (user hasn't explicitly ended the skill interaction).
                    if st.active_skill:
                        expects_reply = True
                # Save memories extracted from user input and assistant reply.
                await self._save_turn_memories(session_id, user_text, content)
                yield AdapterEvent("_meta", None, meta={"expects_reply": expects_reply})
                return

            if _round >= self.MAX_TOOL_ROUNDS:
                yield AdapterEvent(
                    "stream.chunk",
                    {"text": "\n[tool loop limit reached — stopping]\n", "display_only": False},
                )
                async with self._lock:
                    self._sessions[session_id].messages.append(
                        {"role": "assistant", "content": content}
                    )
                await self._save_turn_memories(session_id, user_text, content)
                yield AdapterEvent("_meta", None, meta={"expects_reply": expects_reply})
                return

            if cancel.is_cancelled():
                return

            tool_uses = real_tool_uses
            for tu in tool_uses:
                yield AdapterEvent(
                    "event.tool",
                    {"name": tu.get("name"), "id": tu.get("id"), "input": tu.get("input")},
                )

            async def _run_one(tu: dict[str, Any]) -> dict[str, Any]:
                tr = await self._exec_tool_use(session_id=session_id, tool_use=tu)
                async with self._lock:
                    st = self._sessions.get(session_id)
                    if st is not None and st.active_skill is not None:
                        st.tool_used_this_turn = True
                for block in tr.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        block["text"] = self._truncate_tool_text(
                            block["text"], self.HARD_MAX_TOOL_RESULT_CHARS
                        )
                return tr

            completed = await asyncio.gather(*[_run_one(tu) for tu in tool_uses])

            # Log tool execution results
            for tu, tr in zip(tool_uses, completed):
                is_err = tr.get("is_error", False)
                result_text = ""
                if tr.get("content"):
                    for block in tr["content"]:
                        if isinstance(block, dict) and block.get("text"):
                            result_text = block["text"][:150]
                            break
                log_event("runtime", "tool_result", user_id=user_id, session_id=session_id,
                          tool=tu.get("name"), is_error=is_err, result_preview=result_text)

            if cancel.is_cancelled():
                return

            # --- Engine: handle run_skill tool calls ---
            # run_skill is async and yields events, so handle it specially here.
            run_skill_uses = [tu for tu in tool_uses if tu.get("name") == "run_skill"]
            if run_skill_uses:
                tu_rs = run_skill_uses[0]
                rs_input = tu_rs.get("input") or {}
                rs_skill_id = str(rs_input.get("skill_id") or "")
                rs_action = rs_input.get("action")  # optional group name
                rs_params = rs_input.get("params") or {}
                if not isinstance(rs_params, dict):
                    rs_params = {}
                rs_params = {str(k): str(v) for k, v in rs_params.items()}
                skill_obj = self._skills.get(rs_skill_id)
                engine_steps = self._engine.try_parse(skill_obj) if skill_obj else None

                if skill_obj and engine_steps:
                    # Set skill lock
                    log_event("runtime", "engine_exec", user_id=user_id, session_id=session_id,
                              skill_id=rs_skill_id, action=rs_action, params=rs_params)
                    self._set_active_skill(session_id, rs_skill_id)
                    async with self._lock:
                        st = self._sessions.get(session_id)
                        if st:
                            st.tool_used_this_turn = True

                    # Action-specific status hint
                    _status_hints = {
                        "search": "帮你搜搜看~\n",
                        "add_to_cart": "加购中~\n",
                    }
                    hint = _status_hints.get(rs_action or "", "处理中~\n")
                    yield AdapterEvent("stream.chunk", {"text": hint, "display_only": False})
                    engine_output: str = ""
                    async for ev in self._engine.execute(
                        skill=skill_obj,
                        steps=engine_steps,
                        params=rs_params,
                        session_id=session_id,
                        cancel=cancel,
                        group=rs_action,
                    ):
                        if ev.type == "_engine_result":
                            engine_output = (ev.payload or {}).get("output", "")
                        else:
                            yield ev

                    if cancel.is_cancelled():
                        return

                    # Build tool_result for run_skill and other tools
                    all_results: list[dict[str, Any]] = []
                    for tu, tr in zip(tool_uses, completed):
                        if tu.get("name") == "run_skill":
                            all_results.append({
                                "type": "tool_result",
                                "tool_use_id": str(tu.get("id") or ""),
                                "is_error": False,
                                "content": [{"type": "text", "text": engine_output}],
                            })
                        else:
                            yield AdapterEvent(
                                "event.tool",
                                {"name": tu.get("name"), "id": tu.get("id"), "result": tr, "phase": "result"},
                            )
                            all_results.append(tr)

                    async with self._lock:
                        st = self._sessions[session_id]
                        st.messages.append({"role": "assistant", "content": content})
                        st.messages.append({"role": "user", "content": all_results})
                        messages_snapshot = list(st.messages)
                    continue  # next LLM round to format
                else:
                    # Skill not found or no engine steps - return error as tool_result
                    err_msg = f"Skill '{rs_skill_id}' not found or has no engine steps."
                    # Replace the run_skill completed result with error
                    for i, (tu, tr) in enumerate(zip(tool_uses, completed)):
                        if tu.get("name") == "run_skill":
                            completed[i] = {
                                "type": "tool_result",
                                "tool_use_id": str(tu.get("id") or ""),
                                "is_error": True,
                                "content": [{"type": "text", "text": err_msg}],
                            }

            tool_results: list[dict[str, Any]] = []
            for tu, tr in zip(tool_uses, completed):
                yield AdapterEvent(
                    "event.tool",
                    {"name": tu.get("name"), "id": tu.get("id"), "result": tr, "phase": "result"},
                )
                tool_results.append(tr)

            async with self._lock:
                st = self._sessions[session_id]
                st.messages.append({"role": "assistant", "content": content})
                st.messages.append({"role": "user", "content": tool_results})
                messages_snapshot = list(st.messages)

    async def _save_turn_memories(
        self, session_id: str, user_text: str, content: Any,
    ) -> None:
        """Extract memorable entries from user text and save them."""
        entries = self._extract_memory_entries(user_text)
        assistant_text = _iter_assistant_text(content) if isinstance(content, list) else ""
        entries.extend(self._extract_memory_entries(assistant_text))
        if not entries:
            return
        async with self._lock:
            st = self._sessions.get(session_id)
            uid = st.user_id if st else "default"
        try:
            mc = get_memory_client()
            await asyncio.to_thread(mc.save, uid, entries, session_id)
        except Exception:
            pass

    async def _exec_tool_use(self, *, session_id: str, tool_use: dict[str, Any]) -> dict[str, Any]:
        tool_name = str(tool_use.get("name") or "")
        tool_id = str(tool_use.get("id") or "")
        tool_input = tool_use.get("input") or {}

        if tool_name == "read":
            fp = str(tool_input.get("file_path") or "")

            # --- Skill-lock enforcement ---
            target_skill = self._detect_skill_from_path(fp)
            if target_skill is not None:
                lock_err = self._check_skill_lock(session_id, target_skill)
                if lock_err:
                    return {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "is_error": True,
                        "content": [{"type": "text", "text": lock_err}],
                    }
                self._set_active_skill(session_id, target_skill)

            # --- Engine detection: check if this SKILL.md has deterministic steps ---
            engine_hint = ""
            if target_skill is not None:
                skill_obj = self._skills.get(target_skill)
                if skill_obj is not None:
                    engine_steps = self._engine.try_parse(skill_obj)
                    if engine_steps:
                        # Collect params and groups
                        param_names: set[str] = set()
                        defaults: dict[str, str] = {}
                        groups: set[str] = set()
                        for s in engine_steps:
                            param_names.update(s.params)
                            defaults.update(s.defaults)
                            if s.group:
                                groups.add(s.group)
                        groups_hint = f" Available actions (pass as 'action'): {sorted(groups)}." if groups else ""
                        engine_hint = (
                            f"\n\n[ENGINE] This skill has deterministic steps. "
                            f"You MUST call the run_skill tool to execute it — outputting text like '帮你搜搜看' does NOT count as execution. "
                            f"Call run_skill with skill_id='{target_skill}' and params: {sorted(param_names)}. "
                            f"Defaults: {defaults}.{groups_hint} "
                            f"Extract the required params from the user's message. Do NOT use exec to run the skill commands."
                        )

            try:
                full = self._skills.read_text_file(fp)
                lines = full.splitlines(keepends=True)
                offset = tool_input.get("offset")
                limit = tool_input.get("limit")
                if offset is not None:
                    start = max(0, int(offset) - 1)
                    end = start + int(limit) if limit is not None else len(lines)
                    lines = lines[start:end]
                txt = "".join(lines) + engine_hint
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": txt}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"read failed: {e}"}],
                }

        if tool_name == "write":
            fp = str(tool_input.get("file_path") or "")
            content = str(tool_input.get("content") or "")
            try:
                msg = await asyncio.to_thread(self._skills.write_file, fp, content)
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": msg}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"write failed: {e}"}],
                }

        if tool_name == "edit":
            fp = str(tool_input.get("file_path") or "")
            old_s = str(tool_input.get("old_string") or "")
            new_s = str(tool_input.get("new_string") or "")
            try:
                msg = await asyncio.to_thread(self._skills.edit_file, fp, old_s, new_s)
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": msg}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"edit failed: {e}"}],
                }

        if tool_name == "exec":
            cmd = str(tool_input.get("command") or "").strip()
            workdir = tool_input.get("workdir")
            timeout_s = int(tool_input.get("timeout") or 60)
            if not cmd:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": "exec failed: command is empty"}],
                }
            try:
                res = await asyncio.to_thread(
                    self._skills.exec_command,
                    command=cmd,
                    cwd=str(workdir) if isinstance(workdir, str) and workdir.strip() else None,
                    timeout_s=timeout_s,
                )
                out = (
                    f"exit_code={res.exit_code}\n"
                    f"stdout:\n{res.stdout}\n"
                    f"stderr:\n{res.stderr}\n"
                ).strip()
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": res.exit_code != 0,
                    "content": [{"type": "text", "text": out}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"exec failed: {e}"}],
                }

        if tool_name == "list_dir":
            lp = str(tool_input.get("path") or "")
            max_entries = int(tool_input.get("max_entries") or 200)
            try:
                txt = await asyncio.to_thread(self._skills.list_directory, lp, max_entries=max_entries)
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": txt}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"list_dir failed: {e}"}],
                }

        if tool_name == "grep":
            pat = str(tool_input.get("pattern") or "")
            gp = str(tool_input.get("path") or "")
            max_m = int(tool_input.get("max_matches") or 80)
            try:
                txt = await asyncio.to_thread(
                    self._skills.grep_workspace, pat, gp, max_matches=max_m
                )
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": txt}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"grep failed: {e}"}],
                }

        if tool_name == "web_search":
            ws_query = str(tool_input.get("query") or "").strip()
            ws_count = tool_input.get("count")
            ws_region = tool_input.get("region")
            ws_safe = tool_input.get("safeSearch")
            if not ws_query:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": "web_search failed: query is empty"}],
                }
            try:
                txt = await asyncio.to_thread(
                    ddg_web_search,
                    query=ws_query,
                    count=int(ws_count) if ws_count is not None else None,
                    region=str(ws_region) if ws_region else None,
                    safe_search=str(ws_safe) if ws_safe else None,
                )
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": txt}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"web_search failed: {e}"}],
                }

        if tool_name == "web_fetch":
            url = str(tool_input.get("url") or "")
            max_bytes = int(tool_input.get("max_bytes") or 500_000)
            timeout_s = int(tool_input.get("timeout_s") or 30)
            try:
                txt = await asyncio.to_thread(
                    self._skills.http_get_text, url, max_bytes=max_bytes, timeout_s=timeout_s
                )
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": txt}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"web_fetch failed: {e}"}],
                }

        if tool_name == "browser":
            from .browser_client import BrowserManager

            action = str(tool_input.get("action") or "").strip()
            bm = BrowserManager.get()
            try:
                if action == "navigate":
                    url = str(tool_input.get("url") or "").strip()
                    if not url:
                        raise ValueError("url is required for navigate")
                    wait_until = str(tool_input.get("wait_until") or "load")
                    txt = await bm.navigate(session_id, url=url, wait_until=wait_until)
                elif action == "snapshot":
                    txt = await bm.snapshot(session_id)
                elif action == "screenshot":
                    full_page = bool(tool_input.get("full_page"))
                    txt = await bm.screenshot(session_id, full_page=full_page)
                elif action == "click":
                    sel = tool_input.get("selector")
                    ref = tool_input.get("ref")
                    txt = await bm.click(
                        session_id,
                        selector=str(sel) if sel else None,
                        ref=int(ref) if ref is not None else None,
                    )
                elif action == "type":
                    sel = tool_input.get("selector")
                    ref = tool_input.get("ref")
                    text = str(tool_input.get("text") or "")
                    clear_first = bool(tool_input.get("clear_first"))
                    txt = await bm.type_text(
                        session_id,
                        text=text,
                        selector=str(sel) if sel else None,
                        ref=int(ref) if ref is not None else None,
                        clear_first=clear_first,
                    )
                elif action == "select":
                    sel = tool_input.get("selector")
                    ref = tool_input.get("ref")
                    value = tool_input.get("value")
                    label = tool_input.get("label")
                    txt = await bm.select(
                        session_id,
                        selector=str(sel) if sel else None,
                        ref=int(ref) if ref is not None else None,
                        value=str(value) if value else None,
                        label=str(label) if label else None,
                    )
                elif action == "set_cookies":
                    raw_cookies = tool_input.get("cookies")
                    if not isinstance(raw_cookies, list):
                        raise ValueError("cookies must be an array of {name, value, domain, ...} objects")
                    txt = await bm.set_cookies(session_id, cookies=raw_cookies)
                elif action == "set_cookies_file":
                    fp = str(tool_input.get("file_path") or "").strip()
                    if not fp:
                        raise ValueError("file_path is required for set_cookies_file")
                    txt = await bm.set_cookies_from_file(session_id, file_path=fp)
                elif action == "get_cookies":
                    txt = await bm.get_cookies(session_id)
                elif action == "wait":
                    sel = tool_input.get("selector")
                    ts = tool_input.get("timeout_s")
                    st = str(tool_input.get("state") or "visible")
                    txt = await bm.wait(
                        session_id,
                        selector=str(sel) if sel else None,
                        timeout_s=int(ts) if ts is not None else None,
                        state=st,
                    )
                elif action == "evaluate":
                    js = str(tool_input.get("script") or "").strip()
                    if not js:
                        raise ValueError("script is required for evaluate")
                    txt = await bm.evaluate(session_id, script=js)
                else:
                    txt = (
                        f"Unknown browser action: {action!r}. "
                        "Valid: navigate, snapshot, screenshot, click, type, select, "
                        "set_cookies, set_cookies_file, get_cookies, wait, evaluate."
                    )
                is_err = txt.startswith(("navigate failed:", "snapshot failed:", "screenshot failed:",
                                         "click failed:", "type failed:", "select failed:",
                                         "set_cookies failed:", "get_cookies failed:", "wait failed:",
                                         "evaluate failed:", "Unknown browser"))
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": is_err,
                    "content": [{"type": "text", "text": txt}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"browser failed: {e}"}],
                }

        if tool_name == "price_compare":
            query = str(tool_input.get("query") or "").strip()
            raw_pl = tool_input.get("platforms")
            platforms: list[str] | None = None
            if isinstance(raw_pl, list):
                platforms = [str(x) for x in raw_pl]
            if not query:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": "price_compare failed: query is empty"}],
                }
            try:
                txt = await asyncio.to_thread(
                    call_price_compare_service, query=query, platforms=platforms
                )
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": txt}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"price_compare failed: {e}"}],
                }

        if tool_name == "batch_web_search":
            raw_q = tool_input.get("queries")
            if not isinstance(raw_q, list) or not raw_q:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [
                        {
                            "type": "text",
                            "text": "batch_web_search failed: queries must be a non-empty array of strings",
                        }
                    ],
                }
            queries = [str(x) for x in raw_q]
            try:
                txt = await asyncio.to_thread(batch_web_search_queries, queries)
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": txt}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"batch_web_search failed: {e}"}],
                }

        if tool_name == "update_user_config":
            key = str(tool_input.get("key") or "")
            value = str(tool_input.get("value") or "")
            allowed_keys = {"language", "tone", "agent_profile", "user_info"}
            if not key or key not in allowed_keys:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"Invalid config key: {key!r}. Allowed: {sorted(allowed_keys)}"}],
                }
            try:
                # Update in-memory user_config (shared reference from RuntimeService)
                if self._user_config is not None and hasattr(self._user_config, key):
                    setattr(self._user_config, key, value)
                    # Persist asynchronously
                    from ..gateway.user_config import save_user_config
                    await save_user_config(self._user_config)  # type: ignore[arg-type]
                log_event = __import__("openclaw_gateway_runtime.infra.structured_log", fromlist=["log_event"]).log_event
                log_event("anthropic_runtime", "config_updated", session_id=session_id, key=key, value=value)
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": False,
                    "content": [{"type": "text", "text": f"Config updated: {key} = {value}"}],
                }
            except Exception as e:
                return {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "is_error": True,
                    "content": [{"type": "text", "text": f"update_user_config failed: {e}"}],
                }

        # run_skill is handled in the tool loop (run_turn), not here.
        # Return a placeholder that will be replaced by the engine result.
        if tool_name == "run_skill":
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "is_error": False,
                "content": [{"type": "text", "text": "[engine pending]"}],
            }

        # Unknown tool
        return {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "is_error": True,
            "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
        }

        # (Legacy per-skill tool path removed in favor of OpenClaw-like experience.)
        if False:  # pragma: no cover
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "is_error": True,
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
            }


def _chunk_text(s: str, n: int) -> list[str]:
    if n <= 0:
        return [s]
    return [s[i : i + n] for i in range(0, len(s), n)]

