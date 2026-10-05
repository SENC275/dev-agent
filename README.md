# dev-agent

A local CLI that turns tickets into investigated, implemented, tested, and reviewed
code changes. Use Codex, Claude Code, or Ollama, with a different provider for each role.

```text
Ticket → Investigate → Plan → Approve → Implement → Validate → Review → Human review
```

Changes run in an isolated Git worktree. You inspect the result, request revisions,
and decide when to merge. Failed checks can trigger bounded repair attempts.

## Install

Requires macOS/Linux, Python 3.12+, Git, and at least one configured provider.
Install and authenticate Codex or Claude Code separately, or configure an Ollama server.

```bash
git clone https://github.com/SENC275/dev-agent.git
cd dev-agent
uv tool install .
# Alternatively: pipx install .
```

## Configure your project

Run inside the Git repository you want agents to work on:

```bash
cd /path/to/your-project
dev-agent init
# Edit .dev-agent.yaml for your providers and validation commands.
dev-agent doctor
```

All roles default to Codex. Set `commands.test`, `commands.lint`, and
`commands.typecheck` to checks that actually work in your project.
`doctor` checks local prerequisites; it does not verify model authentication.

Root `AGENTS.md` and `CLAUDE.md` provide project instructions. Use them to point
agents to relevant conventions and existing documentation.

For provider configuration, see the [default config](.dev-agent.example.yaml),
[Claude/mixed example](examples/claude.yaml), and [Ollama example](examples/ollama.yaml).
Adapt model names, server addresses, and role assignments to your environment.

## Run tests manually

```bash
dev-agent test
# From a worktree without a local YAML, use the original project's configuration:
dev-agent test --config /path/to/your-project/.dev-agent.yaml
```

Runs only `commands.test` in the current directory, without AI, a ticket, or a clean
Git baseline. Uses `validation.timeout_seconds`; prints a heartbeat while waiting and
stdout/stderr after completion. Returns the command's exit code (124 on timeout).
Commands use the same argument parsing as workflow validation: use explicit
`sh -c 'first && second'` for shell operators. This manual check does not change ticket
status or replace the workflow's recorded validation and review.

## Create and run a ticket

Generate a draft from a description using your configured planner:

```bash
dev-agent ticket DEMO-001 --description "Add optional done filtering to GET /tasks, filter before pagination, and add regression tests."
# Or: dev-agent ticket DEMO-001 --file /path/to/requirement.md
```

Review `.dev-agent/tickets/DEMO-001.md`, resolve open questions, and edit the acceptance
criteria as needed. Generation only creates a draft; implementation starts separately.
You can also write your own Markdown ticket.

Before starting, commit intended project changes and ensure `git status --short`
is empty. This gives the worktree a reproducible baseline. `init` excludes `.dev-agent.yaml` and `.dev-agent/` through Git’s local
`info/exclude`, without changing `.gitignore`. Tickets, configuration, and run records
do not need commits. Keep configuration unchanged during a run. Existing tracked files
remain tracked; exclusions do not remove them from Git history.

```bash
dev-agent start DEMO-001 --file .dev-agent/tickets/DEMO-001.md
dev-agent status DEMO-001
```

By default, you approve the plan before implementation. To use an independent
agent for early plan approval, set this in `.dev-agent.yaml` before starting:

```yaml
gates:
  plan_review: agent
  approve_final: true
```

The workflow prints progress while agents work. If it stops, inspect `status` and
its referenced artifacts; use `dev-agent resume DEMO-001` when the issue is resolved.
Some interrupted or changed states require manual recovery rather than automatic replay.

## Review, revise, and merge

When ready for human review, inspect the `worktree_path` reported by `status`:

```bash
git -C /path/to/worktree status --short
git -C /path/to/worktree diff
```

Inspect newly added files too: ordinary `git diff` does not display untracked files.
Run revisions and merges from the original project root:

```bash
dev-agent revise DEMO-001
# Or: dev-agent revise DEMO-001 --file /path/to/feedback.md

dev-agent merge DEMO-001 --dry-run
dev-agent merge DEMO-001
```

`revise` accepts feedback and runs another implementation and verification cycle.
`merge` asks for confirmation, commits the reviewed changes, and fast-forwards the
source branch. It requires current passing checks and an unchanged source baseline;
it does not push, deploy, or apply database migrations.

## Reusable knowledge

Knowledge documents are disabled by default so tool-generated documents do not enter
code commits. Investigation records remain local in `.dev-agent/`. To explicitly opt
in to reviewing and committing knowledge documents, configure this before a new ticket:

```yaml
knowledge:
  enabled: true
```

The implementer drafts `docs/knowledge/<ticket>.md` from investigation and final code.
Candidates include scope, source references, the base commit, and recheck conditions.
The independent reviewer checks them alongside code; `revise` can correct or reject
them. Final merge accepts both together, without another early approval gate.
See [knowledge workflow](docs/knowledge-workflow.md) for details.

## More

- [CRUD demo](examples/crud-app/README.md)
- [Detailed design and milestone history](docs/design-and-history.md)
- `dev-agent --help` or `dev-agent <command> --help`

For development:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy dev_agent
```

### Continue a paused ticket

From the source repository, run `dev-agent resume DEMO-001`. A completed, paused
fix cycle is revalidated and reviewed against the current worktree, including manual
repairs. Existing implementation and fix history are preserved.

If the fix budget is exhausted, explicitly grant more attempts:

```sh
dev-agent resume DEMO-001 --additional-fix-cycles 2
```

The extra budget is recorded with the ticket; the frozen YAML configuration stays
unchanged. Plain `resume` never replenishes it. Interrupted or uncertain writes still
require inspection and are not replayed automatically.

Agent plan review corrects an invalid structured response once. An actual rejection
returns feedback to the read-only planner, then independently reviews the revised
plan. `limits.max_plan_revisions` defaults to 2 (0 disables automatic plan changes).
The budget persists across resume; exhausted plans require human review. Responses
and plan versions stay in `.dev-agent/`. Progress shows `correct_output` or
`revise_plan`; failures distinguish `model_output`, `provider_execution`, and
`plan_rejected`. No invalid response or rejected plan grants implementation approval.
