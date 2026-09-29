# dev-agent

## Current status — Milestones 1–12

A reusable [CRUD sandbox and real Codex exercise](examples/crud-app/README.md)
is available for integration experiments.

Install from this checkout (Python 3.12+):

```bash
uv tool install .
# Alternatively:
pipx install .
```

Then, inside your target Git repository:

```bash
dev-agent --help
dev-agent init
dev-agent doctor
```

`init` creates `.dev-agent.yaml` without overwriting existing files. All seven
roles default to Codex. Adjust validation commands for your repository; the
example assumes a Python project. Provider credentials stay in the provider's
existing authentication setup, never in this configuration.

`doctor` checks Python, Git, the working tree, configuration, and required CLI
executables on PATH. It does not check authentication or invoke a model.
Failures exit with code 1 and an actionable message. Codex CLI, Claude Code CLI and Ollama providers are
supported; unused providers do not make checks mandatory.

### Generate a ticket from a description

Run from the configured target repository root (with at least one Git commit):

```bash
dev-agent ticket DEMO-API-003 --description "Add optional done filtering to GET /tasks; filter before pagination, preserve the response fields, and add regression tests."
# Or read a description from a UTF-8 file:
dev-agent ticket DEMO-API-004 --file /absolute/path/to/requirement.md
# Or enter the description at an interactive prompt:
dev-agent ticket DEMO-API-005
```

This uses the configured `planner` provider to read relevant code, tests, and
repository instructions, then saves `tickets/<ID>.md`. The draft includes the
original request, current behavior, testable acceptance criteria, suggested files,
scope boundaries, configured validation commands, assumptions, and open questions.
It does not start implementation, run validation, or create a managed workflow.
Progress messages remain visible while the provider works.

Review and edit the Markdown, especially assumptions and open questions. Ticket
generation allows existing uncommitted changes, but `start` requires a clean,
committed baseline: commit the reviewed ticket and other intended source changes
first, then run:

```bash
dev-agent start DEMO-API-003 --file tickets/DEMO-API-003.md
```

Descriptions are limited to 32 KB; `--description` and `--file` are mutually
exclusive. Existing ticket files are never overwritten. Generation checks for
repository changes during drafting and refuses to save if it detects them.

### Run a ticket

Milestone 12 adds a durable workflow (macOS/Linux):

```bash
# Run from the target repository root, with a clean committed baseline.
dev-agent start DEMO-12 --file /absolute/path/to/ticket.md
dev-agent status DEMO-12
dev-agent resume DEMO-12
```

By default, `start` runs investigation and planning, then displays the plan for human approval.
In an interactive terminal, approval continues through isolated implementation,
validation, independent review, bounded fixes, and final validation. In a
noninteractive terminal it exits 2 at the approval gate. Use `resume` interactively,
or `review-plan <plan-directory>` followed by `resume`. No automatic approval,
commit, merge, or deployment is performed.

`.dev-agent/state.sqlite3` stores managed runs, stage attempts, timestamps, state,
configuration, and artifact/worktree pointers. Detailed provider results, findings,
diffs, and command output remain in existing artifact files. `status` shows the
last recorded state without invoking a provider; it is not a live certification.
`resume` checks a completed result against current approval, worktree identity,
and code fingerprint before returning it as ready again.

Each ticket ID has one managed run per repository. A repeated `start` is rejected;
use `resume` or a new ID. Configuration must stay unchanged for a managed run,
including the fix budget. An OS file lock prevents concurrent managed advances.
Read-only investigation/planning/review attempts can be retried on explicit
resume; previous artifacts remain available. Completed stages are skipped.

Recovery is deliberately conservative: an incomplete worktree, implementation,
validation-command, or fix stage is **not automatically replayed**, even if the
process died between writing its artifact and recording completion in SQLite.
The run stops at `WAITING_FOR_HUMAN`. Inspect the recorded plan directory and
worktree; after diagnosing the issue, use the standalone stage commands to
continue manually. Those manual results are not automatically imported into the
managed journal. Do not delete locks while a provider may still be running.
A database state of `RUNNING` after process death is the last persisted state;
`resume` applies these recovery rules. Existing standalone runs are not imported.
Exit codes are 0 for ready, 2 for a human gate, and 1 for an execution error.

### Configurable plan approval

To let an independent agent handle the early plan review, set this in
`.dev-agent.yaml` **before starting a new ticket** and commit the configuration:

```yaml
gates:
  plan_review: agent   # human (default) or agent
  approve_final: true
```

The plan reviewer uses the configured `reviewer` provider in a **fresh read-only
session**, separate from the planner and later code review. It receives the
plan, ticket, investigation evidence, and a strict approval schema. A passing
review saves `plan-review.json` and an approval record marked `actor: agent`,
bound to the exact plan, evidence, base commit, and review artifact. Implementation
then continues automatically. Editing the reviewed content invalidates approval.
No additional provider or role configuration is required.

A `request_changes` decision stops at the human plan gate with review findings;
provider failures, malformed output, and detected source changes never approve a
plan. This version does not automatically rewrite rejected plans. A human can
edit/approve with `review-plan`, then `resume`. Final code review still ends at
`READY_FOR_HUMAN_REVIEW`; it does not commit, merge, deploy, or approve the final
result on your behalf. `approve_plan` remains true (review is never skipped).

`plan_review: human` preserves the earlier behavior. Existing journal snapshots
without the new field load as human mode. Configuration is still fixed for each
managed run: changing modes applies to new tickets, not an in-flight run.
Both `start` and the standalone `plan` command honor this setting.

### Accept and merge a reviewed ticket

From the original repository root:

```bash
dev-agent merge DEMO-12 --dry-run
dev-agent merge DEMO-12
# Explicit acceptance in noninteractive use:
dev-agent merge DEMO-12 --yes --message "Complete DEMO-12"
```

The command displays source/worktree branches and changed files before asking for
confirmation. It requires a clean source checkout still at the approved base,
current plan approval, passing validation, and a clear independent review matching
the exact worktree snapshot. Unreviewed files and pre-staged changes block merging.
The destination is the source checkout's current branch, displayed for acceptance;
it does not assume that branch is named `main`.

It commits reviewed changes in the worktree using your Git author identity, then
fast-forwards the source branch. This initial merge command expects changes to remain uncommitted, as produced by
the workflow. If you already committed manually, use Git to merge that branch. Hooks and commit
signing are disabled for these automated Git operations. No push, database
migration, deployment, or branch/worktree deletion takes place. Diverged or
advanced source branches require manual reconciliation and revalidation.

A manually recovered failed revision may be accepted when the latest validation
and review artifacts both certify its current contents. The earlier failed steps
remain in history; a successful merge records `MERGED` and saves `merge.json`.
Any partial merge failure preserves the worktree/commit and records `MERGE_FAILED`.
Inspect Git and the receipt before manual recovery; the command does not replay
uncertain Git operations. Do not concurrently edit or switch these checkouts while
merging. `--dry-run` does not stage, commit, or merge files.

### Human revisions and progress

After a managed run reaches `READY_FOR_HUMAN_REVIEW`, run from its original
repository root:

```bash
dev-agent revise DEMO-12
# Type your feedback and press Enter to submit.
# For multiline feedback or noninteractive use:
dev-agent revise DEMO-12 --file /tmp/feedback.md
```

Submitting feedback authorizes another implementation attempt in the same
worktree. The original approved plan remains intact; feedback is an explicit
amendment, with later feedback taking precedence where requirements conflict.
Each round is saved under `<plan_path>/revisions/`, including feedback, execution
results, and status. The independent reviewer and fixer receive all human
feedback, without the revision agent's summary. A revision runs validation,
independent review, bounded fixes, and final validation, then updates the managed
journal. The existing cumulative automatic fix budget is preserved.

Keep feedback files outside the source checkout (or ignored) to preserve the
clean source requirement. Revise currently accepts completed, unchanged results;
uncommitted manual edits after the last validation or failed/interrupted revisions
require manual inspection. `resume` never automatically repeats an uncertain
revision write. Revision locks also block standalone validation/review/fix stages.
No database migration is needed for existing Milestone 12 tickets.

During agent work, the CLI shows an animated indicator, stage, and elapsed time.
Noninteractive output prints a waiting heartbeat every 15 seconds. The indicator
means the local workflow is waiting, not that the model has reported progress;
no completion percentage is invented. Approval and feedback input happen outside
the animation. Provider timeouts continue to apply.

Milestone 2 adds `dev_agent.process.run_process`, a shared asynchronous,
shell-free process runner. `doctor` now uses it for the Git repository check.
It accepts an argument sequence, working directory, optional UTF-8 stdin and a
finite positive timeout (60 seconds by default). Results include the command,
resolved cwd, UTC start time, elapsed seconds, real exit code, stdout, stderr and
`timed_out`. Nonzero exits are results; launch failures raise `OSError` subclasses.
Cancellation cleans up the process and propagates to the caller.

On macOS/Linux, timeout and cancellation kill the process group, including
ordinary descendants holding output pipes. On other platforms, only the direct
child is terminated. Descendants that deliberately detach into another session
are outside this guarantee. Output is buffered in memory, and invalid UTF-8 bytes
are replaced. The timeout covers communication after process creation, with
cleanup performed before returning. This is process management, not a security
sandbox.

Milestone 3 adds `AgentProvider`, validated `AgentTask` / `AgentResult` models,
`CodexCLIProvider`, a deterministic `FakeProvider`, and `ProviderRegistry` for
role-based resolution. No new runtime dependencies are required.

Providers may set `model` and `timeout_seconds` (default: 600) in configuration.
Omitting `model` preserves the user's Codex model selection; no automatic model
fallback is attempted. Each execute call starts a fresh `codex exec --ephemeral`
session with a read-only sandbox by default, no interactive approvals, and the
prompt supplied over stdin. Explicit `read_only=False` selects workspace-write;
the caller must supply an authorized, isolated worktree. The provider itself does
not implement approval gates or create worktrees.

The adapter reads only the final-message file as `output`, retaining stderr for
diagnosis. Timeout, nonzero exit, missing/empty output and invalid UTF-8 are failed
results; process launch errors propagate as `OSError` subclasses. Cancellation
propagates after cleanup. Temporary response files are removed after each call.
Results report provider execution, not whether generated code passes validation.
Use existing Codex authentication; do not put credentials in project config.

The adapter is exercised against Codex CLI 0.153.4. Older versions may lack required
flags and will fail explicitly; there is no compatibility fallback. See the
[official non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode).

Ordinary `pytest` runs use fakes and skip the real integration test. To opt in
(requires Git, an authenticated Codex CLI, network, and provider usage):

```bash
DEV_AGENT_RUN_CODEX_TEST=1 DEV_AGENT_CODEX_MODEL=your-model-name \
  uv run pytest tests/integration/test_codex_cli.py -v
```

The opt-in test runs in a temporary Git repository with a read-only task. The
`DEV_AGENT_CODEX_MODEL` environment variable is test-only; application code uses
the provider's configuration.

Milestone 4 adds `Exploration`, `PatternAnalysis`, `TestAnalysis`, `Finding`,
and `Review` artifact models plus `dev_agent.artifacts.parse_artifact`.

```python
from dev_agent.artifacts import parse_artifact
from dev_agent.models.finding import Review

review = parse_artifact('{"findings": []}', Review)
print(review.model_dump_json(indent=2))
schema = Review.model_json_schema()
```

The core fields shown in the examples below are required; explicit empty lists
are valid when there are no observations/findings. Investigation artifacts also
accept optional `assumptions` and `unresolved_questions` lists, separate from
observations and recommendations. A pattern must cite at least one nonblank file
reference. Findings require one of the four documented severity levels and a
positive integer line number; booleans and numeric strings are not accepted as
line numbers. Unknown fields and blank text are rejected. Surrounding whitespace
in text fields is trimmed.

Parsing accepts one complete JSON value and validates it as the requested object
schema. Markdown fences, trailing prose, duplicate keys at any depth, NaN and
Infinity are rejected without attempting repairs. `ArtifactValidationError`
reports the artifact type and field location/error code (or JSON line/column),
without including invalid field values or the full response.

These checks validate structure, not factual correctness: file references and
line numbers are not checked against repository contents, and advisory severity
never approves code. The investigation stage persists these artifacts; no provider-specific
structured-output flags are added yet.

Milestone 5 adds a runnable investigation command. From the target repository
with `.dev-agent.yaml` and an authenticated Codex CLI:

```bash
dev-agent investigate DEMO-001 --file ticket.md
```

Set `providers.codex.model` in `.dev-agent.yaml` if the CLI's default model is
not available to your account. This command makes three real provider calls,
concurrently, and consumes provider usage. It stops before planning or editing.
The prompt templates are bundled in the installed package.

Each role receives the ticket, repository path, its own instructions and the
expected JSON schema. Every task explicitly selects read-only mode. The
orchestrator writes only run artifacts under:

```text
.dev-agent/runs/DEMO-001/<unique-run-id>/
  ticket.md
  investigation.json
  exploration.json
  patterns.json
  tests.json
```

Run from the directory containing `.dev-agent.yaml`. Add `.dev-agent/` to the
target repository's ignore rules if needed; this command does not edit those rules.
Only validated role results are saved, using temporary files and atomic rename.
A failed role does not discard successful peers. The manifest records each role's
status through its artifact/error fields and the overall COMPLETE or FAILED
result. CLI exit code 1 means investigation failed; rerunning creates a new run,
not a resume. Cancellation records INTERRUPTED when cleanup is possible. An
abrupt process kill can leave RUNNING and incomplete artifacts; this is not yet
a durable workflow/resume implementation. No automatic retries are performed.
Raw provider output and stderr are not persisted by this stage.

Structural validity does not establish factual accuracy. Investigation uses the
provider sandbox and prompts to constrain access; it is not an independent audit
of every file read or a guarantee about external provider tools.

Milestone 6 generates and reviews plans from a COMPLETE investigation:

```bash
# Use the actual directory printed by investigate:
dev-agent plan .dev-agent/runs/DEMO-001/<run-id>
# Reopen the plan directory printed by plan (no new model call):
dev-agent review-plan .dev-agent/runs/DEMO-001/<run-id>/plans/<plan-id>
```

`plan` revalidates the ticket and investigation artifacts, uses the recorded
repository's configuration, and starts a fresh read-only planner. The repository
must have a Git commit. Every generation gets a new directory containing `plan.md`
and `planning.json`; previous plans are not overwritten. The planner must produce
all nine documented level-two headings in order, with nonempty section content.

Rich displays the plan. In an interactive terminal, choose A to approve, E to edit
using VISUAL or EDITOR (default vi), or Q to quit. The editor receives a temporary
copy; only valid edits replace the saved plan, and the updated plan is displayed
again before approval. Editor commands are split into argv, never executed through
a shell. A failed editor leaves the saved plan unchanged.

Non-interactive invocations save/display the plan and exit 2 without prompting or
approving. Q also exits 2; success exits 0 and errors exit 1. Run `review-plan` in
an interactive terminal later. No automatic approval flag is provided.

Approval writes `approval.json`, binding the exact displayed plan SHA-256, input
artifact hashes and Git HEAD. Editing clears prior approval. External plan edits
make its hash stale; changed investigation inputs or Git HEAD require regeneration.
`approval_is_current` checks the content binding (and raises on stale inputs/HEAD).
`planning.json` records the generation's initial awaiting-approval state; consumers
must validate `approval.json`, not infer approval from its existence alone.
This is a local audit record, not a tamper-proof signature. Milestone 7 records whether the repository is clean before and after planning;
worktree creation requires that clean baseline and a currently clean repository. Human judgment is still needed
for assumptions and unresolved questions.

Approval stops here: no worktree, implementation, merge or deployment is performed.

Milestone 7 creates an isolated worktree from a current human-approved plan:

```bash
dev-agent worktree .dev-agent/runs/DEMO-001/<run-id>/plans/<plan-id>
```

The command uses the recorded repository's configuration. Defaults are
`codex/{ticket}` for the new branch and `../worktrees/{ticket}` for the directory
(relative to the repository root). Absolute worktree paths are also supported.
Only `{ticket}` is accepted as a template field. The directory must not already
exist, be a symlink, or overlap any registered worktree. Existing branches are
rejected, including when they are not checked out. Templates and Git branch names
are validated before creation. The source must be the repository root.

The plan must have a valid approval, the same Git HEAD, unchanged investigation
inputs, and a recorded clean baseline. Plans generated before Milestone 7 must be
regenerated and approved. Plans generated with uncommitted changes may still be
reviewed, but cannot create a worktree: first commit or preserve those changes
manually and generate a new plan. Tracked staged/unstaged changes and untracked
non-ignored files all block creation; untracked `.dev-agent/` artifacts are exempt.
Ignored files are not copied to the worktree. This is a clean-commit workflow,
not a mechanism for transferring local edits or secrets.

The new branch is created from the exact approved commit and verified afterwards.
The source checkout and index are left untouched. Checkout hooks are disabled for
this operation. An exclusive per-plan lock and target-directory reservation
prevent accidental reuse; `worktree.json` records repository, path, branch and base
commit. Repeated creation for the same plan is refused.

Failures/timeouts can leave a branch or reserved directory behind; no automatic
rollback, force, reset, clean or deletion runs. Inspect `git worktree list` and
local branches/paths before retrying. A hard interruption can leave `worktree.lock`;
remove it manually only after confirming no creation is still running. Avoid
concurrent manual repository/plan edits during creation. These local audit files
are not tamper-proof security credentials.

Worktree creation stops before implementation or validation. It does not push,
merge, or deploy anything.

Milestone 8 executes one implementation attempt in the recorded worktree:

```bash
dev-agent implement .dev-agent/runs/DEMO-001/<run-id>/plans/<plan-id>
```

This command makes a real provider call and consumes provider usage. It requires
a current approved plan, clean source checkout and worktree, and matching
repository, registered worktree, branch and approved base commit. An existing
implementation record or active lock prevents automatic repetition. It never
creates a missing worktree or repairs a mismatched one.

A fresh implementer session uses workspace-write mode inside the worktree. Its
input contains the ticket, approved plan, and root AGENTS.md / CLAUDE.md files when
present. Symlink instruction files are rejected; the prompt also requires reading
applicable nested instructions before editing. The prompt prohibits modifications
outside the worktree, Git mutations, dependency installation, credential access
and running validation. The task is to implement the smallest approved change and
add/update tests, then report assumptions and unresolved issues.

`implementation.json` is saved alongside the plan with RUNNING, IMPLEMENTED,
FAILED or INTERRUPTED status; timestamps, provider exit/timeout/duration, worktree
identity, approval hashes, and changed files read directly from Git are recorded.
New non-ignored files and tracked changes relative to the approved base are included.
The agent's summary is unverified (`summary_verified: false`), and
`validation_status` remains NOT_RUN. IMPLEMENTED means the provider attempt
completed successfully and postconditions passed, not that requirements or tests
are verified. Raw stderr is not saved by this stage.

Execution checks detect unexpected staging/commits, branch changes, source tree
changes and stale approval. A failure or cancellation preserves partial edits;
there is no automatic retry, rollback, commit, push or cleanup. Inspect the record
and Git diff before any manual recovery. Hard termination can leave RUNNING or an
implementation lock; these are not resumable workflow states yet. Avoid concurrent
manual edits while the agent runs. Prompt rules and postchecks supplement the
provider sandbox; they do not undo unwanted edits or guarantee isolation from
provider tools outside that sandbox. Ignored generated files are not enumerated
in changed_files.

Milestone 9 runs deterministic checks in the implementation worktree:

```bash
dev-agent validate .dev-agent/runs/DEMO-001/<run-id>/plans/<plan-id>
```

The recorded implementation must be IMPLEMENTED and still match the approved
plan, repository, worktree, branch and base commit. Commands come from the source
repository's `.dev-agent.yaml`, not the potentially edited worktree config. No
provider or LLM is invoked. Validation runs test, then lint, then typecheck,
stopping at the first nonzero exit, timeout or process launch failure. Later
checks remain NOT_RUN. All three checks must pass for the CLI to exit 0; failures
exit 1. No fixer or reviewer is started.

Configure the existing `commands.test`, `commands.lint`, `commands.typecheck`
strings and optional `validation.timeout_seconds` (positive, finite; default 300
seconds per command). Strings are split with `shlex.split` and executed directly:
quotes group arguments, but shell pipes, redirection, variable expansion and `&&`
are not interpreted. Use a repository script for multi-step checks. Commands run
with the current environment/PATH and worktree cwd; dependencies must already be
available. The tool does not install them or load `.env`. These are trusted local
commands, not sandboxed execution; choose checks that avoid external side effects
and printing credentials.

Each attempt saves `validations/<validation-id>/validation.json` under the plan
and updates the plan's `validation.json` with the same latest result. RUNNING is
written before execution, replacing any prior pass indication. Records include
command text, parsed argv, cwd, UTC start time, duration, exit code, stdout, stderr,
timeout/error and per-check status. An unstarted command has a null exit code,
not zero. Output remains buffered in memory. History is preserved on reruns.
Cancellation records INTERRUPTED and cleans up the active process using the shared
runner. Hard termination can leave RUNNING or a lock requiring manual inspection.

Validation refuses overlapping runs and checks identity/approval around execution;
staging, commits and source checkout changes are not accepted. Configured tests
may produce generated/ignored files. Milestone 10 binds results to a code fingerprint and rejects relevant content
changes during validation. Rerun validation after editing code; old results cannot
be used for a different fingerprint. Historical
`implementation.json` remains unchanged; validation results are authoritative for
this separate stage. Independent review is still required.

Milestone 10 runs an independent review of the validated change:

```bash
dev-agent review .dev-agent/runs/DEMO-001/<run-id>/plans/<plan-id>
```

The fresh reviewer receives only the original ticket, approved plan, actual Git
diff (including non-ignored untracked additions), validation results and findings
schema. It does not receive the implementer's conversation or summary. The task
runs read-only and is instructed to inspect relevant code and report actionable
correctness, regression, edge-case and coverage findings. It does not run fixes.

Validation now records a SHA-256 fingerprint covering base commit, tracked and
non-ignored untracked file contents/modes, symlink targets, and staged changes.
The code fingerprint must remain unchanged during validation and must match at
review entry and exit. Review also checks worktree identity, approval and the
exact validation artifact. Validation records created before Milestone 10 must
be regenerated. Ignored files (such as build caches or dependencies) and untracked
`.dev-agent/` artifacts are outside this fingerprint. This is not a hermetic build
or a guarantee against edits that occur and revert between checks.

Snapshot limits: ordinary files up to 2 MiB each are supported; submodules and
nonregular files fail explicitly. Tracked/non-ignored `.env` and `.env.*` files
are rejected without reading their contents. Ignored secret files are not scanned.
Untracked text is included as additions; binary additions are represented by name
and content digest. Symlinks are not followed for their contents. External Git
diff programs and text conversion are disabled.

Each attempt saves `reviews/<review-id>/diff.patch`, `review-status.json`, and,
on success, validated `review.json`. The plan directory has the latest
`review-status.json` and `review.json`. Old successful reviews remain in history;
a new attempt clears the latest findings until it succeeds. Provider failures,
invalid JSON, changed inputs and interruption never produce a successful review.
Implementation/validation overlap is refused, and a review lock prevents duplicate
review calls. No model calls are required by the ordinary tests.

The CLI reports counts for critical/high/medium/low findings. Exit 0 means the
review completed, even when findings exist; severity does not approve, merge or
reject code automatically. REVIEWED is an execution status, not a human approval
or READY_FOR_HUMAN_REVIEW. Inspect both the findings and their matching status,
validation and fingerprint. Factual accuracy still requires engineering judgment.

Milestone 11 runs bounded fix and revalidation cycles:

```bash
dev-agent fix .dev-agent/runs/DEMO-001/<run-id>/plans/<plan-id>
```

The command requires a current approved plan, matching worktree identity, passed
validation and a successful review for the same fingerprint. It compares latest
findings with the archived review so an edited findings file is not silently used.
It starts a fresh workspace-write fixer, with the ticket, approved plan, current
diff, findings, repository instructions and any preceding validation failure.
The fixer must verify every finding and produce exactly one evidence-backed
`fixed`, `rejected` or `deferred` decision for each zero-based finding index.
Schema violations or provider failures stop immediately, preserving partial edits.

Every completed fix attempt runs deterministic validation. If it fails, its output
is supplied to the next fixer within the remaining budget. If it passes, a fresh
independent reviewer runs without the fixer's conversation/report. Rejecting a
finding never clears it automatically: the independent review must be clear.
Deferred issues always require human judgment, even when the reviewer is clear.

`limits.max_fix_cycles` defaults to 2 and permits zero. The budget counts fixer
attempts, including failed/interrupted calls, across the plan's `fixes/0001`,
`fixes/0002`, etc. directories. An attempt directory is reserved before the provider
call, so rerunning the command cannot reset its consumed budget. Only an explicit
configuration change can raise the limit; do not delete history to bypass it.

Each attempt preserves input findings, `attempt.json` and a validated `fix.json`
when available. Validation/review retain their existing per-run histories.
`fix-cycle.json` reports RUNNING, FAILED, INTERRUPTED, WAITING_FOR_HUMAN or
READY_FOR_HUMAN_REVIEW. When independent review is clear, another deterministic
validation produces `final-validation.json`. Only a pass for the same code makes
the change READY_FOR_HUMAN_REVIEW. This status does not approve or merge it.

Exit codes: 0 = ready for human review; 2 = budget exhausted, deferred issues or
final validation failure requiring a human; 1 = execution/precondition error.
A cycle lock blocks standalone implement/validate/review commands while the cycle
owns those stages. Cancellation preserves edits and consumed budget. Hard kills
can leave a lock and partial artifacts; inspect them before manual recovery.
This is not yet SQLite-backed resume. A failed/changed validation or review must
be re-established before starting another cycle. After final validation the latest
validation run differs from the preceding review's run; to start a new cycle,
explicitly review that latest validation first. Historical ready status is valid
only for its recorded plan/code fingerprint, not later edits.

No commit, push, merge, deployment or automatic final approval is performed.
The snapshot/provider limits documented above still apply, and model disposition
reports are claims requiring independent checking, not proof of correctness.

End-to-end ticket execution and durable resume are future milestones.
The remainder of this README describes the planned workflow, not current features.
This package has not been published to a package registry.

Development:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy dev_agent
uv build
```

---

A local, provider-agnostic engineering agent harness for turning software tickets into investigated, planned, implemented, tested, and independently reviewed code changes.

`dev-agent` does **not** attempt to build a new AI coding model.

It orchestrates existing coding agents such as:

* OpenAI Codex
* Claude Code
* future CLI/API-based coding agents

Only **one provider is required**.

V1 is designed to work completely with **Codex only**.

Claude is optional and is **not a dependency**.

---

# 1. Vision

The normal AI-assisted development workflow is fragmented:

```text
Read ticket
    ↓
Ask AI to investigate
    ↓
Ask AI for implementation plan
    ↓
Review plan
    ↓
Ask AI to implement
    ↓
Run tests
    ↓
Ask another AI/session to review
    ↓
Fix findings
    ↓
Run tests again
    ↓
Prepare PR
```

`dev-agent` turns this into a repeatable engineering workflow:

```text
Ticket
   ↓
Parallel Investigation
   ↓
Evidence
   ↓
Implementation Plan
   ↓
🛑 Human Approval
   ↓
Isolated Git Worktree
   ↓
Implementation Agent
   ↓
Deterministic Validation
   ↓
Independent Review Agent
   ↓
Fix / Re-validation
   ↓
🛑 Human Review
   ↓
PR Ready
```

The durable abstraction is:

```text
Ticket
   ↓
Evidence
   ↓
Plan
   ↓
Human Gate
   ↓
Change
   ↓
Deterministic Validation
   ↓
Independent Review
   ↓
Human Gate
```

That protocol is the product.

---

# 2. Core Principles

## 2.1 Provider agnostic

Workflow logic must never depend directly on Codex, Claude, or another vendor.

The workflow works with **roles**:

```text
explorer
pattern_researcher
test_researcher
planner
implementer
reviewer
fixer
```

Configuration maps roles to providers.

Example using only Codex:

```yaml
providers:
  codex:
    type: codex_cli

roles:
  explorer:
    provider: codex

  pattern_researcher:
    provider: codex

  test_researcher:
    provider: codex

  planner:
    provider: codex

  implementer:
    provider: codex

  reviewer:
    provider: codex

  fixer:
    provider: codex
```

A company environment could later use:

```yaml
providers:
  codex:
    type: codex_cli

  claude:
    type: claude_cli

roles:
  explorer:
    provider: claude

  pattern_researcher:
    provider: claude

  test_researcher:
    provider: claude

  planner:
    provider: claude

  implementer:
    provider: codex

  reviewer:
    provider: claude

  fixer:
    provider: codex
```

The workflow must behave the same way.

---

# 3. V1 Provider Strategy

V1 supports:

```text
Codex CLI
```

only.

The architecture must allow additional providers later, but **do not implement Claude support in V1**.

This is intentional.

Do not build unused integrations.

The initial abstraction should look conceptually like:

```python
from abc import ABC, abstractmethod


class AgentProvider(ABC):
    @abstractmethod
    async def execute(
        self,
        task: AgentTask,
    ) -> AgentResult: ...
```

V1 implementation:

```python
class CodexCLIProvider(AgentProvider): ...
```

Possible future implementations:

```text
ClaudeCLIProvider
GeminiCLIProvider
OpenAIAgentProvider
LocalModelProvider
```

These are future possibilities, not V1 requirements.

---

# 4. Independent Agents Do Not Require Different Providers

Independent review does **not** mean that the reviewer must use Claude while the implementer uses Codex.

This is valid:

```text
Codex Session A
    ↓
implementation

Codex Session B
    ↓
independent review
```

The important property is **context isolation**.

The reviewer should receive:

```text
ticket
approved plan
git diff
validation results
```

It should NOT inherit the implementation agent's conversation.

Therefore a Codex-only workflow is:

```text
                     Ticket
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
       Codex A        Codex B       Codex C
       Explorer       Patterns       Tests
          │             │             │
          └─────────────┼─────────────┘
                        ▼
                     Codex D
                     Planner
                        │
                  HUMAN APPROVAL
                        │
                     Codex E
                   Implementer
                        │
               pytest / ruff / mypy
                        │
                     Codex F
                     Reviewer
                        │
                     Codex G
                       Fix
```

Each role should run with a clean task context.

---

# 5. Human-Controlled Workflow

The system assists the engineer.

It does not replace engineering judgment.

Two important human gates exist.

## Gate 1 — Plan approval

```text
Investigation
      ↓
Plan
      ↓
🛑 HUMAN APPROVAL
      ↓
Implementation
```

The system must not modify repository code before this approval.

## Gate 2 — Final review

```text
Implementation
      ↓
Validation
      ↓
Independent Review
      ↓
Fix
      ↓
Final Validation
      ↓
🛑 HUMAN REVIEW
```

V1 stops at:

```text
READY_FOR_HUMAN_REVIEW
```

It does not merge or deploy anything.

---

# 6. Understand Before Modifying

Receiving a ticket must never immediately trigger code modification.

The repository must first be investigated.

Investigation should determine:

1. relevant entry points
2. call/request flow
3. existing implementations
4. existing tests
5. database interactions
6. external service interactions
7. configuration
8. error handling
9. important failure scenarios

Only then can an implementation plan be generated.

---

# 7. Evidence Over Speculation

Agent conclusions should contain repository evidence.

Bad:

```text
Use RetryService.
```

Better:

```text
Decision:
Reuse RetryService.

Evidence:
- src/payments/client.py:81
- src/invoices/client.py:104

Reason:
Both clients use the existing abstraction for transient
upstream HTTP failures.
```

Agents must distinguish between:

```text
Observed fact
Assumption
Recommendation
Unresolved question
```

Missing requirements must not silently become invented requirements.

---

# 8. Artifact-Based Communication

Agents should not maintain long conversations with each other.

Each workflow stage produces an artifact.

```text
Explorer
   ↓
exploration.json

Pattern Researcher
   ↓
patterns.json

Test Researcher
   ↓
tests.json

Planner
   ↓
plan.md

Implementer
   ↓
implementation.json

Validator
   ↓
validation.json

Reviewer
   ↓
review.json

Final Validator
   ↓
final-validation.json
```

This makes executions:

* inspectable
* resumable
* reproducible
* debuggable

---

# 9. Deterministic Tasks Are Not AI Tasks

Never ask an AI:

> Did the tests pass?

Run them.

The orchestrator owns:

```text
git
git worktree
git diff
git status

pytest
ruff
mypy

exit codes
timeouts
changed files
process output
```

AI agents own:

```text
repository understanding
reasoning
planning
implementation
code review
fix reasoning
```

Rule:

> If software can determine the answer reliably, do not ask an LLM.

---

# 10. V1 Workflow

V1 intentionally supports a narrow workflow.

Input:

```text
ticket.md
```

Pipeline:

```text
ticket.md
    ↓
parallel investigation
    ↓
evidence artifacts
    ↓
planning
    ↓
plan.md
    ↓
🛑 human approval
    ↓
git worktree
    ↓
implementation
    ↓
deterministic validation
    ↓
independent review
    ↓
fix verified findings
    ↓
deterministic validation
    ↓
READY_FOR_HUMAN_REVIEW
```

---

# 11. Investigation

Investigation is parallel because these tasks are independent and read-only.

```text
                     Ticket
                        │
          ┌─────────────┼─────────────┐
          │             │             │
          ▼             ▼             ▼
       Explorer      Patterns       Tests
          │             │             │
          ▼             ▼             ▼
 exploration.json  patterns.json  tests.json
          │             │             │
          └─────────────┼─────────────┘
                        ▼
                      Planner
                        │
                        ▼
                     plan.md
```

Use `asyncio` for concurrency.

Investigation agents must operate in read-only mode.

---

# 12. Explorer Role

The Explorer finds the implementation path related to the ticket.

Responsibilities:

* locate entry points
* trace call chains
* locate service/repository/client boundaries
* identify database reads/writes
* identify external calls
* identify configuration
* identify existing error handling
* identify important files
* identify likely risks

Example output:

```json
{
  "entry_points": [
    "src/appointments/api.py:42"
  ],
  "call_chain": [
    "AppointmentRouter.create",
    "AppointmentService.create",
    "AppointmentClient.create"
  ],
  "database_writes": [],
  "external_calls": [
    "AppointmentClient.create"
  ],
  "important_files": [
    "src/appointments/api.py",
    "src/appointments/service.py",
    "src/appointments/client.py"
  ],
  "risks": [
    "The upstream operation may succeed before the client receives a timeout."
  ]
}
```

---

# 13. Pattern Researcher Role

Search the repository for existing solutions to similar problems.

Examples:

```text
retry
transactions
idempotency
background jobs
HTTP clients
exception handling
validation
logging
database access
API responses
```

Output must contain evidence.

Example:

```json
{
  "patterns": [
    {
      "name": "HTTP retry",
      "files": [
        "src/payments/client.py:81",
        "src/invoices/client.py:104"
      ],
      "description": "Both clients use RetryPolicy for transient upstream failures."
    }
  ]
}
```

---

# 14. Test Researcher Role

Responsibilities:

* find related tests
* identify fixtures
* identify mocking conventions
* identify current behavior coverage
* identify missing regression coverage
* propose tests for the ticket

Example:

```json
{
  "existing_tests": [
    "tests/appointments/test_service.py"
  ],
  "fixtures": [
    "appointment_client",
    "appointment_factory"
  ],
  "recommended_tests": [
    "successful request",
    "transient 503",
    "timeout",
    "retry exhaustion"
  ]
}
```

---

# 15. Planner Role

Inputs:

```text
ticket.md
exploration.json
patterns.json
tests.json
```

Output:

```text
plan.md
```

The plan should contain:

## Summary

What the ticket requires.

## Current Behavior

How the repository behaves today.

## Proposed Change

The smallest reasonable implementation.

## Expected Files

Example:

```text
src/appointments/service.py
tests/appointments/test_service.py
```

## Existing Patterns

Patterns being reused, including repository evidence.

## Risks

Consider when applicable:

```text
concurrency
idempotency
transaction boundaries
external side effects
backward compatibility
database migrations
security
```

## Test Plan

Specific tests that should be added or changed.

## Assumptions

Explicit assumptions.

## Unresolved Questions

Anything requiring developer judgment.

---

# 16. Human Plan Gate

Execution pauses after plan generation.

Example terminal UI:

```text
────────────────────────────────────────

VEG-123 Implementation Plan

Files

  src/appointments/service.py
  tests/appointments/test_service.py

Approach

  Reuse existing RetryPolicy around the external API request.

Risk

  A timeout after successful upstream creation may produce
  duplicate appointments.

Tests

  + transient 503
  + timeout
  + retry exhausted

Evidence

  src/payments/client.py:81
  src/invoices/client.py:104

────────────────────────────────────────

[A] Approve
[E] Edit plan
[Q] Quit
```

No implementation occurs without approval.

---

# 17. Git Worktree Isolation

Agents must never modify the developer's current working tree.

After plan approval:

```text
project/
worktrees/
    VEG-123/
```

Example branch:

```text
sen/VEG-123
```

All implementation and validation happens inside the worktree.

Configuration:

```yaml
git:
  branch_pattern: "sen/{ticket}"
  worktree_directory: "../worktrees/{ticket}"
```

Worktree creation must be deterministic and safe.

---

# 18. Implementer Role

Inputs:

```text
ticket
approved plan
repository instructions
```

The implementer must:

1. inspect relevant files
2. follow the approved plan
3. reuse existing patterns
4. make the smallest correct change
5. avoid unrelated refactoring
6. add/update tests
7. report assumptions
8. summarize changed files

The implementer must not claim that validation succeeded.

The orchestrator performs validation.

---

# 19. Deterministic Validation

Commands are repository configuration.

Example:

```yaml
commands:
  test: "pytest"
  lint: "ruff check ."
  typecheck: "mypy src"
```

For every command record:

```text
command
cwd
start time
duration
exit code
stdout
stderr
```

Example UI:

```text
Validation

✓ pytest       214 passed
✓ ruff
✓ mypy
```

Failure output may be passed to the fixer.

Automatic retries must be bounded.

---

# 20. Independent Reviewer Role

The reviewer must use a fresh agent context.

It may use the same provider as the implementer.

For example:

```text
Implementer = Codex Session A
Reviewer    = Codex Session B
```

Inputs:

```text
original ticket
approved plan
git diff
validation results
```

Review priorities:

```text
requirement correctness
missing requirements
regressions
edge cases
concurrency
idempotency
transaction boundaries
error handling
backward compatibility
security
test coverage
```

Avoid cosmetic comments unless they materially affect maintainability.

---

# 21. Structured Review Findings

Review output should be machine-readable.

Example:

```json
{
  "findings": [
    {
      "severity": "high",
      "category": "idempotency",
      "file": "src/appointments/service.py",
      "line": 182,
      "scenario": "The upstream request succeeds but the client times out.",
      "impact": "Retrying could create a duplicate appointment.",
      "recommendation": "Verify whether the upstream API supports an idempotency key."
    }
  ]
}
```

Allowed severity:

```text
critical
high
medium
low
```

Severity is advisory.

The orchestrator must not automatically merge or approve code based on model-generated severity.

---

# 22. Fixer Role

The fixer receives review findings.

For each finding:

```text
1. Verify it against the actual code.
2. Decide whether the finding is valid.
3. If valid, make the smallest safe fix.
4. If invalid, explain why with evidence.
5. Avoid unrelated changes.
```

Then deterministic validation runs again.

Loops must be bounded.

Default:

```yaml
limits:
  max_fix_cycles: 2
```

After the limit, require human intervention.

---

# 23. Workflow State Machine

The workflow must be resumable.

```text
NEW
 │
 ▼
INVESTIGATING
 │
 ▼
PLAN_READY
 │
 ▼
AWAITING_PLAN_APPROVAL
 │
 ▼
CREATING_WORKTREE
 │
 ▼
IMPLEMENTING
 │
 ▼
VALIDATING
 │
 ▼
REVIEWING
 │
 ├──────── findings ────────┐
 │                          │
 ▼                          ▼
FINAL_VALIDATION          FIXING
 │                          │
 │                          └──→ VALIDATING
 ▼
READY_FOR_HUMAN_REVIEW
 │
 ▼
DONE
```

Additional states:

```text
FAILED
WAITING_FOR_HUMAN
```

Do not implement the workflow as one giant function.

Steps must be individually resumable.

---

# 24. Persistent State

Use SQLite.

Do not use:

```text
PostgreSQL
Redis
Kafka
external database services
```

Suggested run fields:

```text
run_id
ticket_id
repository
state
created_at
updated_at
current_step
worktree_path
branch
```

Suggested step fields:

```text
step
role
provider
started_at
finished_at
status
artifact_path
command
exit_code
```

Large outputs belong in artifact files, not SQLite.

---

# 25. Task Artifacts

Each ticket receives an artifact directory.

Example:

```text
.dev-agent/
└── runs/
    └── VEG-123/
        ├── ticket.md
        ├── exploration.json
        ├── patterns.json
        ├── tests.json
        ├── plan.md
        ├── implementation.json
        ├── validation.json
        ├── review.json
        └── final-validation.json
```

This provides an audit/debug trail for every run.

---

# 26. Configuration

Target repositories use:

```text
.dev-agent.yaml
```

Example Codex-only configuration:

```yaml
project: veggies-api

providers:
  codex:
    type: codex_cli

roles:
  explorer:
    provider: codex

  pattern_researcher:
    provider: codex

  test_researcher:
    provider: codex

  planner:
    provider: codex

  implementer:
    provider: codex

  reviewer:
    provider: codex

  fixer:
    provider: codex

commands:
  test: "pytest"
  lint: "ruff check ."
  typecheck: "mypy src"

git:
  branch_pattern: "sen/{ticket}"
  worktree_directory: "../worktrees/{ticket}"

gates:
  approve_plan: true
  approve_final: true

limits:
  max_fix_cycles: 2
```

Provider credentials must never be stored here.

---

# 27. Repository Instructions

Agents should respect existing repository instructions.

Common files:

```text
AGENTS.md
CLAUDE.md
```

`dev-agent` does not replace them.

They contain repository knowledge such as:

```text
architecture conventions
testing conventions
database migration rules
API conventions
commands
coding standards
directory-specific instructions
```

---

# 28. CLI

Use Typer.

Initial commands:

```bash
dev-agent --help

dev-agent doctor

dev-agent start VEG-123 --file ticket.md

dev-agent status VEG-123

dev-agent resume VEG-123
```

Later a shorter alias may exist:

```bash
da ticket VEG-123
```

Do not optimize CLI ergonomics prematurely.

---

# 29. Doctor Command

`doctor` validates the current environment.

Example:

```text
$ dev-agent doctor

Environment
────────────────────────

✓ Python 3.12.7
✓ Git
✓ Git repository
✓ .dev-agent.yaml

Providers
────────────────────────

✓ codex     available

Roles
────────────────────────

✓ explorer             → codex
✓ pattern_researcher   → codex
✓ test_researcher      → codex
✓ planner              → codex
✓ implementer          → codex
✓ reviewer             → codex
✓ fixer                → codex

Ready.
```

Optional providers should not fail `doctor`.

Example:

```text
✓ codex     available
○ claude    not installed
```

is valid if no configured role requires Claude.

If configuration says:

```yaml
reviewer:
  provider: claude
```

and Claude is unavailable:

```text
✗ reviewer → claude

Configured provider "claude" is unavailable.

Change the role provider or configure Claude.
```

---

# 30. Technology Stack

Use:

```text
Python 3.12+
Typer
Pydantic
PyYAML
Rich
asyncio
SQLite
pytest
ruff
mypy
```

Use the standard library whenever practical.

Do not introduce an agent framework.

---

# 31. Project Structure

Start with:

```text
dev-agent/
├── pyproject.toml
├── README.md
│
├── dev_agent/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── state.py
│   ├── orchestrator.py
│   │
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   └── codex_cli.py
│   │
│   ├── workflow/
│   │   ├── __init__.py
│   │   ├── engine.py
│   │   ├── ticket.py
│   │   └── steps.py
│   │
│   ├── git/
│   │   ├── __init__.py
│   │   └── worktree.py
│   │
│   ├── validation/
│   │   ├── __init__.py
│   │   └── runner.py
│   │
│   └── models/
│       ├── __init__.py
│       ├── agent.py
│       ├── investigation.py
│       ├── plan.py
│       ├── finding.py
│       └── run.py
│
├── prompts/
│   ├── explore.md
│   ├── patterns.md
│   ├── tests.md
│   ├── synthesize.md
│   ├── implement.md
│   ├── review.md
│   └── fix.md
│
└── tests/
    ├── unit/
    └── integration/
```

Do not create empty abstractions simply because this structure lists them.

Add modules as milestones require them.

---

# 32. Process Runner

Provider execution and validation require a reliable process runner.

It should support:

```text
command
arguments
cwd
timeout
stdin
stdout capture
stderr capture
exit code
duration
```

Prefer `asyncio.create_subprocess_exec`.

Avoid `shell=True` unless there is a concrete requirement.

The process runner should be reusable by:

```text
CodexCLIProvider
Git operations
ValidationRunner
doctor
```

---

# 33. Provider Interface

Suggested models:

```python
class AgentTask(BaseModel):
    role: str
    prompt: str
    working_directory: Path
    read_only: bool = True


class AgentResult(BaseModel):
    success: bool
    output: str
    exit_code: int
    duration_seconds: float
```

Conceptual interface:

```python
class AgentProvider(ABC):
    @abstractmethod
    async def execute(
        self,
        task: AgentTask,
    ) -> AgentResult: ...
```

Provider-specific command construction belongs only inside the provider implementation.

The workflow must never contain code like:

```python
if provider == "codex":
    ...
elif provider == "claude":
    ...
```

Use provider resolution/registration instead.

---

# 34. Testing

Unit tests must not require a real Codex subscription or network access.

Use:

```python
class FakeProvider(AgentProvider): ...
```

The fake provider should return deterministic responses.

This allows testing:

```text
workflow transitions
artifact persistence
invalid output
validation failure
review findings
fix loops
resume behavior
```

Real Codex execution belongs in optional integration tests.

CI must not require an AI provider.

---

# 35. Security

This tool may run against private company repositories.

Rules:

1. Never log credentials.
2. Never put API keys into artifacts.
3. Never automatically read `.env`.
4. Never dump environment variables into prompts.
5. Respect repository ignore rules.
6. Use provider authentication already configured by the developer.
7. Avoid reading secrets unless explicitly necessary.
8. Never force-push automatically.
9. Never automatically merge.
10. Never deploy.
11. Avoid destructive Git operations.

Commands such as:

```text
git reset --hard
git clean -fd
git push --force
```

must never run automatically.

---

# 36. Failure Handling

Expected failures include:

```text
provider unavailable
provider timeout
invalid provider output
invalid JSON
tests failing
lint failing
worktree already exists
branch already exists
process interrupted
developer quits
```

A failure must not corrupt workflow state.

The user should be able to run:

```bash
dev-agent resume VEG-123
```

and continue from the last safe point.

---

# 37. Observability

The current workflow state should always be obvious.

Example:

```text
VEG-123

Ticket             ✓
Investigation      ✓
Plan               ✓
Plan approval      ✓
Worktree           ✓
Implementation     ✓
Tests              ✓
Review             →
Fix                -
Final validation   -
```

When a step fails, expose enough process/provider output to diagnose it.

---

# 38. Non-Goals for V1

Do NOT implement:

```text
Claude integration
Jira API integration
GitHub/GitLab PR creation
web UI
Slack integration
automatic merging
deployment
production access
vector database
RAG infrastructure
LangGraph
CrewAI
Redis
Kafka
distributed workers
cloud execution
automatic ticket selection
unlimited agent loops
```

Do not turn V1 into an autonomous software engineer.

The goal is a reliable local engineering workflow.

---

# 39. V1 Success Criteria

The following command should eventually work:

```bash
dev-agent start VEG-123 --file ticket.md
```

The system should:

1. read the ticket
2. launch three independent read-only Codex investigation sessions
3. collect structured evidence
4. launch a fresh Codex planner
5. create `plan.md`
6. stop for human approval
7. create an isolated Git worktree
8. launch a fresh Codex implementation session
9. run configured validation commands
10. generate the Git diff
11. launch a fresh Codex reviewer
12. collect structured review findings
13. launch a fresh Codex fixer when needed
14. re-run deterministic validation
15. stop in `READY_FOR_HUMAN_REVIEW`

At no point should Claude be required.

---

# 40. Implementation Roadmap

Do not build the entire system at once.

Complete one milestone, validate it, then continue.

---

## Milestone 1 — Project Skeleton

Create:

```text
pyproject.toml
dev_agent/__init__.py
dev_agent/cli.py
dev_agent/config.py
dev_agent/models/
tests/
```

Install/configure:

```text
Typer
Pydantic
PyYAML
Rich
pytest
ruff
mypy
```

Implement:

```bash
dev-agent --help
dev-agent doctor
```

`doctor` initially checks:

```text
Python >= 3.12
Git executable
current directory is a Git repository
.dev-agent.yaml exists
configuration parses successfully
configured providers exist
configured roles resolve to providers
Codex CLI exists if configured
```

Do NOT implement ticket execution.

After Milestone 1:

```text
run pytest
run ruff
run mypy
summarize implementation
STOP
```

Wait for human review.

---

## Milestone 2 — Process Runner

Build a reusable asynchronous process runner.

Requirements:

```text
cwd
arguments
stdin
timeout
stdout
stderr
exit code
duration
```

Test:

```text
success
non-zero exit
timeout
missing executable
working directory
stdout/stderr
```

Do not implement workflow orchestration yet.

---

## Milestone 3 — Provider Abstraction

Implement:

```text
AgentProvider
AgentTask
AgentResult
CodexCLIProvider
FakeProvider
```

Test provider resolution.

Do not invoke real Codex in unit tests.

Add one optional integration test for Codex CLI.

---

## Milestone 4 — Structured Artifacts

Create Pydantic models for:

```text
Exploration
PatternAnalysis
TestAnalysis
Review
Finding
```

Implement JSON parsing and validation.

Agent output that violates the expected schema must fail clearly.

---

## Milestone 5 — Parallel Investigation

Implement:

```text
Explorer
Pattern Researcher
Test Researcher
```

Run them concurrently.

Each receives:

```text
ticket
repository path
role-specific prompt
```

All must be read-only.

Persist:

```text
exploration.json
patterns.json
tests.json
```

---

## Milestone 6 — Planning

Launch a fresh planner agent.

Inputs:

```text
ticket
exploration
patterns
tests
```

Generate:

```text
plan.md
```

Display it using Rich.

Implement:

```text
Approve
Edit
Quit
```

No repository modification before approval.

---

## Milestone 7 — Worktree

Implement safe Git worktree creation.

Use temporary Git repositories for tests.

Support:

```text
configurable branch naming
configurable worktree location
existing branch detection
existing worktree detection
```

Never alter the developer's current working tree.

---

## Milestone 8 — Implementation

Launch a fresh implementer agent inside the worktree.

Inputs:

```text
ticket
approved plan
repository instructions
```

Persist implementation metadata.

---

## Milestone 9 — Validation

Implement configured commands.

Example:

```yaml
commands:
  test: pytest
  lint: ruff check .
  typecheck: mypy src
```

Record results.

Stop cleanly on failure.

---

## Milestone 10 — Independent Review

Generate the actual Git diff.

Launch a fresh reviewer context.

Inputs:

```text
ticket
approved plan
diff
validation
```

Parse structured findings.

---

## Milestone 11 — Fix Cycle

Launch a fresh fixer.

Require it to verify findings before modifying code.

Then run validation again.

Enforce:

```text
max_fix_cycles
```

Never create an infinite loop.

---

## Milestone 12 — Persistence and Resume

Add SQLite persistence.

Implement:

```bash
dev-agent status VEG-123
dev-agent resume VEG-123
```

Test interruption/recovery.

---

# 41. First Task for the Coding Agent

**Do not attempt to implement the whole README.**

Implement **Milestone 1 only**.

Before writing code:

1. inspect this README
2. propose the exact files to create
3. briefly explain the dependency choices
4. identify any assumptions

Then implement Milestone 1.

Requirements:

```text
Python >= 3.12
Typer CLI
Pydantic configuration
PyYAML
Rich
pytest
ruff
mypy
```

Create a minimal example:

```text
.dev-agent.example.yaml
```

using Codex only.

Implement:

```bash
dev-agent --help
dev-agent doctor
```

`doctor` should produce approximately:

```text
dev-agent doctor

Environment
────────────────────────

✓ Python 3.12+
✓ Git
✓ Git repository
✓ Configuration

Providers
────────────────────────

✓ codex     available

Roles
────────────────────────

✓ explorer             → codex
✓ pattern_researcher   → codex
✓ test_researcher      → codex
✓ planner              → codex
✓ implementer          → codex
✓ reviewer             → codex
✓ fixer                → codex

Ready.
```

If Codex is unavailable:

```text
✗ codex unavailable

Required because these roles use it:

  explorer
  pattern_researcher
  test_researcher
  planner
  implementer
  reviewer
  fixer
```

After implementation:

```bash
pytest
ruff check .
mypy dev_agent
```

Fix failures.

Then report:

```text
files created
architecture implemented
tests added
commands executed
remaining assumptions
```

Then **STOP**.

Do not start Milestone 2 until explicitly asked.

---

# 42. Engineering Rules for dev-agent

While building this project:

1. Prefer simple code.
2. Prefer composition over inheritance-heavy designs.
3. Do not introduce abstractions without a current use.
4. Keep provider-specific logic isolated.
5. Keep subprocess execution isolated.
6. Keep Git operations isolated.
7. Use Pydantic at external/data boundaries.
8. Make workflow transitions explicit.
9. Make side effects testable.
10. Do not swallow exceptions.
11. Avoid global mutable state.
12. Use dependency injection where it materially improves testing.
13. Avoid unnecessary dependencies.
14. Add tests for important behavior.
15. Do not implement future milestones early.
16. Never use an LLM for deterministic checks.
17. Never modify unrelated code.
18. Prefer the smallest correct implementation.

---

# 43. Long-Term Direction

Eventually:

```bash
da ticket VEG-123
```

should feel like:

```text
◆ VEG-123

Investigating repository...

  ✓ code path
  ✓ existing patterns
  ✓ tests

Implementation plan ready.

────────────────────────────────

Files

  src/appointments/service.py
  tests/appointments/test_service.py

Approach

  Reuse existing RetryPolicy.

Main Risk

  Timeout after successful upstream creation
  may cause duplicate requests.

Evidence

  src/payments/client.py:81
  src/invoices/client.py:104

────────────────────────────────

[A] Approve
[E] Edit
[Q] Quit
```

After approval:

```text
Creating isolated worktree...
✓

Implementing...
✓

Validation
✓ pytest
✓ ruff
✓ mypy

Independent review
✓

HIGH      0
MEDIUM    1
LOW       1

Verifying findings...
✓

Fixing valid findings...
✓

Final validation
✓

VEG-123 is ready for human review.

Worktree:
../worktrees/VEG-123

Branch:
sen/VEG-123
```

The developer inspects the final diff and decides whether to create the PR.

---

# 44. What This Project Is Really Building

The important part of `dev-agent` is not Codex.

It is not Claude.

It is not any particular model.

The important part is the engineering protocol:

```text
UNDERSTAND
    ↓
COLLECT EVIDENCE
    ↓
PLAN
    ↓
HUMAN DECISION
    ↓
IMPLEMENT
    ↓
VERIFY
    ↓
INDEPENDENTLY REVIEW
    ↓
VERIFY AGAIN
    ↓
HUMAN DECISION
```

Providers are replaceable.

The workflow is the durable asset.

## Local models with Ollama

Ollama can run on the same machine or a LAN server. The dev-agent CLI, Git
worktrees, file tools and validation commands run on your development machine;
only model inference runs on the Ollama server.

See [the complete Ollama example](examples/ollama.yaml). Merge its `providers`
and `roles` sections into your project's `.dev-agent.yaml`; keep your project's
existing validation commands, Git settings and gates. Configure before starting a
**new ticket**: active runs keep a frozen configuration snapshot.

```yaml
providers:
  codex:
    type: codex_cli
  local:
    type: ollama
    base_url: http://10.0.0.28:11434
    model: qwen2.5-coder:7b-instruct
    timeout_seconds: 600
    num_ctx: 8192
    max_output_tokens: 2048
    max_steps: 24
roles:
  explorer: {provider: local}
  pattern_researcher: {provider: local}
  test_researcher: {provider: local}
  planner: {provider: codex}
  implementer: {provider: local}
  reviewer: {provider: codex}
  fixer: {provider: local}
```

This example mixes local execution with Codex planning/review. You can route any
of the seven roles independently; route all seven to `local` to avoid requiring
Codex. No automatic fallback to a paid model occurs. A small model's ability to
finish a complete ticket still depends on the task and its structured outputs.

Run `dev-agent doctor` to check server connectivity and installed model name.
Then use the existing `start`, plan review, `resume`, `revise` and `merge`
workflow. Commit the configuration/ticket as part of the source baseline before
creating the worktree, just as with the Codex provider. Existing progress indicators
continue to show activity while local inference runs.

The Ollama adapter uses schema-constrained JSON actions: list files, read UTF-8
files, write complete files, delete files, and finish. Read-only roles cannot use
write/delete actions. It supplies root AGENTS.md and CLAUDE.md automatically and
instructs the model to read applicable nested instructions. It cannot run shell,
network or database commands; configured validation commands run separately in
the existing workflow.

File tools reject paths outside the worktree, symlinks, hardlinks, Git internals,
common credential paths and ignored write targets. They are application-level
guards, not an OS sandbox or a guarantee that every sensitive project file is
recognized. Use a trusted inference endpoint: prompts and files read by the model
are sent to that endpoint. HTTP redirects and environment proxy settings are
disabled.

Current limits: 48 KB per text file, bounded steps and total execution timeout,
and a conservative character-based conversation-size guard (not an exact token
count). Oversized context, truncated/malformed output or an exhausted step budget
fails explicitly. Increase context only when the server has sufficient memory.
Cancellation closes the HTTP client; the server may take time to release an
already queued generation. `doctor` checks availability, not generation quality
or GPU placement.

## Validation repair and recovery

Managed workflows now hand a completed command failure to the configured
`fixer`, using the approved plan, original ticket, actual diff, validation log
and unusually destructive change warnings. The fixer runs in the existing
worktree. Each attempt is persisted under `fixes/` and shares the existing
`limits.max_fix_cycles` budget with review-driven fixes. Successful repair is
followed by validation, independent review, and final validation. Exhausting the
budget leaves the run waiting for human inspection; it never merges automatically.

For a run stopped by the older CLI at its first failed validation:

```bash
dev-agent resume TICKET-ID
```

Recovery requires the original configuration and approved source baseline,
unchanged worktree contents, matching archived validation records, and a completed
nonzero command exit. It does not rerun investigation or implementation. Timeouts,
interrupted commands, launch failures, stale snapshots and uncertain fixer
attempts are not automatically replayed. Explicit Docker-daemon-unavailable errors
stop before spending another fix attempt. After restoring the environment,
`resume` revalidates the same unchanged worktree and continues review if it passes;
the consumed fix budget is preserved. This applies only to a completed managed
validation-repair cycle with a matching archived failure, not an interrupted fixer.
Other nonzero environmental failures may still reach the fixer; it must defer
problems it cannot safely fix.

`start` now checks for a clean committed source baseline before launching any
agents or creating the ticket record. Resolve intended changes and ignore generated
files first. Source cleanliness is checked again at later workflow boundaries.

The Ollama file adapter requires reading an existing file before writing/deleting
it and rejects edits if that file changed after the read. It refuses full-file
writes that reduce files of at least 30 lines to less than half their line count.
This intentionally also rejects some legitimate large rewrites; use a suitable
provider for those tasks. Other large tracked-file deletions are advisory warnings
sent to the fixer/reviewer, not an automatic determination of correctness.

Progress for validation repair shows the failed command and a compact error
summary. Full validation logs remain in the run artifacts. Investigation now
records provider-provided error details when available; it still does not persist
raw provider stdout/stderr.

## Claude Code provider

Configure `type: claude_cli` to route any role through a fresh Claude Code CLI
session. See [the mixed Claude/Codex/Ollama example](examples/claude.yaml).
Install a current Claude Code CLI on the machine running dev-agent and sign in:

```bash
claude --version
claude auth login
claude auth status
```

The adapter uses the CLI's existing authentication; dev-agent does not store keys.
Your account must have access to Claude Code and the selected model.
`model` is optional; `max_steps` maps to the CLI's maximum agentic turns and
`timeout_seconds` bounds the whole subprocess. Provider roles remain independent,
so Claude can plan/review/fix while Ollama implements.

The adapter uses non-interactive JSON output, fresh sessions without persistence,
restricted mode and safe mode. It requires a current CLI supporting those flags
(`--restricted` was introduced in 2.1.248). Read-only tasks expose Read/Glob/Grep;
editing tasks additionally expose Edit/Write scoped to the working directory.
No Bash, subagent or external MCP tools are provided. Customizations are disabled,
hooks are disabled via invocation settings, and protected file paths are denied.
Repository instructions must be read explicitly through file tools because safe
mode disables automatic CLAUDE.md loading. This relies on Claude Code's permission
enforcement, not an independent OS sandbox; enterprise managed policies still apply.

Only a successful final result envelope is accepted. Malformed output, denied
tool calls, exhausted turns, nonzero exits and timeouts fail the role instead of
being treated as completed work. There is no automatic fallback to another provider.
Tasks requiring JSON (investigation, plan review, code review and fix reports)
pass their schema through Claude Code’s --json-schema option and consume
structured_output, rather than treating free-form prose as JSON. The existing
dev-agent artifact schemas still validate the returned answer.

`dev-agent doctor` checks that `claude` is on PATH; it does not verify account or
model entitlement. Configure and commit before a new ticket; running tickets keep
their original configuration. To test real authenticated read/edit calls in a
temporary repository after setup:

```bash
DEV_AGENT_RUN_CLAUDE_TEST=1 .venv/bin/pytest tests/integration/test_claude_cli.py -q
```

This opt-in test consumes account usage. Normal tests use a fake subprocess and
do not require Claude authentication.

References: [CLI flags](https://code.claude.com/docs/en/cli-reference) and
[programmatic execution](https://code.claude.com/docs/en/headless).

## Ollama final-report schemas

Tasks with `output_schema` now have two phases. The bounded tool loop gathers
evidence and performs authorized edits. Once it returns `finish`, file-tool
execution ends permanently for that provider call. A separate request passes the
actual report schema directly to Ollama's `format` field, retaining the task,
instructions and recorded tool evidence. The response is validated locally with
JSON Schema, including required fields, types, nested definitions and extra-field
rules; downstream artifact validation still applies.

The report phase makes at most two requests (one initial generation and one
formatting retry). Invalid JSON, schema failures and truncated reports can trigger
that single retry; network errors do not. The retry carries the validation error
and regenerates the report from the same evidence. It cannot execute returned
tool actions or repeat edits. Both phases share the existing total timeout and
context-size guard. The report phase adds up to two inference calls beyond
`max_steps`; no extra configuration is required. Non-schema tasks retain their
existing text/Markdown finish behavior.

Schema validity does not prove that the report is true or that code is correct.
No retries fabricate missing fields locally, weaken the schema, reset fix budgets,
or rerun the implementation.

To exercise real local-model editing followed by a structured FixReport:

```bash
DEV_AGENT_OLLAMA_URL=http://10.0.0.28:11434 \
  .venv/bin/pytest tests/integration/test_ollama_live.py -q
```

The optional `DEV_AGENT_OLLAMA_MODEL` overrides the default
`qwen2.5-coder:7b-instruct`. Tests edit only a temporary repository.
