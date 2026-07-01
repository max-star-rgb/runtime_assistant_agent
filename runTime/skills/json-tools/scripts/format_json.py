#!/usr/bin/env python3
"""Pretty-print JSON: from stdin, or from file path in argv[1]."""
from __future__ import annotations

import json
import sys


def main() -> None:
    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
