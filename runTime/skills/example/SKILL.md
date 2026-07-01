---
name: example
description: Minimal demo skill. Runs a bundled Python script and uses its output in the response. Use to verify the agent can discover, read, and execute a skill.
license: MIT
---

# Example Skill

A deterministic "hello" script that prints a greeting. Useful for smoke-testing the skills pipeline.

## Setup

No setup required — the script uses only the Python standard library.

## Usage

```bash
python scripts/hello.py --name Alice
```

The script prints `hello Alice` to stdout.

## References

See [scripts/hello.py](scripts/hello.py) for the full source.
