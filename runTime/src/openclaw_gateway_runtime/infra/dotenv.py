from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | None = None) -> None:
    """
    Minimal .env loader:
    - KEY=VALUE lines
    - ignores blank lines and # comments
    - does not override existing environment variables
    """

    p = Path(path) if path else (Path.cwd() / ".env")
    if not p.exists() or not p.is_file():
        return

    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if not key:
            continue
        if key == "EXTRA_PATH":
            os.environ["PATH"] = val + os.pathsep + os.environ.get("PATH", "")
            continue
        if key in os.environ:
            continue
        os.environ[key] = val

