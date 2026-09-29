import asyncio
import json
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import pytest
from test_investigation import PAYLOADS
from test_validation import commands
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ProjectConfig
from dev_agent.providers.fake import FakeProvider
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.planning import HEADINGS, approve_plan
from dev_agent.workflow.runner import advance


class PipelineProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__({})

    async def execute(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        if task.role in PAYLOADS:
            output = json.dumps(PAYLOADS[task.role])
        elif task.role == "planner":
            output = "\n".join(f"## {heading}\nSmall change.\n" for heading in HEADINGS)
        elif task.role == "implementer":
            (task.working_directory / "app.txt").write_text("implemented\n")
            output = "Implemented."
        else:
            assert task.role == "reviewer"
            output = '{"findings": []}'
        return AgentResult(success=True, output=output, exit_code=0, duration_seconds=0)


def setup(repository: Path) -> tuple[Journal, ProjectConfig, PipelineProvider, ProviderRegistry]:
    journal = Journal(repository)
    config = ProjectConfig(commands=commands())
    provider = PipelineProvider()
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    journal.create("DEMO-12", "Change app", config.model_dump_json())
    return journal, config, provider, registry


def test_restart_after_approval_completes_and_detects_stale_code(repository: Path) -> None:
    journal, config, provider, registry = setup(repository)
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "AWAITING_PLAN_APPROVAL"
    assert len(provider.tasks) == 4
    plan = Path(journal.get("DEMO-12")["plan_path"] or "")
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    journal = Journal(repository)  # Fresh connection/process state.
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert len(provider.tasks) == 6
    assert all(step["status"] == "COMPLETE" for step in journal.steps("DEMO-12"))
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert len(provider.tasks) == 6
    target = Path(journal.get("DEMO-12")["worktree_path"] or "")
    (target / "app.txt").write_text("later edits\n")
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "WAITING_FOR_HUMAN"
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "WAITING_FOR_HUMAN"


@pytest.mark.parametrize("stage", ["worktree", "implement", "validate", "fix"])
def test_uncertain_writes_are_never_replayed(repository: Path, stage: str) -> None:
    journal, config, provider, registry = setup(repository)
    journal.begin("DEMO-12", stage)  # Simulate death before completion is recorded.
    assert (
        asyncio.run(advance(Journal(repository), "DEMO-12", config, registry))
        == "WAITING_FOR_HUMAN"
    )
    assert not provider.tasks


def test_readonly_interruption_can_retry(repository: Path) -> None:
    journal, config, provider, registry = setup(repository)
    journal.begin("DEMO-12", "investigate")
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "AWAITING_PLAN_APPROVAL"
    assert len(provider.tasks) == 4


def test_duplicate_ticket_and_concurrent_resume_rejected(repository: Path) -> None:
    journal, config, provider, registry = setup(repository)
    with pytest.raises(ValueError, match="already exists"):
        journal.create("DEMO-12", "different", config.model_dump_json())
    with journal.lock("DEMO-12"), pytest.raises(ValueError, match="already running"):
        asyncio.run(advance(Journal(repository), "DEMO-12", config, registry))
    assert not provider.tasks


def test_config_change_rejected(repository: Path) -> None:
    journal, config, provider, registry = setup(repository)
    config.limits.max_fix_cycles += 1
    with pytest.raises(ValueError, match="Configuration changed"):
        asyncio.run(advance(journal, "DEMO-12", config, registry))
    assert not provider.tasks


def test_cli_noninteractive_start_status_resume(repository: Path, tmp_path: Path) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("Change app")
    provider = PipelineProvider()
    registry = ProviderRegistry(ProjectConfig(), {"codex_cli": lambda _: provider})
    runner = CliRunner()
    with chdir(repository), patch("dev_agent.cli.ProviderRegistry", return_value=registry):
        started = runner.invoke(app, ["start", "DEMO-12", "--file", str(ticket)])
        assert started.exit_code == 2, started.output
        resumed = runner.invoke(app, ["resume", "DEMO-12"])
        assert resumed.exit_code == 2, resumed.output
        assert len(provider.tasks) == 4
        status = runner.invoke(app, ["status", "DEMO-12"])
        assert status.exit_code == 0
        assert "AWAITING_PLAN_APPROVAL" in status.output
        assert "Change app" not in status.output
        assert runner.invoke(app, ["status", "missing"]).exit_code == 1
