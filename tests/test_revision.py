import asyncio
import json
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import pytest
from test_persistence import PipelineProvider, setup
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.cli import revise as revise_command
from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ProjectConfig
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.locking import check_fix_lock
from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.planning import approve_plan
from dev_agent.workflow.revision import revise
from dev_agent.workflow.runner import advance


@pytest.fixture
def ready(repository: Path) -> tuple[Journal, ProjectConfig, PipelineProvider, ProviderRegistry]:
    journal, config, provider, registry = setup(repository)
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    plan = Path(journal.get("DEMO-12")["plan_path"] or "")
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    return journal, config, provider, registry


def test_two_feedback_rounds_are_reviewed_and_resumable(ready: tuple) -> None:
    journal, config, _, _ = ready

    class Changing(PipelineProvider):
        async def execute(self, task: AgentTask) -> AgentResult:
            result = await super().execute(task)
            if task.role == "implementer":
                (task.working_directory / "app.txt").write_text(f"revised {len(self.tasks)}")
            return result

    provider = Changing()
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    plan = Path(journal.get("DEMO-12")["plan_path"])
    original = (plan / "implementation.json").read_bytes()
    approval = (plan / "approval.json").read_bytes()
    for feedback in ["Add clear errors", "Also add a regression test"]:
        assert (
            asyncio.run(revise(journal, "DEMO-12", feedback, config, registry))
            == "READY_FOR_HUMAN_REVIEW"
        )
        assert feedback in provider.tasks[-1].prompt
        assert provider.tasks[-1].role == "reviewer" and provider.tasks[-1].read_only
        assert (
            asyncio.run(advance(journal, "DEMO-12", config, registry)) == "READY_FOR_HUMAN_REVIEW"
        )
    assert "Add clear errors" in provider.tasks[-1].prompt
    assert len(list((plan / "revisions").iterdir())) == 2
    assert len([s for s in journal.steps("DEMO-12") if s["name"] == "revise"]) == 2
    assert (plan / "implementation.json").read_bytes() == original
    assert (plan / "approval.json").read_bytes() == approval
    assert not (plan / "revision.lock").exists()


@pytest.mark.parametrize("mode", ["failure", "cancel"])
def test_failed_revision_preserves_edits_and_cannot_be_replayed(ready: tuple, mode: str) -> None:
    journal, config, _, _ = ready

    class Failed(PipelineProvider):
        async def execute(self, task: AgentTask) -> AgentResult:
            (task.working_directory / "app.txt").write_text("partial edit")
            if mode == "cancel":
                raise asyncio.CancelledError()
            return AgentResult(success=False, output="Failed", exit_code=1, duration_seconds=0)

    provider = Failed()
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    with pytest.raises(asyncio.CancelledError if mode == "cancel" else ValueError):
        asyncio.run(revise(journal, "DEMO-12", "Fix it", config, registry))
    plan = Path(journal.get("DEMO-12")["plan_path"])
    record = json.loads(next((plan / "revisions").glob("*/revision.json")).read_text())
    assert record["status"] == ("INTERRUPTED" if mode == "cancel" else "FAILED")
    assert not (plan / "revision.lock").exists()
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "WAITING_FOR_HUMAN"
    target = Path(journal.get("DEMO-12")["worktree_path"])
    assert (target / "app.txt").read_text() == "partial edit"
    with pytest.raises(ValueError, match="completed run"):
        asyncio.run(revise(journal, "DEMO-12", "retry", config, registry))


def test_empty_feedback_and_busy_lock_do_not_launch_provider(ready: tuple) -> None:
    journal, config, provider, registry = ready
    count = len(provider.tasks)
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(revise(journal, "DEMO-12", "  ", config, registry))
    with journal.lock("DEMO-12"), pytest.raises(ValueError, match="already running"):
        asyncio.run(revise(journal, "DEMO-12", "Change", config, registry))
    plan = Path(journal.get("DEMO-12")["plan_path"])
    (plan / "revision.lock").write_text("other process")
    with pytest.raises(ValueError, match="revision is running"):
        check_fix_lock(plan)
    assert len(provider.tasks) == count


def test_cli_file_and_interactive_feedback(ready: tuple, tmp_path: Path) -> None:
    journal, config, _, registry = ready
    feedback = tmp_path / "feedback.md"
    feedback.write_text("Improve error messages")
    cli = CliRunner()
    with (
        chdir(journal.repository),
        patch("dev_agent.cli.load_config", return_value=config),
        patch("dev_agent.cli.ProviderRegistry", return_value=registry),
    ):
        result = cli.invoke(app, ["revise", "DEMO-12", "--file", str(feedback)])
        assert result.exit_code == 0, result.output
        assert "Revision ready" in result.output
        result = cli.invoke(app, ["revise", "DEMO-12"])
        assert result.exit_code == 1 and "--file" in result.output
        with (
            patch("dev_agent.cli.sys.stdin.isatty", return_value=True),
            patch("dev_agent.cli.typer.prompt", return_value="Add a test"),
        ):
            revise_command("DEMO-12", None)
            assert journal.get("DEMO-12")["state"] == "READY_FOR_HUMAN_REVIEW"


def test_validation_failure_stops_before_review(ready: tuple) -> None:
    from test_validation import command, commands

    from dev_agent.validation.runner import validate

    journal, config, provider, registry = ready
    previous = len(provider.tasks)

    async def failing_validation(directory: Path, *_args: object) -> object:
        return await validate(
            directory, commands(command("raise SystemExit(1)")), config.validation
        )

    with patch("dev_agent.workflow.revision.validate", side_effect=failing_validation):
        with pytest.raises(ValueError, match="validation failed"):
            asyncio.run(revise(journal, "DEMO-12", "Change", config, registry))
    assert len(provider.tasks) == previous + 1
    assert provider.tasks[-1].role == "implementer"
    assert journal.get("DEMO-12")["state"] == "FAILED"


def test_stale_worktree_refuses_revision_without_provider(ready: tuple) -> None:
    journal, config, provider, registry = ready
    before = len(provider.tasks)
    target = Path(journal.get("DEMO-12")["worktree_path"])
    (target / "app.txt").write_text("manual edits")
    with pytest.raises(ValueError, match="stale"):
        asyncio.run(revise(journal, "DEMO-12", "Change", config, registry))
    assert len(provider.tasks) == before
    assert (target / "app.txt").read_text() == "manual edits"
