---
name: python-coding
description: Make focused Python code changes that match this repository's style.
---

# Python Coding

Use this skill when implementing or refactoring Python code in this repository.
It is intentionally compact so the harness can load it during local experiments.

## Style

- Keep the code small, explicit, and easy to inspect.
- Prefer standard-library features unless the project already depends on a
  package that solves the problem well.
- Use type hints where neighboring code uses them.
- Return clear error strings at tool boundaries instead of raising raw internal
  exceptions to the user.
- Keep comments rare and useful.

## Editing Checklist

1. Read the surrounding module before editing.
2. Make the narrowest change that satisfies the request.
3. Update or add tests when behavior changes.
4. Run the smallest relevant test command, then the full suite if practical.
5. Report changed files and verification results.

## Common Commands

```bash
python3 -m unittest
python3 -m unittest tests.test_tools
python3 -m kirakira_agent
```
