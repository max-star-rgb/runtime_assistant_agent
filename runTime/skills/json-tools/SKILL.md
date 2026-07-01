---
name: json-tools
description: Pretty-print or validate JSON files using a bundled Python script. Use when the user needs formatted JSON output.
license: MIT
---

# JSON Tools

## Pretty-print a File

```bash
python scripts/format_json.py /absolute/path/to/data.json
```

Reads JSON from the given path and writes indented output to stdout.

Without arguments the script reads from stdin (useful when piping).

## Guardrails

- The JSON file must be under the allowed workspace roots.
- Output is UTF-8 with `ensure_ascii=False`.
