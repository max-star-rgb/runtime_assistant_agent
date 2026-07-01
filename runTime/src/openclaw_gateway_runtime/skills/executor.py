from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .registry import Skill


@dataclass(frozen=True)
class ScriptResult:
    exit_code: int
    stdout: str
    stderr: str


class SkillsExecutor:
    """
    Execute scripts bundled in a skill folder.

    Security posture (MVP):
    - only allow execution of files under `<skill>/scripts/`
    - never follow symlinks
    - caller chooses which interpreter to use by file extension heuristics
    """

    def __init__(self, *, python_exe: str = "python", bash_exe: str = "bash") -> None:
        self._python = python_exe
        self._bash = bash_exe

    def _resolve_script_path(self, skill: Skill, rel_script: str) -> Path:
        scripts = skill.scripts_dir()
        candidate = (scripts / rel_script).resolve()
        scripts_root = scripts.resolve()
        if not str(candidate).startswith(str(scripts_root) + os.sep) and candidate != scripts_root:
            raise ValueError("script path escapes scripts/ root")
        if candidate.is_symlink():
            raise ValueError("symlink scripts are not allowed")
        if not candidate.exists() or not candidate.is_file():
            raise FileNotFoundError(f"script not found: {candidate}")
        return candidate

    def run_script(
        self,
        *,
        skill: Skill,
        script: str,
        args: List[str] | None = None,
        timeout_s: int = 60,
        cwd: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
    ) -> ScriptResult:
        args = args or []
        path = self._resolve_script_path(skill, script)

        cmd: list[str]
        ext = path.suffix.lower()
        if ext == ".py":
            cmd = [self._python, str(path), *args]
        elif ext in {".sh", ".bash"}:
            cmd = [self._bash, str(path), *args]
        else:
            # Try executing directly (works for files with shebang on *nix; on Windows often not).
            cmd = [str(path), *args]

        proc = subprocess.run(
            cmd,
            cwd=str(cwd or skill.root_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return ScriptResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

