# CRUD sandbox

A small task-management CLI backed by SQLite, using only the Python standard
library. It is a target application for dev-agent experiments, not part of the
installed dev-agent package.

From this directory:

```bash
python3 tasks.py create "Buy milk"
python3 tasks.py list
python3 tasks.py get 1
python3 tasks.py update 1 --title "Buy oat milk" --done
python3 tasks.py delete 1
python3 -m unittest discover -v
```

Data is stored in `tasks.sqlite3`, ignored by Git. Use
`python3 tasks.py --db /path/to/test.sqlite3 list` for a separate database.
Missing tasks and blank titles produce an error with exit code 1.

## Real Codex exercise

`ticket.md` asks for completion-status filters. This checked-in fixture preserves
the **before** version, so it can be reused for future experiments.

The local exercise copies the fixture into an independent Git repository at
`../../.dev-agent/demo/repository`, runs the installed dev-agent commands, then
uses an isolated worktree at `../../.dev-agent/demo/worktree` for a real Codex
session. The finished implementation and its tests live in that worktree.

The exercise is manually coordinated; Milestone 1 does not implement
`dev-agent start`, agent orchestration, or resume. `doctor` checks executable
availability, not provider login or model access.

See `DEMO-RESULT.md` for the actual result and artifact locations.
