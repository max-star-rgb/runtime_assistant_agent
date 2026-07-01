#!/usr/bin/env python3
"""Print current UTC time as ISO 8601 (one line)."""
from __future__ import annotations

import datetime

if __name__ == "__main__":
    print(datetime.datetime.now(datetime.timezone.utc).isoformat())
