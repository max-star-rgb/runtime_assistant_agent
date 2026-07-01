from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .skill_md import SkillFrontmatter, parse_skill_md
from .executor import ScriptResult


@dataclass(frozen=True)
class Skill:
    id: str
    root_dir: Path
    frontmatter: SkillFrontmatter
    body_md: str

    def scripts_dir(self) -> Path:
        return self.root_dir / "scripts"

    def references_dir(self) -> Path:
        return self.root_dir / "references"

    def assets_dir(self) -> Path:
        return self.root_dir / "assets"


class SkillsRegistry:
    """
    Discovers OpenClaw-style skills from one or more roots.

    Each skill is a directory containing SKILL.md.
    """

    def __init__(self, roots: Iterable[Path]) -> None:
        self._roots = [Path(r).resolve() for r in roots]
        self._skills_by_id: Dict[str, Skill] = {}

    @classmethod
    def from_env(cls) -> "SkillsRegistry":
        # Default to this repo's `skills/`, plus optional external paths.
        raw = os.environ.get("SKILLS_PATHS", "").strip()
        # Prefer repo-local skills even if process cwd differs.
        repo_root_guess = Path(__file__).resolve().parents[3]  # .../src/openclaw_gateway_runtime/skills/registry.py
        roots: list[Path] = [repo_root_guess / "skills"]
        # Also include cwd-relative skills for local overrides / dev convenience.
        roots.append(Path.cwd() / "skills")
        if raw:
            for part in raw.split(os.pathsep):
                if part.strip():
                    roots.append(Path(part.strip()))
        return cls(roots)

    def refresh(self) -> None:
        skills: Dict[str, Skill] = {}
        for root in self._roots:
            if not root.exists() or not root.is_dir():
                continue
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                if not skill_md.exists():
                    continue
                text = skill_md.read_text(encoding="utf-8")
                fm, body = parse_skill_md(text)
                skill_id = fm.name.strip()
                if not fm.description.strip():
                    continue
                if not fm.enabled:
                    continue
                dir_name = entry.name
                if skill_id != dir_name:
                    import warnings
                    warnings.warn(
                        f"skill name '{skill_id}' does not match directory '{dir_name}' "
                        f"(per Agent Skills spec, name must match parent directory)",
                        stacklevel=1,
                    )
                    skill_id = dir_name
                if skill_id in skills:
                    continue
                skills[skill_id] = Skill(id=skill_id, root_dir=entry, frontmatter=fm, body_md=body)
        self._skills_by_id = skills

    def _runtime_platform(self) -> str:
        # Match OpenClaw's style loosely: darwin/linux/win32
        sys = platform.system().lower()
        if "darwin" in sys or "mac" in sys:
            return "darwin"
        if "linux" in sys:
            return "linux"
        if "windows" in sys:
            return "win32"
        return sys

    def _limits(self) -> tuple[int, int]:
        """
        OpenClaw-like prompt limits (approximate, char-based).
        """
        max_chars = int(os.environ.get("SKILLS_MAX_PROMPT_CHARS") or "30000")
        max_count = int(os.environ.get("SKILLS_MAX_IN_PROMPT") or "150")
        return max(0, max_chars), max(0, max_count)

    def _has_bin(self, name: str) -> bool:
        return shutil.which(name) is not None

    def _eligibility_note(self, s: Skill) -> tuple[bool, str | None]:
        """
        Return (eligible, note). If not eligible, caller should omit from prompt list.
        """
        fm = s.frontmatter
        if fm.disable_model_invocation:
            return (False, "disable-model-invocation")

        meta = fm.metadata.get("openclaw") if isinstance(fm.metadata.get("openclaw"), dict) else None
        if not isinstance(meta, dict):
            return (True, None)

        # OS filter
        os_list = meta.get("os")
        if isinstance(os_list, list) and os_list:
            plat = self._runtime_platform()
            if plat not in [str(x) for x in os_list]:
                return (False, f"os-mismatch:{plat}")

        requires = meta.get("requires")
        if isinstance(requires, dict):
            bins = requires.get("bins")
            if isinstance(bins, list) and bins:
                missing = [b for b in [str(x) for x in bins] if not self._has_bin(b)]
                if missing:
                    return (False, f"missing-bins:{','.join(missing)}")
            any_bins = requires.get("anyBins") or requires.get("anybins")
            if isinstance(any_bins, list) and any_bins:
                names = [str(x) for x in any_bins]
                if not any(self._has_bin(b) for b in names):
                    return (False, f"missing-anyBins:{','.join(names)}")
            envs = requires.get("env")
            if isinstance(envs, list) and envs:
                missing_env = [e for e in [str(x) for x in envs] if not os.environ.get(e)]
                if missing_env:
                    return (False, f"missing-env:{','.join(missing_env)}")

        return (True, None)

    def format_available_skills_xml(self) -> str:
        """
        OpenClaw-like skills listing for prompts.

        We include name/description and a location pointer to SKILL.md.
        Body and bundled resources are NOT included (progressive disclosure).
        """

        max_chars, max_count = self._limits()
        skills = []
        for s in self.list():
            ok, note = self._eligibility_note(s)
            if not ok:
                continue
            skills.append((s, note))

        # Count truncation first (OpenClaw truncates catalog by count).
        total = len(skills)
        by_count = skills[:max_count] if max_count >= 0 else []
        count_truncated = len(by_count) < total

        full_xml = self._format_skills_xml(by_count, compact=False)
        if len(full_xml) <= max_chars:
            return self._prepend_budget_notice(
                xml=full_xml,
                total=total,
                included=len(by_count),
                compact=False,
                truncated=count_truncated,
            )

        # Compact (omit descriptions) if it fits with overhead reservation.
        COMPACT_WARNING_OVERHEAD = 150
        compact_budget = max(0, max_chars - COMPACT_WARNING_OVERHEAD)
        compact_xml = self._format_skills_xml(by_count, compact=True)
        if len(compact_xml) <= compact_budget:
            return self._prepend_budget_notice(
                xml=compact_xml,
                total=total,
                included=len(by_count),
                compact=True,
                truncated=count_truncated,
            )

        # Binary-search reduce count until compact fits.
        lo, hi = 0, len(by_count)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            test_xml = self._format_skills_xml(by_count[:mid], compact=True)
            if len(test_xml) <= compact_budget:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        final_xml = self._format_skills_xml(by_count[:best], compact=True)
        return self._prepend_budget_notice(
            xml=final_xml,
            total=total,
            included=best,
            compact=True,
            truncated=True,
        )

    def _prepend_budget_notice(
        self,
        *,
        xml: str,
        total: int,
        included: int,
        compact: bool,
        truncated: bool,
    ) -> str:
        if truncated:
            note = f"⚠️ Skills truncated: included {included} of {total}{' (compact format, descriptions omitted)' if compact else ''}."
            return note + "\n" + xml
        if compact:
            note = "⚠️ Skills catalog using compact format (descriptions omitted)."
            return note + "\n" + xml
        return xml

    def _format_skills_xml(self, skills: list[tuple[Skill, str | None]], *, compact: bool) -> str:
        entries: list[str] = ["<available_skills>"]
        for s, note in skills:
            loc = str((s.root_dir / "SKILL.md").resolve())
            base = str(s.root_dir.resolve())
            name = s.id.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            desc = (
                s.frontmatter.description.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            entries.append("  <skill>")
            entries.append(f"    <name>{name}</name>")
            if not compact:
                entries.append(f"    <description>{desc}</description>")
            entries.append(f"    <location>{loc}</location>")
            entries.append(f"    <baseDir>{base}</baseDir>")
            if note:
                entries.append(f"    <note>{note}</note>")
            entries.append("  </skill>")
        entries.append("</available_skills>")
        return "\n".join(entries)

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    def _extra_allowed_roots(self) -> list[Path]:
        """
        Optional extra directory trees (e.g. user Desktop) for read/list/grep/exec cwd.

        Env EXTRA_WORKSPACE_DIRS: pipe-separated absolute paths (| avoids Windows drive-letter issues).
        Example: C:\\Users\\Me\\Desktop|D:\\Shared
        """
        raw = (os.environ.get("EXTRA_WORKSPACE_DIRS") or "").strip()
        if not raw:
            return []
        out: list[Path] = []
        for part in raw.split("|"):
            part = part.strip()
            if not part:
                continue
            try:
                p = Path(part).expanduser().resolve(strict=False)
            except (OSError, ValueError):
                continue
            out.append(p)
        return out

    def _is_allowed_path(self, p: Path) -> bool:
        try:
            p = p.resolve()
        except (OSError, ValueError):
            return False
        root = self._repo_root().resolve()
        try:
            p.relative_to(root)
            return True
        except ValueError:
            pass
        for extra in self._extra_allowed_roots():
            try:
                p.relative_to(extra)
                return True
            except ValueError:
                continue
        return False

    def path_access_denied_message(self) -> str:
        return (
            "path is outside allowed roots (repository root, and directories listed in "
            "EXTRA_WORKSPACE_DIRS if set). To access Desktop or Documents, add them to EXTRA_WORKSPACE_DIRS "
            "as pipe-separated absolute paths."
        )

    def read_text_file(self, path: str, *, max_bytes: int = 200000) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if not self._is_allowed_path(p):
            raise ValueError(self.path_access_denied_message())
        data = p.read_bytes()
        if len(data) > max_bytes:
            data = data[:max_bytes]
        return data.decode("utf-8", errors="replace")

    def exec_command(self, *, command: str, cwd: Optional[str], timeout_s: int) -> ScriptResult:
        """
        Execute a command with a workspace-local cwd restriction.
        """
        if not command.strip():
            raise ValueError("command is empty")
        workdir = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        if not self._is_allowed_path(workdir):
            raise ValueError(self.path_access_denied_message())
        # Use SkillsExecutor's result shape for consistency.
        import subprocess

        proc = subprocess.run(
            command,
            cwd=str(workdir),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        return ScriptResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def write_file(self, file_path: str, content: str) -> str:
        """Create or overwrite a file (aligned with OpenClaw `write` tool)."""
        p = Path(file_path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        if not self._is_allowed_path(p):
            raise ValueError(self.path_access_denied_message())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {p}"

    def edit_file(self, file_path: str, old_string: str, new_string: str) -> str:
        """
        Replace one occurrence of old_string with new_string in a file
        (aligned with OpenClaw `edit` tool).
        """
        p = Path(file_path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        if not self._is_allowed_path(p):
            raise ValueError(self.path_access_denied_message())
        if not p.exists():
            raise FileNotFoundError(f"file not found: {p}")
        text = p.read_text(encoding="utf-8")
        if old_string not in text:
            raise ValueError("old_string not found in file")
        count = text.count(old_string)
        if count > 1:
            raise ValueError(f"old_string is ambiguous ({count} occurrences); provide more context")
        new_text = text.replace(old_string, new_string, 1)
        p.write_text(new_text, encoding="utf-8")
        return f"edited {p} (replaced {len(old_string)} chars with {len(new_string)} chars)"

    _GREP_SKIP_DIRS = frozenset(
        {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", "dist", "build", ".eggs"}
    )

    def list_directory(self, path: str, *, max_entries: int = 200) -> str:
        """
        Non-recursive listing of one directory under the workspace root.
        """
        p = Path(path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        if not self._is_allowed_path(p):
            raise ValueError(self.path_access_denied_message())
        if not p.exists():
            raise ValueError("path does not exist")
        if not p.is_dir():
            raise ValueError("not a directory")
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        truncated = len(entries) > max_entries
        entries = entries[:max_entries]
        lines = [f"{'dir' if e.is_dir() else 'file'}\t{e.name}" for e in entries]
        out = "\n".join(lines)
        if truncated:
            out += f"\n...(truncated to {max_entries} entries)"
        return out

    def http_get_text(self, url: str, *, max_bytes: int = 500_000, timeout_s: int = 30) -> str:
        """
        Fetch a public http(s) URL; response body as text (truncated to max_bytes).
        """
        from urllib.parse import urlparse
        from urllib.request import Request, urlopen

        raw = url.strip()
        u = urlparse(raw)
        if u.scheme not in ("http", "https"):
            raise ValueError("only http/https URLs are allowed")
        if not u.netloc:
            raise ValueError("invalid URL")
        req = Request(raw, method="GET", headers={"User-Agent": "openclaw-gateway-runtime/1.0"})
        with urlopen(req, timeout=timeout_s) as resp:
            chunk = resp.read(max_bytes + 1)
        truncated = len(chunk) > max_bytes
        if truncated:
            chunk = chunk[:max_bytes]
        text = chunk.decode("utf-8", errors="replace")
        if truncated:
            text += f"\n...[truncated to {max_bytes} bytes]"
        return text

    def grep_workspace(
        self,
        pattern: str,
        path: str,
        *,
        max_matches: int = 80,
        max_file_bytes: int = 200_000,
        max_files_scanned: int = 2000,
    ) -> str:
        """
        Regex search in one file or recursively under a directory (text files only; bounded work).
        """
        if not pattern.strip():
            raise ValueError("pattern is empty")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e

        root = Path(path)
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        else:
            root = root.resolve()
        if not self._is_allowed_path(root):
            raise ValueError(self.path_access_denied_message())
        if not root.exists():
            raise ValueError("path does not exist")

        matches: list[str] = []
        files_scanned = 0

        def _try_file(fp: Path) -> None:
            nonlocal files_scanned
            if files_scanned >= max_files_scanned:
                return
            if not fp.is_file():
                return
            if not self._is_allowed_path(fp):
                return
            files_scanned += 1
            try:
                data = fp.read_bytes()
            except OSError:
                return
            if b"\x00" in data[:8192]:
                return
            if len(data) > max_file_bytes:
                data = data[:max_file_bytes]
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
            rel = str(fp)
            for li, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    matches.append(f"{rel}:{li}:{line}")
                    if len(matches) >= max_matches:
                        return

        if root.is_file():
            _try_file(root)
        else:
            for dirpath, dirnames, filenames in os.walk(root, topdown=True):
                base = Path(dirpath)
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d not in self._GREP_SKIP_DIRS and not d.startswith(".")
                ]
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    fp = base / fn
                    _try_file(fp)
                    if len(matches) >= max_matches or files_scanned >= max_files_scanned:
                        break
                if len(matches) >= max_matches or files_scanned >= max_files_scanned:
                    break

        if not matches:
            return "no matches"
        out = "\n".join(matches)
        if len(matches) >= max_matches:
            out += f"\n...(stopped at {max_matches} matches)"
        elif files_scanned >= max_files_scanned:
            out += f"\n...(stopped after scanning {max_files_scanned} files)"
        return out

    def list(self) -> list[Skill]:
        return sorted(self._skills_by_id.values(), key=lambda s: s.id)

    def get(self, skill_id: str) -> Optional[Skill]:
        return self._skills_by_id.get(skill_id)

    # Note: Anthropic tool mapping is handled by AgentRuntime (read_file, exec, list_dir, http_get, grep, …).

