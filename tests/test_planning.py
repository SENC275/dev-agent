import asyncio
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.models.agent import AgentResult
from dev_agent.models.config import ProjectConfig
from dev_agent.providers.fake import FakeProvider
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.plan_gate import review_plan
from dev_agent.workflow.planning import (
    HEADINGS,
    approval_is_current,
    approve_plan,
    checked_plan,
    edit_plan,
    generate_plan,
    validate_plan,
)

PLAN = "\n\n".join(f"## {heading}\nDetails for {heading}." for heading in HEADINGS)


@pytest.fixture
def investigation(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "baseline",
        ],
        check=True,
        capture_output=True,
    )
    run = tmp_path / ".dev-agent/runs/DEMO/run1"
    run.mkdir(parents=True)
    (run / "ticket.md").write_text("Add a filter")
    (run / "investigation.json").write_text(
        json.dumps(
            {
                "status": "COMPLETE",
                "repository": str(tmp_path),
            }
        )
    )
    (run / "exploration.json").write_text(
        json.dumps(
            {
                key: []
                for key in (
                    "entry_points",
                    "call_chain",
                    "database_writes",
                    "external_calls",
                    "important_files",
                    "risks",
                )
            }
        )
    )
    (run / "patterns.json").write_text('{"patterns": []}')
    (run / "tests.json").write_text(
        '{"existing_tests": [], "fixtures": [], "recommended_tests": []}'
    )
    return run


def provider(output: str = PLAN, success: bool = True) -> tuple[ProviderRegistry, FakeProvider]:
    fake = FakeProvider(
        {
            "planner": AgentResult(
                success=success, output=output, exit_code=0 if success else 1, duration_seconds=0
            )
        }
    )
    return ProviderRegistry(ProjectConfig(), {"codex_cli": lambda config: fake}), fake


def generate(run: Path) -> Path:
    return asyncio.run(generate_plan(run, provider()[0]))


def test_generate_fresh_planner_and_approve(investigation: Path) -> None:
    registry, fake = provider()
    directory = asyncio.run(generate_plan(investigation, registry))
    assert (directory / "plan.md").read_text() == PLAN
    assert not (directory / "approval.json").exists()
    assert fake.tasks[0].role == "planner" and fake.tasks[0].read_only
    assert fake.tasks[0].working_directory == investigation.parents[3]
    assert "Add a filter" in fake.tasks[0].prompt
    assert all(
        name in fake.tasks[0].prompt for name in ("exploration.json", "patterns.json", "tests.json")
    )
    assert not asyncio.run(approval_is_current(directory))
    asyncio.run(approve_plan(directory, PLAN))
    assert asyncio.run(approval_is_current(directory))
    approval = json.loads((directory / "approval.json").read_text())
    assert approval["decision"] == "APPROVED" and len(approval["plan_sha256"]) == 64
    assert generate(investigation) != directory


@pytest.mark.parametrize(
    "contents", ["", "## Summary\nonly summary", PLAN.replace("Details for Risks.", "")]
)
def test_invalid_plan(contents: str) -> None:
    with pytest.raises(ValueError):
        validate_plan(contents)


@pytest.mark.parametrize("case", ["incomplete", "bad_json", "provider_failure", "bad_plan"])
def test_generation_failure_no_plan(investigation: Path, case: str) -> None:
    registry, fake = provider(
        "bad plan" if case == "bad_plan" else PLAN, success=case != "provider_failure"
    )
    if case == "incomplete":
        p = investigation / "investigation.json"
        p.write_text(p.read_text().replace("COMPLETE", "FAILED"))
    if case == "bad_json":
        (investigation / "patterns.json").write_text("invalid")
    with pytest.raises(ValueError):
        asyncio.run(generate_plan(investigation, registry))
    assert not (investigation / "plans").exists()
    if case in ("incomplete", "bad_json"):
        assert not fake.tasks


def test_edit_invalidates_approval(investigation: Path) -> None:
    directory = generate(investigation)
    asyncio.run(approve_plan(directory, PLAN))
    updated = PLAN.replace("Details for Summary.", "Updated scope.")
    asyncio.run(edit_plan(directory, updated, PLAN))
    assert not (directory / "approval.json").exists()
    assert asyncio.run(checked_plan(directory)) == updated
    asyncio.run(approve_plan(directory, updated))
    assert asyncio.run(approval_is_current(directory))
    (directory / "plan.md").write_text(PLAN)
    assert not asyncio.run(approval_is_current(directory))


def test_approval_rejects_changed_display(investigation: Path) -> None:
    directory = generate(investigation)
    (directory / "plan.md").write_text(PLAN + "\nNew detail")
    with pytest.raises(ValueError, match="changed after display"):
        asyncio.run(approve_plan(directory, PLAN))
    assert not (directory / "approval.json").exists()


@pytest.mark.parametrize("change", ["input", "head"])
def test_stale_inputs_or_head_block_approval(investigation: Path, change: str) -> None:
    directory = generate(investigation)
    if change == "input":
        (investigation / "ticket.md").write_text("Changed requirements")
    else:
        subprocess.run(
            [
                "git",
                "-C",
                str(investigation.parents[3]),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "next",
            ],
            check=True,
            capture_output=True,
        )
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(approve_plan(directory, PLAN))


def test_noninteractive_gate_never_prompts(investigation: Path) -> None:
    directory = generate(investigation)
    with (
        patch("dev_agent.workflow.plan_gate.sys.stdin.isatty", return_value=False),
        patch(
            "dev_agent.workflow.plan_gate.typer.prompt",
            side_effect=AssertionError("must not prompt"),
        ),
    ):
        assert not review_plan(directory, Console(file=io.StringIO()))
    assert not (directory / "approval.json").exists()


@pytest.mark.parametrize("choice,approved", [("A", True), ("Q", False)])
def test_interactive_gate(investigation: Path, choice: str, approved: bool) -> None:
    directory = generate(investigation)
    with (
        patch("dev_agent.workflow.plan_gate.sys.stdin.isatty", return_value=True),
        patch("dev_agent.workflow.plan_gate.typer.prompt", return_value=choice),
    ):
        assert review_plan(directory, Console(file=io.StringIO())) == approved
    assert asyncio.run(approval_is_current(directory)) == approved


def test_cli_plan_noninteractive(investigation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(investigation.parents[3])
    Path(".dev-agent.yaml").write_text("{}")
    with (
        patch("dev_agent.cli.ProviderRegistry", return_value=provider()[0]),
        patch("dev_agent.workflow.plan_gate.sys.stdin.isatty", return_value=False),
    ):
        result = CliRunner().invoke(app, ["plan", str(investigation)])
    assert result.exit_code == 2, result.output
    assert "approval requires an interactive terminal" in result.output


def test_edit_then_approve_displays_updated_plan(investigation: Path) -> None:
    directory = generate(investigation)
    updated = PLAN.replace("Details for Summary.", "Updated human plan.")
    display = io.StringIO()

    def editor(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(args[-1]).write_text(updated)
        return subprocess.CompletedProcess(args, 0)

    with (
        patch("dev_agent.workflow.plan_gate.sys.stdin.isatty", return_value=True),
        patch("dev_agent.workflow.plan_gate.typer.prompt", side_effect=["E", "A"]),
        patch("dev_agent.workflow.plan_gate.subprocess.run", side_effect=editor),
    ):
        # Patching subprocess.run does not affect the async Git runner.
        assert review_plan(directory, Console(file=display))
    assert "Updated human plan." in display.getvalue()
    assert (directory / "plan.md").read_text() == updated
    assert asyncio.run(approval_is_current(directory))


def test_editor_failure_preserves_original(investigation: Path) -> None:
    directory = generate(investigation)
    with (
        patch("dev_agent.workflow.plan_gate.sys.stdin.isatty", return_value=True),
        patch("dev_agent.workflow.plan_gate.typer.prompt", side_effect=["E", "Q"]),
        patch(
            "dev_agent.workflow.plan_gate.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["editor"],
                1,
            ),
        ),
    ):
        assert not review_plan(directory, Console(file=io.StringIO()))
    assert (directory / "plan.md").read_text() == PLAN
    assert not (directory / "approval.json").exists()


def test_request_changes_then_quit_and_approve_later(investigation: Path) -> None:
    directory = generate(investigation)
    updated = PLAN.replace("Details for Summary.", "Keep CloudWatch logs during migration.")
    registry, fake = provider(updated)
    display = io.StringIO()
    with (
        patch("dev_agent.workflow.plan_gate.sys.stdin.isatty", return_value=True),
        patch("dev_agent.workflow.plan_gate.typer.prompt", side_effect=["R", "Keep logs", "Q"]),
    ):
        assert not review_plan(directory, Console(file=display), registry)
    assert "Keep CloudWatch logs" in display.getvalue()
    assert (directory / "plan.md").read_text() == updated
    assert not asyncio.run(approval_is_current(directory))
    assert len(fake.tasks) == 1
    assert fake.tasks[0].read_only and fake.tasks[0].role == "planner"
    assert "Keep logs" in fake.tasks[0].prompt
    history = list((directory / "human-plan-revisions").iterdir())
    assert len(history) == 1
    assert (history[0] / "before.md").read_text() == PLAN
    assert (history[0] / "feedback.md").read_text() == "Keep logs"
    assert json.loads((history[0] / "status.json").read_text())["status"] == "COMPLETE"
    with (
        patch("dev_agent.workflow.plan_gate.sys.stdin.isatty", return_value=True),
        patch("dev_agent.workflow.plan_gate.typer.prompt", return_value="A"),
    ):
        assert review_plan(directory, Console(file=io.StringIO()))
    assert asyncio.run(approval_is_current(directory))


@pytest.mark.parametrize("output,success", [("invalid plan", True), (PLAN, False)])
def test_failed_plan_request_preserves_plan_and_revokes_approval(
    investigation: Path, output: str, success: bool
) -> None:
    from dev_agent.workflow.plan_revision import request_changes

    directory = generate(investigation)
    asyncio.run(approve_plan(directory, PLAN))
    with pytest.raises(ValueError):
        asyncio.run(request_changes(directory, "Change plan", provider(output, success)[0], PLAN))
    assert (directory / "plan.md").read_text() == PLAN
    assert not asyncio.run(approval_is_current(directory))
    assert not (directory / "plan-review.lock").exists()
    attempt = next((directory / "human-plan-revisions").iterdir())
    assert json.loads((attempt / "status.json").read_text())["status"] == "FAILED"


@pytest.mark.parametrize("block", ["plan-review.lock", "worktree.lock", "worktree.json"])
def test_plan_request_blocks_concurrent_work(investigation: Path, block: str) -> None:
    from dev_agent.workflow.plan_revision import request_changes

    directory = generate(investigation)
    (directory / block).write_text("{}")
    registry, fake = provider()
    with pytest.raises(ValueError):
        asyncio.run(request_changes(directory, "Change plan", registry, PLAN))
    assert not fake.tasks
    assert (directory / block).exists()
    assert (directory / "plan.md").read_text() == PLAN


def test_multiple_plan_requests_keep_feedback(investigation: Path) -> None:
    from dev_agent.workflow.plan_revision import request_changes

    directory = generate(investigation)
    first = PLAN.replace("Details for Summary.", "First update")
    second = first.replace("First update", "Second update")
    asyncio.run(request_changes(directory, "Preserve logs", provider(first)[0], PLAN))
    registry, fake = provider(second)
    asyncio.run(request_changes(directory, "Add rollback", registry, first))
    assert "Preserve logs" in fake.tasks[0].prompt
    assert "Add rollback" in fake.tasks[0].prompt
    assert len(list((directory / "human-plan-revisions").iterdir())) == 2
    assert not asyncio.run(approval_is_current(directory))


def test_plan_request_detects_repository_mutation(investigation: Path) -> None:
    from dev_agent.workflow.plan_revision import request_changes

    directory = generate(investigation)
    registry, fake = provider()
    execute = fake.execute

    async def mutate(task):
        (task.working_directory / "unexpected.txt").write_text("changed")
        return await execute(task)

    with (
        patch.object(fake, "execute", side_effect=mutate),
        pytest.raises(ValueError, match="changed"),
    ):
        asyncio.run(request_changes(directory, "Change plan", registry, PLAN))
    assert (directory / "plan.md").read_text() == PLAN
    assert not asyncio.run(approval_is_current(directory))


def test_plan_request_rejects_changed_frozen_config(investigation: Path) -> None:
    from dev_agent.workflow.persistence import Journal
    from dev_agent.workflow.plan_revision import request_changes

    directory = generate(investigation)
    journal = Journal(investigation.parents[3])
    config = ProjectConfig()
    journal.create("DEMO", "Ticket", config.model_dump_json())
    journal.update("DEMO", plan_path=str(directory))
    changed = config.model_copy(update={"knowledge": config.knowledge.model_copy(
        update={"enabled": True}
    )})
    registry, fake = provider()
    registry.config = changed
    with pytest.raises(ValueError, match="Configuration changed"):
        asyncio.run(request_changes(directory, "Change plan", registry, PLAN))
    assert not fake.tasks


def test_human_request_cannot_be_auto_approved(investigation: Path) -> None:
    from dev_agent.models.config import GatesConfig
    from dev_agent.workflow.agent_plan_gate import review_plan_by_agent
    from dev_agent.workflow.plan_revision import request_changes

    directory = generate(investigation)
    registry, fake = provider()
    asyncio.run(request_changes(directory, "Change plan", registry, PLAN))
    registry.config = registry.config.model_copy(update={"gates": GatesConfig(plan_review="agent")})
    assert not asyncio.run(review_plan_by_agent(directory, registry))
    assert len(fake.tasks) == 1
    with pytest.raises(ValueError, match="explicit human approval"):
        asyncio.run(approve_plan(directory, PLAN, actor="agent", review_sha256="fake"))
    asyncio.run(approve_plan(directory, PLAN))
    assert asyncio.run(review_plan_by_agent(directory, registry))


def test_empty_plan_feedback_cancels_without_call(investigation: Path) -> None:
    directory = generate(investigation)
    registry, fake = provider()
    with (
        patch("dev_agent.workflow.plan_gate.sys.stdin.isatty", return_value=True),
        patch("dev_agent.workflow.plan_gate.typer.prompt", side_effect=["R", "", "Q"]),
    ):
        assert not review_plan(directory, Console(file=io.StringIO()), registry)
    assert not fake.tasks
    assert not (directory / "human-plan-revisions").exists()
