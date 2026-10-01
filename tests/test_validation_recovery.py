import asyncio
import json
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import pytest
from test_persistence import PipelineProvider
from test_validation import command, commands
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.git.worktree import create_approved_worktree
from dev_agent.models.agent import AgentResult
from dev_agent.models.config import ProjectConfig
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.validation.runner import validate
from dev_agent.workflow.implementation import implement
from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.planning import approve_plan
from dev_agent.workflow.runner import advance


class RepairProvider(PipelineProvider):
    async def execute(self, task):
        if task.role == "fixer":
            self.tasks.append(task)
            assert "validation_failures" in task.prompt and "AssertionError" in task.prompt
            (task.working_directory / "app.txt").write_text("fixed\n")
            return AgentResult(
                success=True,
                exit_code=0,
                duration_seconds=0,
                output='{"decisions":[],"summary":"Fixed app.txt"}',
            )
        return await super().execute(task)


def prepare(repo, limit=2):
    config = ProjectConfig(
        commands=commands(
            command("from pathlib import Path; assert Path('app.txt').read_text() == 'fixed\\n'")
        )
    )
    config.limits.max_fix_cycles = limit
    provider = RepairProvider()
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    journal = Journal(repo)
    journal.create("REPAIR", "Fix app", config.model_dump_json())
    assert asyncio.run(advance(journal, "REPAIR", config, registry)) == "AWAITING_PLAN_APPROVAL"
    plan = Path(journal.get("REPAIR")["plan_path"])
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    return journal, config, provider, registry, plan


def test_initial_failure_routes_to_fixer(repository):
    journal, config, provider, registry, plan = prepare(repository)
    assert asyncio.run(advance(journal, "REPAIR", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert [t.role for t in provider.tasks][-3:] == ["implementer", "fixer", "reviewer"]
    assert (plan / "fixes/0001/input-validation.json").exists()
    assert asyncio.run(advance(journal, "REPAIR", config, registry)) == "READY_FOR_HUMAN_REVIEW"


def legacy_failure(repo):
    journal, config, provider, registry, plan = prepare(repo)
    wt = asyncio.run(create_approved_worktree(plan, config.git))
    journal.update("REPAIR", worktree_path=str(wt.path), branch=wt.branch)
    journal.finish(journal.begin("REPAIR", "worktree"), "COMPLETE", plan)
    asyncio.run(implement(plan, registry))
    journal.finish(journal.begin("REPAIR", "implement"), "COMPLETE", plan)
    report = asyncio.run(validate(plan, config.commands, config.validation))
    assert not report.success
    journal.finish(journal.begin("REPAIR", "validate"), "FAILED", report.directory)
    journal.update("REPAIR", state="FAILED")
    return journal, config, provider, registry, plan, Path(wt.path)


def test_resume_old_failed_validation_does_not_repeat_implementation(repository):
    journal, config, provider, registry, plan, target = legacy_failure(repository)
    count = len(provider.tasks)
    assert (
        asyncio.run(advance(Journal(repository), "REPAIR", config, registry))
        == "READY_FOR_HUMAN_REVIEW"
    )
    assert [t.role for t in provider.tasks[count:]] == ["fixer", "reviewer"]
    assert (target / "app.txt").read_text() == "fixed\n"


@pytest.mark.parametrize("tamper", ["code", "archive", "timeout", "interrupted"])
def test_resume_refuses_unsafe_validation(repository, tamper):
    journal, config, provider, registry, plan, target = legacy_failure(repository)
    if tamper == "code":
        (target / "app.txt").write_text("human edits")
    else:
        report = json.loads((plan / "validation.json").read_text())
        if tamper == "timeout":
            report["commands"][0]["timed_out"] = True
        elif tamper == "interrupted":
            report["status"] = "INTERRUPTED"
        else:
            report["run_id"] = "missing"
        text = json.dumps(report)
        (plan / "validation.json").write_text(text)
        if tamper != "archive":
            (Path(report["directory"]) / "validation.json").write_text(text)
    count = len(provider.tasks)
    with pytest.raises((ValueError, OSError)):
        asyncio.run(advance(journal, "REPAIR", config, registry))
    assert len(provider.tasks) == count


def test_budget_exhaustion_needs_human(repository):
    journal, config, provider, registry, plan = prepare(repository, limit=0)
    assert asyncio.run(advance(journal, "REPAIR", config, registry)) == "WAITING_FOR_HUMAN"
    assert not any(t.role == "fixer" for t in provider.tasks)


def test_dirty_start_does_not_create_ticket_or_call_provider(repository, tmp_path):
    (repository / "app.txt").write_text("uncommitted")
    ticket = tmp_path / "ticket.md"
    ticket.write_text("Fix app")
    with chdir(repository), patch("dev_agent.cli.ProviderRegistry") as registry:
        result = CliRunner().invoke(app, ["start", "DIRTY", "--file", str(ticket)])
    assert result.exit_code == 1 and "clean, committed" in result.output
    registry.assert_not_called()
    with pytest.raises(ValueError):
        Journal(repository).get("DIRTY")


def test_large_deletion_is_visible_to_review(repository):
    from test_worktree import git

    from dev_agent.workflow.implementation import change_warnings

    (repository / "README.md").write_text("original text\n" * 100)
    git(repository, "add", "README.md")
    git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "docs",
    )
    base = git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("replacement\n")
    warnings = asyncio.run(change_warnings(repository, base))
    assert len(warnings) == 1 and "README.md" in warnings[0]


def test_validation_summary_contains_actionable_result():
    from dev_agent.validation.runner import CommandResult, ValidationRun, failure_summary

    report = ValidationRun(
        run_id="test",
        directory=Path("/tmp/test"),
        worktree=Path("/tmp/work"),
        base_commit="base",
        plan_sha256="plan",
        started_at="now",
        timeout_seconds=1,
        status="FAILED",
        commands=[
            CommandResult(
                name="test",
                command="pytest",
                arguments=["pytest"],
                cwd="/tmp/work",
                status="FAILED",
                exit_code=1,
                stdout="14 passed, 19 errors in 1.44s\n",
            )
        ],
    )
    assert "19 errors" in failure_summary(report)


def test_environment_failure_does_not_spend_budget_and_resume_revalidates(repository, tmp_path):
    marker = tmp_path / "environment-ready"
    checks = commands(
        command(
            f"from pathlib import Path; import sys; "
            f"ready=Path({str(marker)!r}).exists(); "
            "print('ready' if ready else 'Cannot connect to the Docker daemon'); "
            "sys.exit(0 if ready else 1)"
        )
    )
    config = ProjectConfig(commands=checks)
    provider = RepairProvider()
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    journal = Journal(repository)
    journal.create("ENV", "Fix app", config.model_dump_json())
    asyncio.run(advance(journal, "ENV", config, registry))
    plan = Path(journal.get("ENV")["plan_path"])
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    assert asyncio.run(advance(journal, "ENV", config, registry)) == "WAITING_FOR_HUMAN"
    assert not any(t.role == "fixer" for t in provider.tasks)
    assert not list((plan / "fixes").iterdir())
    marker.touch()
    assert asyncio.run(advance(journal, "ENV", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert len([t for t in provider.tasks if t.role == "implementer"]) == 1
    assert not any(t.role == "fixer" for t in provider.tasks)


def test_resume_completed_pause_accepts_manual_fix(repository):
    journal, config, provider, registry, plan = prepare(repository, limit=0)
    assert asyncio.run(advance(journal, "REPAIR", config, registry)) == "WAITING_FOR_HUMAN"
    target = Path(journal.get("REPAIR")["worktree_path"])
    (target / "app.txt").write_text("fixed\n")
    assert asyncio.run(advance(Journal(repository), "REPAIR", config, registry)) == (
        "READY_FOR_HUMAN_REVIEW"
    )
    assert [t.role for t in provider.tasks].count("implementer") == 1
    assert not any(t.role == "fixer" for t in provider.tasks)
    assert journal.get("REPAIR")["current_step"] == "complete"


def test_resume_budget_is_explicit_and_persistent(repository):
    journal, config, provider, registry, plan = prepare(repository, limit=0)
    assert asyncio.run(advance(journal, "REPAIR", config, registry)) == "WAITING_FOR_HUMAN"
    assert asyncio.run(advance(journal, "REPAIR", config, registry)) == "WAITING_FOR_HUMAN"
    assert not any(t.role == "fixer" for t in provider.tasks)
    frozen = journal.get("REPAIR")["config"]
    assert (
        asyncio.run(
            advance(Journal(repository), "REPAIR", config, registry, additional_fix_cycles=1)
        )
        == "READY_FOR_HUMAN_REVIEW"
    )
    budget = json.loads((plan / "fix-budget.json").read_text())
    assert budget["additional_cycles"] == 1
    assert len(budget["grants"]) == 1
    assert journal.get("REPAIR")["config"] == frozen
    assert json.loads((plan / "fix-cycle.json").read_text())["cycles_used"] == 1
    assert asyncio.run(advance(journal, "REPAIR", config, registry)) == "READY_FOR_HUMAN_REVIEW"
    assert [t.role for t in provider.tasks].count("fixer") == 1
    assert [t.role for t in provider.tasks].count("implementer") == 1


@pytest.mark.parametrize("blocker", ["lock", "receipt", "uncertain"])
def test_resume_refuses_incomplete_cycle_without_granting_budget(repository, blocker):
    journal, config, provider, registry, plan = prepare(repository, limit=0)
    assert asyncio.run(advance(journal, "REPAIR", config, registry)) == "WAITING_FOR_HUMAN"
    if blocker == "lock":
        (plan / "fix-cycle.lock").write_text("busy")
    elif blocker == "receipt":
        receipt = json.loads((plan / "fix-cycle.json").read_text())
        receipt["status"] = "RUNNING"
        (plan / "fix-cycle.json").write_text(json.dumps(receipt))
    else:
        journal.begin("REPAIR", "implement")
    count = len(provider.tasks)
    with pytest.raises(ValueError):
        asyncio.run(advance(journal, "REPAIR", config, registry, additional_fix_cycles=1))
    assert len(provider.tasks) == count
    assert not (plan / "fix-budget.json").exists()


def test_resume_review_pause_and_cli_budget(repository):
    from test_fixing import FINDING, CycleProvider

    class ReviewPauseProvider(PipelineProvider):
        def __init__(self):
            super().__init__()
            self.cycle = CycleProvider()

        async def execute(self, task):
            if task.role == "reviewer" and not self.cycle.fixes:
                self.tasks.append(task)
                return AgentResult(
                    success=True,
                    exit_code=0,
                    duration_seconds=0,
                    output=json.dumps({"findings": [FINDING]}),
                )
            if task.role in {"reviewer", "fixer"}:
                self.tasks.append(task)
                return await self.cycle.execute(task)
            return await super().execute(task)

    config = ProjectConfig(commands=commands())
    config.limits.max_fix_cycles = 0
    provider = ReviewPauseProvider()
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    journal = Journal(repository)
    journal.create("REVIEW", "Change app", config.model_dump_json())
    asyncio.run(advance(journal, "REVIEW", config, registry))
    plan = Path(journal.get("REVIEW")["plan_path"])
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    assert asyncio.run(advance(journal, "REVIEW", config, registry)) == "WAITING_FOR_HUMAN"
    assert journal.steps("REVIEW")[-1]["status"] == "COMPLETE"
    with (
        chdir(repository),
        patch("dev_agent.cli.load_config", return_value=config),
        patch("dev_agent.cli.ProviderRegistry", return_value=registry),
    ):
        result = CliRunner().invoke(
            app,
            [
                "resume",
                "REVIEW",
                "--additional-fix-cycles",
                "1",
            ],
        )
    assert result.exit_code == 0, result.output
    assert journal.get("REVIEW")["state"] == "READY_FOR_HUMAN_REVIEW"
    assert [t.role for t in provider.tasks].count("implementer") == 1
    assert provider.cycle.fixes == 1
