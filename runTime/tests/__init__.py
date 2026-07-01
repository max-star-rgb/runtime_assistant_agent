"""
Test package: default to disabling structured JSON logs on stderr so runners that
capture output do not block on full pipes (see OPENCLAW_STRUCTURED_LOG).
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")
