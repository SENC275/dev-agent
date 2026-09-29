# Real Codex CRUD exercise

Status: passed. This was a manually coordinated real Codex CLI run, not a
`dev-agent start` run. The product remains at Milestone 1.

## What ran

1. Created the standard-library SQLite CRUD fixture and verified its five tests
   plus create/read/update/delete CLI behavior.
2. Copied it into a standalone local Git repository, ran `dev-agent init`,
   adjusted validation commands for this fixture, and ran `dev-agent doctor`.
3. Created branch `codex/DEMO-001` in an isolated Git worktree.
4. Invoked the real Codex CLI with the sample ticket and workspace-write sandbox.
5. Independently inspected the resulting diff and reran validation.

The existing default `gpt-5.4` model was rejected by the account. The retry used
`gpt-6-astra`, listed in the local model catalog, and completed with exit code 0.
No global model settings were changed. This exposed a known boundary of the
current doctor command: executable presence does not establish model access.

## Change and results

Codex changed only `tasks.py` and `test_tasks.py`:

- Added `list --done` and `list --no-done`.
- Preserved unfiltered output and ascending ID order.
- Added four regression tests, including actual CLI subprocess execution.

Independent validation:

- Modified application: 9 tests passed.
- `ruff check .`: passed.
- `mypy --strict tasks.py`: passed.
- `git diff --check`: passed.
- CLI acceptance: two tasks; completed filter returns one and incomplete filter
  returns the other.
- Existing dev-agent tests: 20 passed; its mypy and repository ruff checks passed.

The diff was inspected in this parent session; a separate fresh AI reviewer
session was not run. There was no automatic approval gate, fix loop, or resume.

## Inspect or reproduce

`DEMO-001.patch` is the real Codex-generated change against this directory's
baseline `tasks.py` and `test_tasks.py`. Apply it in a copy of this fixture:

```bash
git apply DEMO-001.patch
python3 -m unittest discover -v
python3 tasks.py create "Buy milk"
python3 tasks.py create "Write tests"
python3 tasks.py update 2 --done
python3 tasks.py list --done
python3 tasks.py list --no-done
```

Current local artifacts (relative to the dev-agent project root):

- `.dev-agent/demo/worktree/`: runnable modified app and added tests.
- `.dev-agent/demo/repository/`: standalone source repository.
- `.dev-agent/demo/prompt.txt`: implementation request.
- `.dev-agent/demo/doctor.txt`: actual doctor output.
- `.dev-agent/demo/codex.log`: initial rejected call.
- `.dev-agent/demo/codex-retry.log`: successful real Codex session.
- `.dev-agent/demo/implementation.md`: Codex's own summary.
- `.dev-agent/demo/validation.json`: independently captured check results.

These runtime files are intentionally ignored by Git. The fixture, ticket,
patch and this report remain shareable in `examples/crud-app/`.

Invocation reference: [official Codex non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
