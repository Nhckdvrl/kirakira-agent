---
name: repo-navigation
description: Explore an unfamiliar repository before making changes.
---

# Repository Navigation

Use this skill when a request requires understanding the project structure,
entry points, or existing conventions before editing code.

## Workflow

1. Start with a fast file inventory using `rg --files`.
2. Read the nearest README, package metadata, and relevant tests.
3. Identify the smallest set of files that own the requested behavior.
4. Prefer existing naming, module boundaries, and helper functions.
5. Summarize what you learned before making broad changes.

## Useful Commands

```bash
rg --files
rg "keyword|function_name|class_name"
sed -n '1,220p' path/to/file.py
python3 -m unittest
```

## Guardrails

- Do not rewrite unrelated modules while exploring.
- Treat uncommitted changes as user work unless told otherwise.
- If two implementations already exist, follow the one used by tests or the CLI.
