import asyncio
import json
from pathlib import Path

import pytest
from test_persistence import PipelineProvider, setup
from test_worktree import git
from test_worktree import repository as repository

from dev_agent.models.agent import AgentResult
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.merging import merge, preview_merge
from dev_agent.workflow.planning import approve_plan
from dev_agent.workflow.runner import advance
from dev_agent.workflow.synchronization import synchronize


def complete(journal, config, registry, ticket):
    state = asyncio.run(advance(journal, ticket, config, registry))
    plan = Path(journal.get(ticket)["plan_path"])
    if state == "READY_FOR_HUMAN_REVIEW":
        return plan
    assert state == "AWAITING_PLAN_APPROVAL"
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    assert asyncio.run(advance(journal, ticket, config, registry)) == "READY_FOR_HUMAN_REVIEW"
    return plan


class Integrator(PipelineProvider):
    def __init__(self, conflict=False, leave_markers=False):
        super().__init__()
        self.conflict = conflict
        self.leave_markers = leave_markers

    async def execute(self, task):
        if (
            task.role == "reviewer"
            and task.output_schema
            and "decision" in task.output_schema.get("properties", {})
        ):
            self.tasks.append(task)
            return AgentResult(
                success=True,
                output=json.dumps(
                    {"decision": "approve", "summary": "Checked new baseline", "issues": []}
                ),
                exit_code=0,
                duration_seconds=0,
            )
        if task.role == "implementer":
            self.tasks.append(task)
            if "FIRST" in task.prompt:
                name = "app.txt" if self.conflict else "first.txt"
                (task.working_directory / name).write_text("upstream\n")
            else:
                (task.working_directory / "app.txt").write_text("ticket\n")
                (task.working_directory / "empty.txt").write_text("")
                (task.working_directory / "binary.dat").write_bytes(b"\0\xffdemo")
                executable = task.working_directory / "executable.sh"
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            return AgentResult(success=True, output="done", exit_code=0, duration_seconds=0)
        if task.role == "fixer":
            self.tasks.append(task)
            if not self.leave_markers:
                (task.working_directory / "app.txt").write_text("upstream\nticket\n")
            return AgentResult(
                success=True,
                output=json.dumps({"status": "resolved", "summary": "Resolved both changes"}),
                exit_code=0,
                duration_seconds=0,
            )
        return await super().execute(task)


def two_tickets(
    repository, conflict=False, leave_markers=False, agent=False, integration_failure=False
):
    git(repository, "config", "user.name", "Sync Test")
    git(repository, "config", "user.email", "sync-test@example.invalid")
    journal, config, _, _ = setup(repository)
    if agent:
        config.gates.plan_review = "agent"
    if integration_failure:
        from test_validation import command

        config.commands.test = command(
            "from pathlib import Path; import sys; "
            "sys.exit(int(Path('first.txt').exists() and Path('empty.txt').exists()))"
        )
        config.limits.max_fix_cycles = 0
    with journal.connect() as db:
        db.execute(
            "UPDATE runs SET config=? WHERE ticket_id='DEMO-12'", (config.model_dump_json(),)
        )
    provider = Integrator(conflict, leave_markers)
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    journal.create("FIRST", "FIRST change", config.model_dump_json())
    first = complete(journal, config, registry, "FIRST")
    second = complete(journal, config, registry, "DEMO-12")
    preview = asyncio.run(preview_merge(journal, "FIRST"))
    asyncio.run(merge(journal, "FIRST", preview, "Accept first"))
    return journal, config, provider, registry, first, second


@pytest.mark.parametrize("conflict", [False, True])
def test_two_parallel_tickets_sync_and_merge(repository, conflict):
    journal, config, provider, registry, first, old = two_tickets(repository, conflict)
    original = dict(journal.get("DEMO-12"))
    old_target = Path(original["worktree_path"])
    old_status = git(old_target, "status", "--porcelain")
    old_files = {p.name: p.read_bytes() for p in old.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        asyncio.run(preview_merge(journal, "DEMO-12"))
    source_head = git(repository, "rev-parse", "HEAD")
    assert (
        asyncio.run(synchronize(journal, "DEMO-12", config, registry)) == "AWAITING_PLAN_APPROVAL"
    )
    new_plan = Path(journal.get("DEMO-12")["plan_path"])
    assert new_plan != old
    assert not (new_plan / "validation.json").exists()
    asyncio.run(approve_plan(new_plan, (new_plan / "plan.md").read_text()))
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert git(repository, "rev-parse", "HEAD") == source_head  # Sync never accepts/merges.
    new_target = Path(journal.get("DEMO-12")["worktree_path"])
    assert new_target != old_target
    assert (new_target / "empty.txt").read_bytes() == b""
    assert (new_target / "binary.dat").read_bytes() == b"\0\xffdemo"
    assert (new_target / "executable.sh").stat().st_mode & 0o111
    assert (old_target / "app.txt").read_text() == "ticket\n"
    assert git(old_target, "status", "--porcelain") == old_status
    assert {p.name: p.read_bytes() for p in old.iterdir() if p.is_file()} == old_files
    if conflict:
        assert (new_target / "app.txt").read_text() == "upstream\nticket\n"
        assert any(t.role == "fixer" for t in provider.tasks)
    else:
        assert (new_target / "first.txt").read_text() == "upstream\n"
        assert not any(t.role == "fixer" for t in provider.tasks)
    validation = json.loads((new_plan / "validation.json").read_text())
    assert validation["base_commit"] == source_head
    assert (new_plan / "review.json").exists()
    preview = asyncio.run(preview_merge(journal, "DEMO-12"))
    asyncio.run(merge(journal, "DEMO-12", preview, "Accept second after sync"))
    assert journal.get("DEMO-12")["state"] == "MERGED"
    assert (repository / "empty.txt").exists()


def test_unresolved_conflicts_preserve_both_worktrees_and_block_merge(repository):
    journal, config, _, registry, _, old = two_tickets(repository, True, True)
    asyncio.run(synchronize(journal, "DEMO-12", config, registry))
    plan = Path(journal.get("DEMO-12")["plan_path"])
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    head = git(repository, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="Unresolved conflict"):
        asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert journal.get("DEMO-12")["state"] == "WAITING_FOR_HUMAN"
    assert git(repository, "rev-parse", "HEAD") == head
    assert old.exists() and not (plan / "validation.json").exists()
    with pytest.raises(ValueError, match="--continue"):
        asyncio.run(advance(journal, "DEMO-12", config, registry))
    with pytest.raises((ValueError, FileNotFoundError)):
        asyncio.run(preview_merge(journal, "DEMO-12"))


@pytest.mark.parametrize("kind", ["dirty_source", "staged", "edited", "committed", "locked"])
def test_preflight_rejects_unsafe_state_without_model_calls(repository, kind):
    journal, config, provider, registry, _, old = two_tickets(repository)
    target = Path(journal.get("DEMO-12")["worktree_path"])
    if kind == "dirty_source":
        (repository / "unrelated").write_text("human changes")
    elif kind == "staged":
        git(target, "add", "app.txt")
    elif kind == "edited":
        (target / "app.txt").write_text("unreviewed edits")
    elif kind == "committed":
        git(target, "add", "app.txt")
        git(target, "commit", "-m", "manual commit")
    else:
        (repository / ".dev-agent/merge.lock").write_text("other merge")
    calls = len(provider.tasks)
    with pytest.raises(ValueError):
        asyncio.run(synchronize(journal, "DEMO-12", config, registry))
    assert len(provider.tasks) == calls
    assert old.exists()


def test_manual_conflict_resolution_continues_without_second_fixer(repository):
    journal, config, provider, registry, _, _ = two_tickets(repository, True, True)
    asyncio.run(synchronize(journal, "DEMO-12", config, registry))
    plan = Path(journal.get("DEMO-12")["plan_path"])
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    with pytest.raises(ValueError, match="Unresolved conflict"):
        asyncio.run(advance(journal, "DEMO-12", config, registry))
    target = Path(journal.get("DEMO-12")["worktree_path"])
    (target / "app.txt").write_text("human chose upstream + ticket\n")
    fixer_count = sum(t.role == "fixer" for t in provider.tasks)
    assert (
        asyncio.run(synchronize(journal, "DEMO-12", config, registry, continue_sync=True))
        == "READY_FOR_HUMAN_REVIEW"
    )
    assert sum(t.role == "fixer" for t in provider.tasks) == fixer_count
    assert (target / "app.txt").read_text() == "human chose upstream + ticket\n"
    assert asyncio.run(preview_merge(journal, "DEMO-12")).target == target


def test_agent_gate_and_cli_sync(repository, monkeypatch):
    from typer.testing import CliRunner

    from dev_agent.cli import app

    journal, config, provider, registry, _, old = two_tickets(repository, agent=True)
    monkeypatch.setattr("dev_agent.cli.load_config", lambda _: config)
    monkeypatch.chdir(repository)
    monkeypatch.setattr("dev_agent.cli.ProviderRegistry", lambda _: registry)
    calls = len(provider.tasks)
    result = CliRunner().invoke(app, ["sync", "DEMO-12"])
    assert result.exit_code == 0, result.output
    assert "Ready for human review" in result.output
    new_calls = provider.tasks[calls:]
    assert any(
        t.output_schema and "decision" in t.output_schema.get("properties", {}) for t in new_calls
    )
    assert any(t.role == "reviewer" and t.working_directory != repository for t in new_calls)
    assert Path(journal.get("DEMO-12")["plan_path"]) != old
    assert asyncio.run(preview_merge(journal, "DEMO-12"))


def test_source_advances_while_waiting_for_approval(repository):
    journal, config, provider, registry, _, _ = two_tickets(repository)
    asyncio.run(synchronize(journal, "DEMO-12", config, registry))
    before = git(repository, "worktree", "list", "--porcelain")
    git(repository, "commit", "--allow-empty", "-m", "Third ticket merged")
    count = len(provider.tasks)
    with pytest.raises(ValueError, match="Source changed"):
        asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert len(provider.tasks) == count
    assert git(repository, "worktree", "list", "--porcelain") != ""
    assert len(git(repository, "worktree", "list", "--porcelain").split("worktree ")) == len(
        before.split("worktree ")
    )


def test_new_validation_failures_cannot_merge(repository):
    from unittest.mock import patch

    journal, config, _, registry, _, _ = two_tickets(repository)
    asyncio.run(synchronize(journal, "DEMO-12", config, registry))
    plan = Path(journal.get("DEMO-12")["plan_path"])
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))

    async def fail(*args, **kwargs):
        raise ValueError("new baseline test failure")

    with patch("dev_agent.workflow.runner.validate", side_effect=fail):
        with pytest.raises(ValueError, match="new baseline test failure"):
            asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert not (plan / "review.json").exists()
    with pytest.raises((ValueError, FileNotFoundError)):
        asyncio.run(preview_merge(journal, "DEMO-12"))


def test_clean_git_merge_with_behavioral_failure_pauses_for_fix(repository):
    journal, config, _, registry, _, _ = two_tickets(repository, integration_failure=True)
    head = git(repository, "rev-parse", "HEAD")
    asyncio.run(synchronize(journal, "DEMO-12", config, registry))
    plan = Path(journal.get("DEMO-12")["plan_path"])
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "WAITING_FOR_HUMAN"
    assert journal.get("DEMO-12")["current_step"] == "validation_fix"
    validation = json.loads((plan / "validation.json").read_text())
    assert validation["status"] == "FAILED"
    assert not (plan / "review.json").exists()
    assert git(repository, "rev-parse", "HEAD") == head
    with pytest.raises((ValueError, FileNotFoundError)):
        asyncio.run(preview_merge(journal, "DEMO-12"))
    target = Path(journal.get("DEMO-12")["worktree_path"])
    # Manual repair to this synthetic test app, followed by normal resume/review.
    (target / "empty.txt").unlink()
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert asyncio.run(preview_merge(journal, "DEMO-12"))


@pytest.mark.parametrize(
    "answer", ["unstructured reply", '{"status":"needs_human","summary":"Requirements disagree"}']
)
def test_resolver_must_explicitly_confirm_resolution(repository, answer):
    from unittest.mock import patch

    journal, config, provider, registry, _, _ = two_tickets(repository, True)
    asyncio.run(synchronize(journal, "DEMO-12", config, registry))
    plan = Path(journal.get("DEMO-12")["plan_path"])
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    original = provider.execute

    async def execute(task):
        result = await original(task)
        if task.role == "fixer":
            return AgentResult(success=True, output=answer, exit_code=0, duration_seconds=0)
        return result

    with patch.object(provider, "execute", side_effect=execute):
        with pytest.raises(ValueError, match="decision"):
            asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert journal.get("DEMO-12")["state"] == "WAITING_FOR_HUMAN"
    assert not (plan / "validation.json").exists()
