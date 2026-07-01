from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("openclaw_gateway_runtime")
_FILE_HANDLER: logging.FileHandler | None = None


def configure_logging(*, level: str | None = None) -> None:
    """
    One-line JSON per log record on stderr (suitable for log collectors).
    If LOG_FILE is set, also writes to that file.
    """
    lvl_name = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, lvl_name, logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(lvl)

    _LOGGER.handlers.clear()
    _LOGGER.setLevel(lvl)
    _LOGGER.propagate = True

    # File output
    global _FILE_HANDLER
    log_file = os.environ.get("LOG_FILE", "").strip()
    if log_file and _FILE_HANDLER is None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _FILE_HANDLER = logging.FileHandler(str(log_path), encoding="utf-8")
        _FILE_HANDLER.setFormatter(logging.Formatter("%(message)s"))
        _LOGGER.addHandler(_FILE_HANDLER)


def log_event(component: str, event: str, *, level: str = "info", **fields: Any) -> None:
    """
    Emit one structured JSON object per line.
    Set OPENCLAW_STRUCTURED_LOG=0 to disable (e.g. unit tests / captured stderr).
    """
    if os.environ.get("OPENCLAW_STRUCTURED_LOG", "1") == "0":
        return
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": component,
        "event": event,
    }
    for k, v in fields.items():
        if k in payload:
            continue
        payload[k] = v
    try:
        log_fn = getattr(_LOGGER, level, _LOGGER.info)
        log_fn(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        return
