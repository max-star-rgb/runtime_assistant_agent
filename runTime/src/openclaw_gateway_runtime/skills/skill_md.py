from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str
    metadata: Dict[str, Any]
    user_invocable: bool
    disable_model_invocation: bool
    enabled: bool
    raw: Dict[str, Any]


def _parse_bool(raw: object, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in {"true", "yes", "1", "on"}:
            return True
        if v in {"false", "no", "0", "off"}:
            return False
    return default


def _parse_json5ish(blob: str) -> Dict[str, Any]:
    """
    Best-effort parser for OpenClaw-style metadata blocks.

    OpenClaw uses JSON5; to avoid extra deps we support a pragmatic subset:
    - single quotes for strings
    - trailing commas
    - unquoted object keys (simple identifiers)
    - line comments (// ...) and block comments (/* ... */)
    """
    import json
    import re

    s = blob.strip()
    # Strip comments
    s = re.sub(r"/\*[\s\S]*?\*/", "", s)
    s = re.sub(r"//.*?$", "", s, flags=re.MULTILINE)
    # Replace single-quoted strings with double-quoted (naive but ok for our skill metadata)
    s = re.sub(r"'", '"', s)
    # Quote unquoted keys: { foo: 1 } -> { "foo": 1 }
    s = re.sub(r'([{\s,])([A-Za-z_][A-Za-z0-9_\-]*)\s*:', r'\1"\2":', s)
    # Remove trailing commas: { "a": 1, } / [1,2,]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    obj = json.loads(s)
    if not isinstance(obj, dict):
        raise ValueError("metadata must be an object")
    return obj


def _parse_yaml_like(lines: list[str]) -> Dict[str, Any]:
    """
    Minimal YAML-ish parser for OpenClaw SKILL.md frontmatter.

    We deliberately avoid a YAML dependency. This supports:
    - `name: ...`
    - `description: ...`
    - `metadata:` followed by indented JSON object (as used by OpenClaw skills)
    """

    out: Dict[str, Any] = {}
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        if not raw.strip():
            i += 1
            continue
        if raw.startswith("metadata:"):
            # Collect subsequent indented lines as a JSON-ish blob.
            blob_lines: list[str] = []
            i += 1
            while i < len(lines):
                ln = lines[i].rstrip("\n")
                if ln.startswith("  ") or ln.startswith("\t") or (ln and ln[0].isspace()):
                    blob_lines.append(ln.strip())
                    i += 1
                    continue
                break
            blob = "\n".join(blob_lines).strip()
            if blob:
                try:
                    out["metadata"] = _parse_json5ish(blob)
                except Exception:
                    out["metadata"] = {"_raw": blob}
            else:
                out["metadata"] = {}
            continue

        if ":" in raw:
            k, v = raw.split(":", 1)
            key = k.strip()
            val = v.strip().strip("'").strip('"')
            out[key] = val
        i += 1
    return out


def parse_skill_md(text: str) -> Tuple[SkillFrontmatter, str]:
    """
    Parse an OpenClaw-style SKILL.md:
    - YAML frontmatter between `---` and `---`
    - body markdown after that
    """

    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].lstrip().startswith("---"):
        raise ValueError("SKILL.md missing frontmatter start delimiter (---).")

    # Find end delimiter.
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].lstrip().startswith("---"):
            end_idx = i
            break
    if end_idx is None:
        raise ValueError("SKILL.md missing frontmatter end delimiter (---).")

    fm_lines = [ln for ln in lines[1:end_idx]]
    body = "".join(lines[end_idx + 1 :]).lstrip("\n")
    parsed = _parse_yaml_like([ln.rstrip("\n") for ln in fm_lines])

    name = str(parsed.get("name") or "").strip()
    desc = str(parsed.get("description") or "").strip()
    metadata = parsed.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    user_invocable = _parse_bool(parsed.get("user-invocable"), True)
    disable_model_invocation = _parse_bool(parsed.get("disable-model-invocation"), False)
    enabled = _parse_bool(parsed.get("enabled"), True)

    if not name:
        raise ValueError("SKILL.md frontmatter missing required field: name")
    if not desc:
        raise ValueError("SKILL.md frontmatter missing required field: description")

    return (
        SkillFrontmatter(
            name=name,
            description=desc,
            metadata=metadata,
            user_invocable=user_invocable,
            disable_model_invocation=disable_model_invocation,
            enabled=enabled,
            raw=parsed,
        ),
        body,
    )

