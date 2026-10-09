import asyncio
from pathlib import Path

import pytest
from test_persistence import setup
from test_plan_recovery import APPROVE
from test_plan_recovery import setup as agent_setup
from test_worktree import git
from test_worktree import repository as repository

from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.runner import advance


def test_failed_plan_review_reuses_investigation_and_plan(repository):
    journal, config, provider, registry = agent_setup(repository, ["provider_failure", APPROVE])
    with pytest.raises(ValueError, match="provider_execution"):
        asyncio.run(advance(journal, "RECOVER", config, registry))
    before = len(provider.tasks)
    old = journal.get("RECOVER")["plan_path"]
    assert asyncio.run(advance(Journal(repository), "RECOVER", config, registry)) == (
        "READY_FOR_HUMAN_REVIEW"
    )
    assert journal.get("RECOVER")["plan_path"] == old
    assert not any(t.role in {"explorer", "pattern_researcher", "test_researcher", "planner"}
                   for t in provider.tasks[before:])


def test_changed_commit_reinvestigates_and_preserves_old_plan(repository):
    journal, config, provider, registry = setup(repository)
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    old = Path(journal.get("DEMO-12")["plan_path"])
    (repository / "app.txt").write_text("new baseline\n")
    git(repository, "add", "app.txt")
    git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "changed source")
    assert asyncio.run(advance(Journal(repository), "DEMO-12", config, registry)) == (
        "AWAITING_PLAN_APPROVAL"
    )
    assert len(provider.tasks) == 8
    assert old.exists() and Path(journal.get("DEMO-12")["plan_path"]) != old
    assert any(s["name"] == "recovery_reset" for s in journal.steps("DEMO-12"))
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert len(provider.tasks) == 8


def test_dirty_source_blocks_without_calls_or_overwrite(repository):
    journal, config, provider, registry = setup(repository)
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    old = journal.get("DEMO-12")["plan_path"]
    (repository / "new.py").write_text("uncommitted = True")
    with pytest.raises(ValueError, match="Commit or stash"):
        asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert len(provider.tasks) == 4 and journal.get("DEMO-12")["plan_path"] == old
    assert (repository / "new.py").read_text() == "uncommitted = True"


@pytest.mark.parametrize("change", ["missing", "edited", "legacy"])
def test_invalid_research_or_legacy_checkpoint_restarts_safely(repository, change):
    journal, config, provider, registry = setup(repository)
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    path = Path(journal.get("DEMO-12")["investigation_path"]) / "patterns.json"
    if change == "missing":
        path.unlink()
    elif change == "edited":
        path.write_text("{}")
    else:
        for checkpoint in (repository / ".dev-agent").glob("recovery-*.json"):
            checkpoint.unlink()
    asyncio.run(advance(Journal(repository), "DEMO-12", config, registry))
    assert len(provider.tasks) == 8


def test_interrupted_plan_reuses_research(repository):
    journal, config, provider, registry = setup(repository)
    original = registry.execute

    async def interrupt(stage, directory, task):
        if stage == "plan":
            raise asyncio.CancelledError()
        return await original(stage, directory, task)

    registry.execute = interrupt
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert len(provider.tasks) == 3
    registry.execute = original
    assert asyncio.run(advance(Journal(repository), "DEMO-12", config, registry)) == (
        "AWAITING_PLAN_APPROVAL"
    )
    assert len(provider.tasks) == 4


def test_damaged_plan_only_repeats_planning(repository):
    journal, config, provider, registry = setup(repository)
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    investigation = journal.get("DEMO-12")["investigation_path"]
    old = Path(journal.get("DEMO-12")["plan_path"])
    (old / "plan.md").write_text("truncated plan")
    asyncio.run(advance(Journal(repository), "DEMO-12", config, registry))
    assert len(provider.tasks) == 5
    assert journal.get("DEMO-12")["investigation_path"] == investigation
    assert Path(journal.get("DEMO-12")["plan_path"]) != old
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert len(provider.tasks) == 5


def test_source_mutation_during_planning_stops_before_approval(repository):
    journal, config, provider, registry = setup(repository)
    original = registry.execute

    async def mutate(stage, directory, task):
        output = await original(stage, directory, task)
        if stage == "plan":
            (repository / "app.txt").write_text("unexpected change")
        return output

    registry.execute = mutate
    with pytest.raises(ValueError, match="Source changed during read-only"):
        asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert not journal.get("DEMO-12")["worktree_path"]
    assert not any(t.role == "implementer" for t in provider.tasks)
