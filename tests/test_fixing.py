import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from test_implementation import prepared as prepared
from test_implementation import registry
from test_review import reviewer
from test_review import validated as validated
from test_validation import command, commands
from test_validation import implemented as implemented
from test_worktree import approved_plan as approved_plan
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import LimitsConfig, ProjectConfig
from dev_agent.providers.fake import FakeProvider
from dev_agent.validation.runner import validate
from dev_agent.workflow.fixing import fix
from dev_agent.workflow.review import review

FINDING = {
    "severity": "high",
    "category": "correctness",
    "file": "app.txt",
    "line": 1,
    "scenario": "Bad input",
    "impact": "Wrong result",
    "recommendation": "Handle bad input",
}


@pytest.fixture
def reviewed(validated: tuple[Path, Path, Path]) -> tuple[Path, Path, Path]:
    asyncio.run(review(validated[0], registry(reviewer(json.dumps({"findings": [FINDING]})))))
    return validated


def config(limit: int = 2) -> ProjectConfig:
    return ProjectConfig(commands=commands(), limits=LimitsConfig(max_fix_cycles=limit))


class CycleProvider(FakeProvider):
    def __init__(self, mode: str = "fixed") -> None:
        super().__init__({})
        self.mode = mode
        self.fixes = 0
        self.reviews = 0

    async def execute(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        if task.role == "fixer":
            self.fixes += 1
            if self.mode == "cancel":
                raise asyncio.CancelledError()
            if self.mode in ("fixed", "validation_recovery"):
                (task.working_directory / "app.txt").write_text(f"fixed {self.fixes}\n")
            if self.mode == "failed":
                return AgentResult(success=False, output="", exit_code=1, duration_seconds=0)
            if self.mode == "malformed":
                output = "{}"
            else:
                disposition = (
                    "rejected"
                    if self.mode.startswith("rejected")
                    else ("deferred" if self.mode == "deferred" else "fixed")
                )
                decisions = (
                    []
                    if self.mode == "missing_decision"
                    else [
                        {
                            "finding_index": 0,
                            "disposition": disposition,
                            "explanation": "Verified against code",
                            "evidence": ["app.txt:1"],
                        }
                    ]
                )
                output = json.dumps(
                    {"decisions": decisions, "summary": "Reported findings checked."}
                )
        else:
            assert task.role == "reviewer" and task.read_only
            self.reviews += 1
            output = json.dumps({"findings": [FINDING] if self.mode == "rejected" else []})
        return AgentResult(success=True, output=output, exit_code=0, duration_seconds=0)


def test_fixed_validated_reviewed_and_finally_validated(reviewed: tuple[Path, Path, Path]) -> None:
    directory, target, source = reviewed
    provider = CycleProvider()
    status = asyncio.run(fix(directory, config(), registry(provider)))
    assert status == "READY_FOR_HUMAN_REVIEW"
    assert provider.fixes == 1 and provider.reviews == 1
    assert provider.tasks[0].role == "fixer" and not provider.tasks[0].read_only
    assert provider.tasks[0].working_directory == target
    assert "schema" in provider.tasks[0].prompt and "approved_plan" in provider.tasks[0].prompt
    assert (source / "app.txt").read_text() == "baseline\n"
    assert json.loads((directory / "final-validation.json").read_text())["status"] == "PASSED"
    assert (
        json.loads((directory / "fixes/0001/fix.json").read_text())["decisions"][0]["disposition"]
        == "fixed"
    )
    assert not (directory / "fix-cycle.lock").exists()


def test_rejected_findings_require_independent_clearance_and_budget_persists(
    reviewed: tuple[Path, Path, Path],
) -> None:
    directory, _, _ = reviewed
    provider = CycleProvider("rejected")
    assert asyncio.run(fix(directory, config(2), registry(provider))) == "WAITING_FOR_HUMAN"
    assert provider.fixes == 2 and provider.reviews == 2
    assert asyncio.run(fix(directory, config(2), registry(provider))) == "WAITING_FOR_HUMAN"
    assert provider.fixes == 2
    assert len(list((directory / "fixes").iterdir())) == 2


def test_zero_budget_never_calls_fixer(reviewed: tuple[Path, Path, Path]) -> None:
    provider = CycleProvider()
    assert asyncio.run(fix(reviewed[0], config(0), registry(provider))) == "WAITING_FOR_HUMAN"
    assert not provider.tasks


def test_no_findings_only_final_validation(validated: tuple[Path, Path, Path]) -> None:
    directory, _, _ = validated
    asyncio.run(review(directory, registry(reviewer())))
    provider = CycleProvider()
    assert asyncio.run(fix(directory, config(0), registry(provider))) == "READY_FOR_HUMAN_REVIEW"
    assert not provider.tasks


@pytest.mark.parametrize("mode", ["failed", "malformed", "missing_decision", "cancel"])
def test_failed_attempt_consumes_budget_and_preserves_history(
    reviewed: tuple[Path, Path, Path], mode: str
) -> None:
    directory, _, _ = reviewed
    provider = CycleProvider(mode)
    with pytest.raises(asyncio.CancelledError if mode == "cancel" else ValueError):
        asyncio.run(fix(directory, config(), registry(provider)))
    record = json.loads((directory / "fixes/0001/attempt.json").read_text())
    assert record["status"] == ("INTERRUPTED" if mode == "cancel" else "FAILED")
    assert not (directory / "fix-cycle.lock").exists()
    # Unchanged code still has a current review, but the failed attempt consumed its budget.
    assert asyncio.run(fix(directory, config(1), registry(provider))) == "WAITING_FOR_HUMAN"
    assert provider.fixes == 1


def test_validation_failure_feedback_and_bounded_recovery(
    reviewed: tuple[Path, Path, Path],
) -> None:
    directory, _, _ = reviewed
    cfg = config()
    cfg.commands.test = command(
        "from pathlib import Path; "
        'raise SystemExit(0 if Path("app.txt").read_text() == "fixed 2\\n" else 1)'
    )
    provider = CycleProvider("validation_recovery")
    assert asyncio.run(fix(directory, cfg, registry(provider))) == "READY_FOR_HUMAN_REVIEW"
    assert provider.fixes == 2 and provider.reviews == 1
    assert '"status": "FAILED"' in provider.tasks[1].prompt


def test_deferred_stops_for_human_even_if_reviewer_is_clear(
    reviewed: tuple[Path, Path, Path],
) -> None:
    provider = CycleProvider("deferred")
    assert asyncio.run(fix(reviewed[0], config(), registry(provider))) == "WAITING_FOR_HUMAN"
    assert provider.fixes == 1


def test_lock_blocks_standalone_stages(reviewed: tuple[Path, Path, Path]) -> None:
    directory, _, _ = reviewed
    (directory / "fix-cycle.lock").write_text("active")
    with pytest.raises(ValueError, match="fix cycle"):
        asyncio.run(validate(directory, config().commands, config().validation))
    with pytest.raises(ValueError, match="fix cycle"):
        asyncio.run(review(directory, registry(reviewer())))
    with pytest.raises(FileExistsError):
        asyncio.run(fix(directory, config(), registry(CycleProvider())))


def test_cli_human_wait_exit(reviewed: tuple[Path, Path, Path]) -> None:
    with (
        patch("dev_agent.cli.load_config", return_value=config(0)),
        patch("dev_agent.cli.ProviderRegistry", return_value=registry(CycleProvider())),
    ):
        result = CliRunner().invoke(app, ["fix", str(reviewed[0])])
    assert result.exit_code == 2 and "WAITING_FOR_HUMAN" in result.output


def test_persistent_validation_failure_stops_at_budget(reviewed: tuple[Path, Path, Path]) -> None:
    cfg = config(1)
    cfg.commands.test = command("raise SystemExit(1)")
    provider = CycleProvider()
    assert asyncio.run(fix(reviewed[0], cfg, registry(provider))) == "WAITING_FOR_HUMAN"
    assert provider.fixes == 1 and provider.reviews == 0


def test_final_validation_failure_is_not_ready(validated: tuple[Path, Path, Path]) -> None:
    directory, _, _ = validated
    asyncio.run(review(directory, registry(reviewer())))
    cfg = config()
    cfg.commands.test = command("raise SystemExit(1)")
    provider = CycleProvider()
    assert asyncio.run(fix(directory, cfg, registry(provider))) == "WAITING_FOR_HUMAN"
    assert not provider.tasks
    assert json.loads((directory / "final-validation.json").read_text())["status"] == "FAILED"


def test_rejected_finding_still_gets_independent_review(reviewed: tuple[Path, Path, Path]) -> None:
    provider = CycleProvider("rejected_clear")
    assert asyncio.run(fix(reviewed[0], config(), registry(provider))) == "READY_FOR_HUMAN_REVIEW"
    assert provider.fixes == 1 and provider.reviews == 1
    report = json.loads((reviewed[0] / "fixes/0001/fix.json").read_text())
    assert report["decisions"][0]["disposition"] == "rejected"


@pytest.mark.parametrize("change", ["code", "approval", "findings"])
def test_stale_inputs_block_fixer(reviewed: tuple[Path, Path, Path], change: str) -> None:
    directory, target, _ = reviewed
    if change == "code":
        (target / "app.txt").write_text("manual edit")
    elif change == "approval":
        (directory / "approval.json").unlink()
    else:
        (directory / "review.json").write_text('{"findings": []}')
    provider = CycleProvider()
    with pytest.raises(ValueError):
        asyncio.run(fix(directory, config(), registry(provider)))
    assert not provider.tasks
