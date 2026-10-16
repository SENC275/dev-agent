# dev-agent

Turn a ticket into a reviewed code change, from your terminal.

**dev-agent coordinates Codex, Claude Code and Ollama** through a repeatable workflow.
Assign different providers to research, planning, implementation and review; inspect
what happened and decide when to merge.

```text
Describe → Draft ticket → Investigate → Plan → Approve → Implement → Test → Review
                                                            ↑               │
                                                            └── Fix/retest ─┘
                                                    Human review → Merge
```

Implementation happens in an isolated Git worktree. Configuration, tickets and run
records stay local by default. Generating a ticket does not start implementation;
merging does not push or deploy.

## Install

You need macOS/Linux, Python 3.12+, Git, and `uv` or `pipx`. Install and authenticate
the provider CLIs you intend to use, or make an Ollama server available.

```bash
git clone https://github.com/SENC275/dev-agent.git
cd dev-agent
uv tool install .
# Alternative: pipx install .
```

## Your first ticket

Run the following commands from the **project you want to change**, not the dev-agent
source repository. The example requirement assumes a Python task API with `GET /tasks`;
replace it with a small, testable change relevant to your own project.

### 1. Configure providers and checks

```bash
cd /path/to/your-project
dev-agent init
```

Edit `.dev-agent.yaml`. This example uses Codex for research, planning and review,
and Claude for implementation and fixes:

```yaml
providers:
  codex:
    type: codex_cli
    timeout_seconds: 900
  claude:
    type: claude_cli
    timeout_seconds: 900

roles:
  explorer: {provider: codex}
  pattern_researcher: {provider: codex}
  test_researcher: {provider: codex}
  planner: {provider: codex}
  implementer: {provider: claude}
  reviewer: {provider: codex}
  fixer: {provider: claude}

commands:
  test: python3 -m pytest
  lint: "true"
  typecheck: "true"

gates:
  plan_review: agent
  approve_final: true
```

This example requires both CLIs on `PATH`. To use only Claude, set all seven role
providers to `claude`. Providers use their CLI's default model unless you add
`model:`; choose one available to your account. See the [Ollama example](examples/ollama.yaml)
for a local implementer.

Use checks that work in your project's environment. All three command values must
be nonempty strings: **use `"true"`, not `null`, to skip an unused check**. Shell
operators require an explicit shell, e.g. `sh -c 'command1 && command2'`.

```bash
dev-agent doctor
dev-agent test
```

`doctor` checks prerequisites, not account authentication or model access. `test`
runs only `commands.test` in the current directory, without a model call or ticket.
Point root `AGENTS.md` or `CLAUDE.md` to the conventions and documentation agents
should read.

### 2. Generate and review a draft

```bash
dev-agent ticket TASK-001 --description \
  "Add optional done filtering to GET /tasks. Filter before pagination, preserve existing behavior when omitted, and add regression tests."

cat .dev-agent/tickets/TASK-001.md
```

Edit the draft's acceptance criteria and resolve open questions before starting.
You can also supply an external description with `--file ~/requirements/task.md`,
or write your own ticket Markdown without using a model to draft it.

### 3. Start implementation

Commit intended **project code** changes first and ensure `git status --short` is
empty. An existing commit is required as the worktree baseline.

```bash
dev-agent start TASK-001 --file .dev-agent/tickets/TASK-001.md
```

The example config uses an independent agent to approve the plan. The default from
`init` is human approval: inspect/edit the plan in the terminal approval flow before
implementation begins. Progress and elapsed time remain visible during agent work.

**Keep `.dev-agent.yaml` unchanged for this ticket.** Its configuration is frozen
at start. Tickets, YAML and run records do not need commits: `init` excludes
`.dev-agent.yaml` and `.dev-agent/` through Git's local `info/exclude`. Files already
tracked by Git remain tracked.

### 4. Inspect, revise and merge

```bash
dev-agent summary TASK-001
dev-agent status TASK-001
```

`summary` shows validation results, review findings, changed files and token usage.
`status` provides the `worktree_path` and artifact locations. Inspect that worktree:

```bash
git -C /path/from/worktree_path status --short
git -C /path/from/worktree_path diff HEAD
```

Open new untracked files too; ordinary Git diffs omit them. If you want changes,
run from the original project root:

```bash
dev-agent revise TASK-001
# Or: dev-agent revise TASK-001 --file ~/requirements/feedback.md
```

When satisfied:

```bash
dev-agent merge TASK-001 --dry-run
dev-agent merge TASK-001
```

Merge asks for confirmation, commits the reviewed changes and fast-forwards your
source branch. It requires current passing checks and an unchanged source baseline.
You still control the final push, deployment and database migrations.

## Parallel tickets and an updated main branch

Different tickets can run in separate terminals/worktrees. If `TASK-001` merges
first, update `TASK-002` before accepting it:

```bash
dev-agent sync TASK-002
dev-agent summary TASK-002
dev-agent merge TASK-002 --dry-run
dev-agent merge TASK-002
```

Sync preserves the old worktree, creates a new integration worktree on current source
HEAD, and repeats planning approval, validation and independent review. Conflicts get
one fixer attempt; unresolved conflicts pause for manual edits and `sync TASK-002 --continue`.
Requires Git 2.38+. There is no automatic multi-ticket scheduler/merge queue yet.
See [parallel integration and recovery](docs/synchronization.md).

## Generate a batch of tickets

Put each requirement in its own folder. **The subfolder name becomes the ticket ID.**

```text
~/requirements/
├── TF-003/
│   └── description.md
└── TF-004/
    ├── description.md
    └── constraints.md
```

From the target repository:

```bash
dev-agent ticket batch --folder ~/requirements
```

Drafts appear in `.dev-agent/tickets/TF-003.md` and `TF-004.md`. Review scope overlap
and dependencies before starting them individually. Repeating the command skips
unchanged successes and retries failures; changed inputs or existing edits produce
conflicts instead of being overwritten. [Input rules and recovery →](docs/ticket-batches.md)

## Example: Terraform checks

For a Terraform project with native mock tests and environment tfvars, use a script
that initializes providers, checks formatting, validates and tests every environment:

```sh
# scripts/test.sh — commit this script as part of your project baseline.
#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
terraform init -backend=false -input=false
terraform fmt -check -recursive
terraform validate
for environment in dev staging prod; do
  terraform test -var-file="environments/$environment.tfvars"
done
```

Replace the `commands` section in your YAML:

```yaml
commands:
  test: sh scripts/test.sh
  lint: "true"
  typecheck: "true"
```

This assumes `tests/*.tftest.hcl` uses mock providers and `command = plan`; it is not
a guarantee that arbitrary Terraform tests avoid real infrastructure. Provider
installation may need network access. Adapt the environment list to your project.

Example requirement: “Disable retired EC2 pools in dev, staging and prod by setting
their counts to zero. Preserve resource definitions, live Fargate routing and RDS
settings. Add regression tests using the actual environment tfvars.”

## When a run stops

Start with `dev-agent summary TASK-001` and `dev-agent status TASK-001`. Check the
referenced `validation.json`, `review.json` or provider diagnostic before retrying.

| Situation | Next step |
| --- | --- |
| Provider or validation failed | Fix the cause, then `dev-agent resume TASK-001`. |
| Fix cycle limit reached | Inspect remaining findings; grant one more attempt with `dev-agent resume TASK-001 --additional-fix-cycles 1`. |
| Configuration changed since start | Restore the original YAML, or start a new ID with the same ticket file and the new configuration. |
| Checks pass but review has findings | Read `summary` / `review.json`; passing tests alone do not complete review. |
| Ready for human review | Inspect the worktree, then revise or merge. |

Resume preserves history and checks saved evidence. It does not reset fix budgets or
blindly replay interrupted writes. A manual `dev-agent test` does not update the
workflow's recorded validation. [Recovery details →](docs/resume.md)

## Optional capabilities

| Capability | Command or setting | Details |
| --- | --- | --- |
| Token and cache accounting | `dev-agent usage TASK-001` | [Usage](docs/usage.md); missing metrics remain unknown. |
| Ticket context reuse | Enabled by default; `context.investigation_mode: single_pass` is opt-in | [Context reuse](docs/context-reuse.md); default investigation stays `parallel`. |
| Cross-ticket project index | `project_context.enabled: true` | [Project context](docs/project-context.md) |
| Reviewable knowledge documents | `knowledge.enabled: true` | [Knowledge](docs/knowledge-workflow.md); opt-in documents enter code commits. |
| Local corpus candidates | `dev-agent corpus collect TASK-001`, then `dev-agent corpus inspect TASK-001` | [Corpus](docs/corpus.md); no automatic training or upload. |

## Development

From the dev-agent source repository:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy dev_agent
```

See the [full configuration example](.dev-agent.example.yaml),
[review summary guide](docs/review-summary.md) and [design history](docs/design-and-history.md).
Use `dev-agent --help` or `dev-agent <command> --help` for command options.

### 本地 Dashboard

在项目根目录运行：

```bash
dev-agent dashboard
```

自动打开本地页面，查看 ticket 状态、阶段历史、审查结果、变更文件和 token 用量。
可以运行当前项目配置的 `commands.test`；resume、revise、sync、merge 提供 CLI 命令。

多个项目放在一个页面里管理：

```bash
dev-agent dashboard --project ~/sen --project ~/terraform-corpus-demo-20261011
```

服务只监听 `127.0.0.1`。端口占用时加 `--port 8766`；不自动打开浏览器用 `--no-open`。
运行状态是日志中的最近记录，不代表对应进程仍然存活。详情按需刷新，列表每 3 秒刷新。
测试在主项目目录执行，不能替代 ticket worktree 的验证。测试输出在完成后显示，运行中显示耗时。
关闭服务后，已启动的测试会等到完成或配置的超时再退出。Dashboard 的测试记录仅保存在当前服务内存中。

### 让 agent 修改尚未批准的计划

审核菜单支持 `[A] Approve  [E] Edit  [R] Request changes  [Q] Quit`。
选 **R**，输入修改意见，例如“保留 CloudWatch Logs，补充 shadow 阶段和回滚测试”。
配置中的 planner 会复用已有调查、修订完整计划；完成后重新展示，只有选 **A** 才批准。
可多轮 R；空反馈取消本轮；Q 保存退出，之后 `dev-agent resume TICKET_ID` 回到审核。

每轮的旧计划、反馈、模型回复和状态保存在该 plan 的 `human-plan-revisions/`。
失败或无效回复不替换原计划；旧批准会失效。此操作不消耗自动审查的修订次数。
人类请求修改后，即使原本配置了 agent plan review，也需要人类明确批准。
已创建 worktree 的计划请用完成后的 `dev-agent revise`，不能从此入口改写。
