import asyncio
import json
from pathlib import Path

import pytest
from test_persistence import PipelineProvider
from test_validation import commands
from test_worktree import repository as repository

from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import GatesConfig, ProjectConfig
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.planning import approval_is_current, approve_plan
from dev_agent.workflow.runner import advance


class Reviewing(PipelineProvider):
    def __init__(self, mode: str = "approve") -> None:
        super().__init__()
        self.mode = mode

    async def execute(self, task: AgentTask) -> AgentResult:
        if task.role == "reviewer" and "proposed implementation plan" in task.prompt:
            self.tasks.append(task)
            assert task.read_only
            output = json.dumps(
                {
                    "decision": self.mode
                    if self.mode in {"approve", "request_changes"}
                    else "approve",
                    "summary": "Checked acceptance criteria and tests.",
                    "issues": ["Add an error-handling test."]
                    if self.mode == "request_changes"
                    else [],
                }
            )
            if self.mode == "malformed":
                output = "Looks good!"
            if self.mode == "edit_source":
                (task.working_directory / "app.txt").write_text("unauthorized edit")
            return AgentResult(
                success=self.mode != "failure",
                output=output,
                exit_code=1 if self.mode == "failure" else 0,
                duration_seconds=0,
            )
        return await super().execute(task)


def setup(repository: Path, mode: str = "approve") -> tuple:
    config = ProjectConfig(commands=commands(), gates=GatesConfig(plan_review="agent"))
    journal = Journal(repository)
    journal.create("AUTO-1", "Improve app", config.model_dump_json())
    provider = Reviewing(mode)
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    return journal, config, provider, registry


def test_auto_review_reaches_final_human_review_and_binds_receipt(repository: Path) -> None:
    journal, config, provider, registry = setup(repository)
    assert asyncio.run(advance(journal, "AUTO-1", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    roles = [task.role for task in provider.tasks]
    assert roles.index("planner") < roles.index("reviewer") < roles.index("implementer")
    assert roles.count("reviewer") == 2
    plan = Path(journal.get("AUTO-1")["plan_path"])
    approval = json.loads((plan / "approval.json").read_text())
    assert approval["actor"] == "agent" and approval["review_sha256"]
    assert asyncio.run(approval_is_current(plan))
    assert any(s["name"] == "plan_review" for s in journal.steps("AUTO-1"))
    (plan / "plan-review.json").write_text("{}")
    assert not asyncio.run(approval_is_current(plan))


def test_rejection_stops_before_writes_and_allows_explicit_human_override(repository: Path) -> None:
    journal, config, provider, registry = setup(repository, "request_changes")
    assert asyncio.run(advance(journal, "AUTO-1", config, registry)) == "AWAITING_PLAN_APPROVAL"
    plan = Path(journal.get("AUTO-1")["plan_path"])
    assert not (plan / "approval.json").exists()
    assert not (plan / "worktree.json").exists()
    assert all(task.read_only for task in provider.tasks)
    count = len(provider.tasks)
    assert asyncio.run(advance(journal, "AUTO-1", config, registry)) == "AWAITING_PLAN_APPROVAL"
    assert len(provider.tasks) == count
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    assert asyncio.run(advance(journal, "AUTO-1", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert json.loads((plan / "approval.json").read_text())["actor"] == "human"


@pytest.mark.parametrize("mode", ["malformed", "failure", "edit_source"])
def test_bad_review_never_grants_approval(repository: Path, mode: str) -> None:
    journal, config, provider, registry = setup(repository, mode)
    with pytest.raises(ValueError):
        asyncio.run(advance(journal, "AUTO-1", config, registry))
    plan = Path(journal.get("AUTO-1")["plan_path"])
    assert not (plan / "approval.json").exists()
    assert not (plan / "worktree.json").exists()
    assert not (plan / "plan-review.lock").exists()
    assert all(task.read_only for task in provider.tasks)


def test_legacy_config_snapshot_keeps_default_human_gate(repository: Path) -> None:
    config = ProjectConfig(commands=commands())
    old = config.model_dump()
    del old["gates"]["plan_review"]
    journal = Journal(repository)
    journal.create("LEGACY", "Improve app", json.dumps(old))
    provider = PipelineProvider()
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    assert asyncio.run(advance(journal, "LEGACY", config, registry)) == "AWAITING_PLAN_APPROVAL"
    assert all(task.role != "reviewer" for task in provider.tasks)


def test_gate_configuration_cannot_disable_all_review() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GatesConfig(plan_review="skip")
    with pytest.raises(ValidationError):
        GatesConfig(approve_final=False)


def test_agent_mode_cli_runs_noninteractively_without_human_gate(
    repository: Path, tmp_path: Path
) -> None:
    from contextlib import chdir
    from unittest.mock import patch

    from typer.testing import CliRunner

    from dev_agent.cli import app

    ticket = tmp_path / "ticket.md"
    ticket.write_text("Improve app")
    config = ProjectConfig(commands=commands(), gates=GatesConfig(plan_review="agent"))
    provider = Reviewing()
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    with (
        chdir(repository),
        patch("dev_agent.cli.load_config", return_value=config),
        patch("dev_agent.cli.ProviderRegistry", return_value=registry),
        patch("dev_agent.cli.run_plan_gate", side_effect=AssertionError("Unexpected human gate")),
    ):
        result = CliRunner().invoke(app, ["start", "AUTO-CLI", "--file", str(ticket)])
    assert result.exit_code == 0, result.output
    assert "Ready for human review" in result.output


def test_approval_with_blocking_issues_is_invalid() -> None:
    from pydantic import ValidationError

    from dev_agent.models.plan_review import PlanReview

    with pytest.raises(ValidationError):
        PlanReview(decision="approve", summary="Fine", issues=["Missing tests"])
