---
name: test-debugging
description: Diagnose failing tests and turn failures into small, verified fixes.
---

# Test Debugging

Use this skill when tests fail, behavior regresses, or a bug report includes an
error message.

## Workflow

1. Reproduce the failure with the narrowest test command.
2. Read the full traceback and identify the first project-owned frame.
3. Inspect the test expectation before changing implementation.
4. Fix the cause, not only the assertion text.
5. Re-run the failing test, then related tests.

## Notes

- Prefer deterministic tests over sleeps, network calls, or environment-specific
  behavior.
- Keep test fixtures temporary and isolated.
- When a failure depends on missing environment variables, make the expected
  requirement explicit in the test or error message.

## Useful Commands

```bash
python3 -m unittest tests.test_tools
python3 -m unittest tests.test_cli
python3 -m unittest
```
