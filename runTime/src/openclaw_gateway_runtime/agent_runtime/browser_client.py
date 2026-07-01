"""
Stealth browser automation client for the agent runtime.

Uses Patchright (anti-detection Playwright fork) when available, falls back
to standard Playwright.  Each session uses a **persistent context** so that
cookies, localStorage and session state survive across actions and even
across process restarts (data is stored on disk).

Key anti-detection practices (from Patchright / online-shopping skill):
  - headless: False  (headless mode is a fingerprint signal)
  - viewport: None   (let browser use its natural viewport)
  - NO custom userAgent or HTTP header injection
  - persistent context  (more realistic than ephemeral contexts)
  - --disable-blink-features=AutomationControlled
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

def _cfg_headless() -> bool:
    return (os.environ.get("BROWSER_HEADLESS") or "true").strip().lower() in ("true", "1", "yes")


def _cfg_timeout_ms() -> int:
    return int(os.environ.get("BROWSER_TIMEOUT_S") or "30") * 1000


def _cfg_user_data_dir() -> Path:
    default = Path(tempfile.gettempdir()) / "runtime_browser_profile"
    raw = os.environ.get("BROWSER_USER_DATA_DIR", "").strip()
    return Path(raw) if raw else default


# ---------------------------------------------------------------------------
# Ref-indexed accessibility snapshot
# ---------------------------------------------------------------------------

@dataclass
class _SnapshotRef:
    """One entry in a flattened accessibility tree."""
    ref: int
    role: str
    name: str
    attrs: dict[str, Any] = field(default_factory=dict)


def _format_snapshot(refs: list[_SnapshotRef]) -> str:
    lines: list[str] = []
    for r in refs:
        extra = " ".join(f'{k}={v!r}' if isinstance(v, str) else f'{k}={v}' for k, v in r.attrs.items())
        label = f'"{r.name}"' if r.name else ""
        parts = [f"[{r.ref}]", r.role, label, extra]
        lines.append(" ".join(p for p in parts if p))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JavaScript snippet: extract interactive/semantic elements from the DOM
# ---------------------------------------------------------------------------

_JS_SNAPSHOT = """
() => {
    const INTERACTIVE = new Set([
        'a', 'button', 'input', 'select', 'textarea', 'details', 'summary'
    ]);
    const SEMANTIC = new Set([
        'h1','h2','h3','h4','h5','h6','p','li','th','td','label','img','nav','main',
        'header','footer','article','section','form','table'
    ]);
    const results = [];
    let refCounter = 0;

    function cssPath(el) {
        const parts = [];
        let cur = el;
        while (cur && cur.nodeType === 1) {
            let sel = cur.tagName.toLowerCase();
            if (cur.id) { parts.unshift('#' + cur.id); break; }
            const parent = cur.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
                if (siblings.length > 1) sel += ':nth-of-type(' + (siblings.indexOf(cur) + 1) + ')';
            }
            parts.unshift(sel);
            cur = parent;
        }
        return parts.join(' > ');
    }

    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let node;
    while (node = walker.nextNode()) {
        const tag = node.tagName.toLowerCase();
        const role = node.getAttribute('role') || '';
        if (!INTERACTIVE.has(tag) && !SEMANTIC.has(tag) && !role) continue;
        if (node.offsetWidth === 0 && node.offsetHeight === 0) continue;

        refCounter++;
        const entry = { ref: refCounter, selector: cssPath(node) };

        if (role) entry.role = role;
        else if (tag === 'a') entry.role = 'link';
        else if (tag === 'button' || (tag === 'input' && node.type === 'submit')) entry.role = 'button';
        else if (tag === 'input') entry.role = node.type === 'checkbox' ? 'checkbox' : 'textbox';
        else if (tag === 'select') entry.role = 'combobox';
        else if (tag === 'textarea') entry.role = 'textbox';
        else if (tag === 'img') entry.role = 'img';
        else if (/^h[1-6]$/.test(tag)) { entry.role = 'heading'; entry.level = parseInt(tag[1]); }
        else entry.role = tag;

        const ariaLabel = node.getAttribute('aria-label');
        const alt = node.getAttribute('alt');
        const placeholder = node.getAttribute('placeholder');
        const text = node.textContent?.trim().substring(0, 80);
        entry.name = ariaLabel || alt || placeholder || text || '';

        if (tag === 'a' && node.href) entry.href = node.href;
        if (node.disabled) entry.disabled = true;
        if (document.activeElement === node) entry.focused = true;
        if (tag === 'input' || tag === 'textarea') {
            const v = node.value;
            if (v) entry.value = v.substring(0, 80);
        }

        results.push(entry);
    }
    return results;
}
"""


# ---------------------------------------------------------------------------
# BrowserManager — singleton per runtime process
# ---------------------------------------------------------------------------

class BrowserManager:
    """
    Lazily manages a Patchright/Playwright Chromium persistent context.

    Each ``session_id`` maps to an independent Page (tab) within the single
    persistent browser context.  Cookies and session state are persisted to
    disk automatically.
    """

    _instance: "BrowserManager | None" = None

    def __init__(self) -> None:
        self._pw_module: Any = None        # patchright or playwright module
        self._playwright: Any = None       # async_playwright instance
        self._context: Any = None          # persistent browser context
        self._pages: dict[str, Any] = {}   # session_id -> Page
        self._ref_map: dict[str, list[_SnapshotRef]] = {}
        self._selector_map: dict[str, dict[int, str]] = {}
        self._lock = asyncio.Lock()
        self._engine: str = "unknown"      # "patchright" or "playwright"

    @classmethod
    def get(cls) -> "BrowserManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def _ensure_browser(self) -> None:
        # Health-check: if the context exists but is broken, tear it down
        # so we can recreate it below.
        if self._context is not None:
            try:
                await self._context.new_page()
            except Exception:
                # Context is dead — reset everything so we re-launch.
                self._pages.clear()
                self._ref_map.clear()
                self._selector_map.clear()
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
                try:
                    if self._playwright:
                        await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            else:
                # Health-check page created successfully — close it and return.
                pages = self._context.pages
                if pages:
                    try:
                        await pages[-1].close()
                    except Exception:
                        pass
                return

        # Prefer Patchright for anti-detection; fall back to Playwright
        try:
            from patchright.async_api import async_playwright
            self._engine = "patchright"
        except ImportError:
            try:
                from playwright.async_api import async_playwright
                self._engine = "playwright"
            except ImportError as exc:
                raise RuntimeError(
                    "Neither patchright nor playwright is installed. "
                    "Run: pip install patchright && python -m patchright install chromium"
                ) from exc

        self._playwright = await async_playwright().start()

        user_data = _cfg_user_data_dir()
        user_data.mkdir(parents=True, exist_ok=True)

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]

        headless = _cfg_headless()

        # Persistent context: cookies/localStorage saved to disk automatically
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data),
            headless=headless,
            viewport=None,         # real viewport — critical for anti-detection
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=launch_args,
        )

    async def _get_page(self, session_id: str) -> Any:
        async with self._lock:
            await self._ensure_browser()
            if session_id not in self._pages:
                page = await self._context.new_page()
                self._pages[session_id] = page
            return self._pages[session_id]

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------

    async def navigate(self, session_id: str, *, url: str, wait_until: str = "load") -> str:
        page = await self._get_page(session_id)
        valid_waits = ("load", "domcontentloaded", "networkidle", "commit")
        if wait_until not in valid_waits:
            wait_until = "load"
        try:
            resp = await page.goto(url, wait_until=wait_until, timeout=_cfg_timeout_ms())
            status = resp.status if resp else "unknown"
            title = await page.title()

            captcha_title_kw = ("验证码", "验证拦截", "安全验证", "人机验证", "访问被拒绝")
            captcha_body_kw = ("通过验证", "请选择符合描述", "滑块", "拖动滑块", "人机验证",
                               "访问行为存在异常")

            async def _is_captcha() -> bool:
                t = await page.title() or ""
                if any(kw in t for kw in captcha_title_kw):
                    return True
                try:
                    body = await page.evaluate("() => (document.body?.innerText || '').substring(0, 500)")
                    return any(kw in body for kw in captcha_body_kw)
                except Exception:
                    return False

            if await _is_captcha():
                if not _cfg_headless():
                    for attempt in range(6):
                        await asyncio.sleep(10)
                        if not await _is_captcha():
                            final_title = await page.title()
                            return (
                                f"Navigated to {url} (status={status}, title={final_title!r}) "
                                f"[CAPTCHA resolved after {(attempt + 1) * 10}s] "
                                f"(engine={self._engine})"
                            )
                    final_title = await page.title()
                    return (
                        f"Navigated to {url} (status={status}, title={final_title!r}) "
                        f"[CAPTCHA detected — waited 60s but not resolved. "
                        f"Please complete the verification in the browser window.] "
                        f"(engine={self._engine})"
                    )
                else:
                    return (
                        f"Navigated to {url} (status={status}, title={title!r}) "
                        f"[CAPTCHA detected — set BROWSER_HEADLESS=false to allow manual verification] "
                        f"(engine={self._engine})"
                    )

            return f"Navigated to {url} (status={status}, title={title!r}) (engine={self._engine})"
        except Exception as e:
            return f"navigate failed: {e}"

    async def snapshot(self, session_id: str) -> str:
        page = await self._get_page(session_id)

        if hasattr(page, "aria_snapshot"):
            try:
                aria_text: str = await page.aria_snapshot(mode="ai", timeout=_cfg_timeout_ms())
                self._ref_map[session_id] = self._parse_aria_refs(aria_text)
                return aria_text
            except Exception:
                pass  # fall through to JS method

        try:
            raw = await page.evaluate(_JS_SNAPSHOT)
            if not raw:
                return "(empty page — no accessible elements)"
            refs: list[_SnapshotRef] = []
            for item in raw:
                refs.append(_SnapshotRef(
                    ref=item["ref"],
                    role=item.get("role", ""),
                    name=item.get("name", ""),
                    attrs={k: v for k, v in item.items() if k not in ("ref", "role", "name", "selector")},
                ))
            self._ref_map[session_id] = refs
            self._selector_map[session_id] = {item["ref"]: item["selector"] for item in raw if "selector" in item}
            return _format_snapshot(refs)
        except Exception as e:
            return f"snapshot failed: {e}"

    @staticmethod
    def _parse_aria_refs(aria_text: str) -> list[_SnapshotRef]:
        import re
        refs: list[_SnapshotRef] = []
        for m in re.finditer(r'\[ref=(e?\d+)\]', aria_text):
            ref_str = m.group(1)
            ref_num = int(ref_str.lstrip("e")) if ref_str.startswith("e") else int(ref_str)
            refs.append(_SnapshotRef(ref=ref_num, role="", name="", attrs={"raw_ref": m.group(1)}))
        return refs

    async def screenshot(self, session_id: str, *, full_page: bool = False) -> str:
        page = await self._get_page(session_id)
        try:
            tmp_dir = Path(tempfile.gettempdir()) / "runtime_browser"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            path = tmp_dir / f"screenshot_{session_id}_{id(page)}.png"
            await page.screenshot(path=str(path), full_page=full_page, timeout=_cfg_timeout_ms())
            return f"Screenshot saved to {path} ({path.stat().st_size} bytes)"
        except Exception as e:
            return f"screenshot failed: {e}"

    async def click(self, session_id: str, *, selector: str | None = None, ref: int | None = None) -> str:
        page = await self._get_page(session_id)
        try:
            locator = self._resolve_locator(page, session_id, selector=selector, ref=ref)
            await locator.click(timeout=_cfg_timeout_ms())
            return "ok"
        except Exception as e:
            return f"click failed: {e}"

    async def type_text(
        self,
        session_id: str,
        *,
        text: str,
        selector: str | None = None,
        ref: int | None = None,
        clear_first: bool = False,
    ) -> str:
        page = await self._get_page(session_id)
        try:
            locator = self._resolve_locator(page, session_id, selector=selector, ref=ref)
            if clear_first:
                await locator.fill("", timeout=_cfg_timeout_ms())
            await locator.fill(text, timeout=_cfg_timeout_ms())
            return "ok"
        except Exception as e:
            return f"type failed: {e}"

    async def select(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        ref: int | None = None,
        value: str | None = None,
        label: str | None = None,
    ) -> str:
        page = await self._get_page(session_id)
        try:
            locator = self._resolve_locator(page, session_id, selector=selector, ref=ref)
            if label is not None:
                await locator.select_option(label=label, timeout=_cfg_timeout_ms())
            elif value is not None:
                await locator.select_option(value=value, timeout=_cfg_timeout_ms())
            else:
                return "select failed: either value or label is required"
            return "ok"
        except Exception as e:
            return f"select failed: {e}"

    # ------------------------------------------------------------------
    # Cookie management
    # ------------------------------------------------------------------

    async def set_cookies(self, session_id: str, *, cookies: list[dict[str, Any]]) -> str:
        await self._get_page(session_id)
        try:
            normalized: list[dict[str, Any]] = []
            for c in cookies:
                entry: dict[str, Any] = {
                    "name": str(c.get("name", "")),
                    "value": str(c.get("value", "")),
                    "domain": str(c.get("domain", "")),
                    "path": str(c.get("path", "/")),
                }
                if "httpOnly" in c:
                    entry["httpOnly"] = bool(c["httpOnly"])
                if "secure" in c:
                    entry["secure"] = bool(c["secure"])
                if "sameSite" in c:
                    entry["sameSite"] = str(c["sameSite"])
                normalized.append(entry)
            await self._context.add_cookies(normalized)
            return f"Injected {len(normalized)} cookies (persistent context — saved to disk)"
        except Exception as e:
            return f"set_cookies failed: {e}"

    async def get_cookies(self, session_id: str, *, urls: list[str] | None = None) -> str:
        page = await self._get_page(session_id)
        try:
            if urls:
                raw = await self._context.cookies(urls)
            else:
                current_url = page.url
                raw = await self._context.cookies([current_url]) if current_url and current_url != "about:blank" else await self._context.cookies()
            summary = [{"name": c["name"], "domain": c["domain"], "path": c["path"],
                        "value": c["value"][:40] + ("..." if len(c["value"]) > 40 else "")}
                       for c in raw]
            return _json.dumps(summary, ensure_ascii=False, indent=2)
        except Exception as e:
            return f"get_cookies failed: {e}"

    async def set_cookies_from_file(self, session_id: str, *, file_path: str) -> str:
        try:
            text = Path(file_path).read_text(encoding="utf-8")
            raw = _json.loads(text)
            if not isinstance(raw, list):
                return "set_cookies_from_file failed: JSON must be an array of cookie objects"
            cookies: list[dict[str, Any]] = []
            for c in raw:
                entry: dict[str, Any] = {
                    "name": str(c.get("name", "")),
                    "value": str(c.get("value", "")),
                    "domain": str(c.get("domain", "")),
                    "path": str(c.get("path", "/")),
                }
                if c.get("httpOnly") is not None:
                    entry["httpOnly"] = bool(c["httpOnly"])
                if c.get("secure") is not None:
                    entry["secure"] = bool(c["secure"])
                if c.get("sameSite"):
                    sam = str(c["sameSite"])
                    if sam in ("Strict", "Lax", "None"):
                        entry["sameSite"] = sam
                if c.get("expirationDate"):
                    entry["expires"] = float(c["expirationDate"])
                elif c.get("expires"):
                    entry["expires"] = float(c["expires"])
                cookies.append(entry)
            return await self.set_cookies(session_id, cookies=cookies)
        except _json.JSONDecodeError as e:
            return f"set_cookies_from_file failed: invalid JSON — {e}"
        except Exception as e:
            return f"set_cookies_from_file failed: {e}"

    # ------------------------------------------------------------------
    # Wait
    # ------------------------------------------------------------------

    async def wait(self, session_id: str, *, selector: str | None = None, timeout_s: int | None = None, state: str = "visible") -> str:
        page = await self._get_page(session_id)
        timeout_ms = (timeout_s or 10) * 1000
        try:
            if selector:
                valid_states = ("visible", "hidden", "attached", "detached")
                if state not in valid_states:
                    state = "visible"
                await page.locator(selector).wait_for(state=state, timeout=timeout_ms)
                return f"ok — element '{selector}' is {state}"
            else:
                await asyncio.sleep(min(timeout_ms / 1000, 30))
                return f"ok — waited {timeout_ms / 1000:.1f}s"
        except Exception as e:
            return f"wait failed: {e}"

    # ------------------------------------------------------------------
    # Evaluate JavaScript
    # ------------------------------------------------------------------

    async def evaluate(self, session_id: str, *, script: str) -> str:
        """Run JavaScript in the page and return the result as JSON string."""
        page = await self._get_page(session_id)
        try:
            result = await page.evaluate(script)
            if result is None:
                return "(no return value)"
            return _json.dumps(result, ensure_ascii=False, indent=2) if not isinstance(result, str) else result
        except Exception as e:
            return f"evaluate failed: {e}"

    # ------------------------------------------------------------------
    # Locator resolution
    # ------------------------------------------------------------------

    def _resolve_locator(
        self,
        page: Any,
        session_id: str,
        *,
        selector: str | None = None,
        ref: int | None = None,
    ) -> Any:
        if selector:
            return page.locator(selector)
        if ref is not None:
            refs = self._ref_map.get(session_id, [])
            entry = next((r for r in refs if r.ref == ref), None)
            if entry is None:
                raise ValueError(
                    f"ref {ref} not found in last snapshot. "
                    f"Call snapshot first, then use a valid [N] ref."
                )
            sel_map = self._selector_map.get(session_id, {})
            if ref in sel_map:
                return page.locator(sel_map[ref])
            raw_ref = entry.attrs.get("raw_ref")
            if raw_ref:
                try:
                    return page.locator(f"internal:aria-ref={raw_ref}")
                except Exception:
                    pass
            if entry.role:
                return page.get_by_role(entry.role, name=entry.name, exact=True)
            raise ValueError(f"ref {ref} has no resolvable locator.")
        raise ValueError("Either selector or ref is required.")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            page = self._pages.pop(session_id, None)
            if page:
                await page.close()
            self._ref_map.pop(session_id, None)
            self._selector_map.pop(session_id, None)

    async def shutdown(self) -> None:
        async with self._lock:
            for sid in list(self._pages):
                page = self._pages.pop(sid)
                try:
                    await page.close()
                except Exception:
                    pass
            self._ref_map.clear()
            self._selector_map.clear()
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
