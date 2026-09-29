# DEMO-001: Filter tasks by completion status

Currently `python tasks.py list` returns every task.
Add optional `list --done` and `list --no-done` flags.

Acceptance criteria:
- No flag preserves the existing behavior and ID ordering.
- `--done` returns only completed tasks.
- `--no-done` returns only incomplete tasks.
- An empty result is the JSON array `[]`.
- Add regression tests; no database migration or new dependency is required.

This is a sample ticket for future dev-agent execution. Milestone 1 cannot run it.
