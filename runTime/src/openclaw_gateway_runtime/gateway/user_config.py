from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _config_base_dir() -> Path:
    """
    Base directory for per-user config files.
    Defaults to a 'users' subdirectory alongside MEMORY_LOCAL_DIR (or a temp dir).
    Override with USER_CONFIG_DIR env var.
    """
    explicit = (os.environ.get("USER_CONFIG_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    memory_dir = (os.environ.get("MEMORY_LOCAL_DIR") or "").strip()
    if memory_dir:
        return Path(memory_dir) / "users"
    import tempfile
    return Path(tempfile.gettempdir()) / "openclaw_runtime" / "users"


@dataclass
class UserConfig:
    user_id: str
    language: str = "zh-CN"
    user_info: str = ""
    agent_profile: str = "default"
    tone: str = "friendly"
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UserConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def _config_path(user_id: str) -> Path:
    return _config_base_dir() / user_id / "config.json"


def load_user_config(user_id: str) -> UserConfig:
    """Load persisted config for *user_id*; returns defaults if file does not exist."""
    path = _config_path(user_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["user_id"] = user_id
            return UserConfig.from_dict(data)
        except Exception:
            pass
    return UserConfig(user_id=user_id)


def merge_config(local: UserConfig, incoming: dict[str, Any]) -> UserConfig:
    """
    Merge call.incoming payload into local config.
    'language' always uses incoming value (each connection can switch language).
    Other fields: local (persisted) values take priority; incoming fills in missing fields.
    """
    defaults = UserConfig(user_id=local.user_id)
    merged = UserConfig.from_dict(local.to_dict())

    for key in ("language", "user_info", "agent_profile", "tone"):
        local_val = getattr(local, key)
        default_val = getattr(defaults, key)
        incoming_val = incoming.get(key)
        if incoming_val is not None:
            if key == "language" or local_val == default_val:
                setattr(merged, key, str(incoming_val))

    return merged


async def save_user_config(config: UserConfig) -> None:
    """Asynchronously persist *config* to the local JSON file."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _save_sync, config)


def _save_sync(config: UserConfig) -> None:
    path = _config_path(config.user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.to_dict()
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
