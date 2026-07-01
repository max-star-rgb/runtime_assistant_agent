---
name: workspace-nav
description: Explore the repository tree, read files, and make edits. Use when the user asks about project structure, wants to find files, or needs file modifications.
license: MIT
---

# Workspace Navigation

Browse directories, read source files, search for symbols, and create or edit files — all within the allowed workspace roots.

## Browsing

List the contents of a directory:

```bash
# list_dir with an absolute path
list_dir path=/absolute/path/to/folder
```

## Reading

```bash
# Full file
read file_path=src/main.py

# Partial — large files
read file_path=src/main.py offset=50 limit=30
```

## Searching

```bash
grep pattern="class MyClass" path=src/
```

## Creating Files

```bash
write file_path=path/to/new_file.txt content="hello world"
```

## Editing Files

Replace a unique string in an existing file:

```bash
edit file_path=src/main.py old_string="def old_name(" new_string="def new_name("
```

`old_string` must appear exactly once; include surrounding context if ambiguous.

## Guardrails

- All paths must be under the repo root or directories listed in `EXTRA_WORKSPACE_DIRS`.
- Use `list_dir` before guessing paths.
- Prefer `read` with `offset`/`limit` on files larger than a few hundred lines.
