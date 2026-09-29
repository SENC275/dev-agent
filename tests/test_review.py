import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from test_implementation import prepared as prepared
from test_implementation import registry
from test_validation import commands
from test_validation import implemented as implemented
from test_worktree import approved_plan as approved_plan
from test_worktree import git
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ValidationConfig
from dev_agent.providers.fake import FakeProvider
from dev_agent.validation.runner import validate
from dev_agent.workflow.review import review


@pytest.fixture
def validated(implemented: tuple[Path, Path, Path]) -> tuple[Path, Path, Path]:
    asyncio.run(validate(implemented[0], commands(), ValidationConfig()))
    return implemented


def reviewer(output: str = '{"findings": []}', success: bool = True) -> FakeProvider:
    return FakeProvider(
        {"reviewer": AgentResult(success=success, output=output, exit_code=0, duration_seconds=0)}
    )


def test_independent_review_and_actual_diff(validated: tuple[Path, Path, Path]) -> None:
    directory, target, _ = validated
    provider = reviewer()
    output = asyncio.run(review(directory, registry(provider)))
    task = provider.tasks[0]
    assert task.role == "reviewer" and task.read_only and task.working_directory == target
    assert "agent_summary" not in task.prompt
    assert "implemented\\n" in task.prompt
    assert "new_test.py" in task.prompt and "regression test placeholder" in task.prompt
    assert "schema" in task.prompt and "validation" in task.prompt
    assert json.loads((output / "review.json").read_text()) == {"findings": []}
    assert json.loads((directory / "review-status.json").read_text())["status"] == "REVIEWED"
    assert not (directory / "review.lock").exists()
    second = asyncio.run(review(directory, registry(reviewer())))
    assert output != second and (output / "review.json").exists()


@pytest.mark.parametrize(
    "change", ["tracked", "new", "failed_validation", "legacy_validation", "approval"]
)
def test_stale_or_invalid_inputs_never_call_provider(
    validated: tuple[Path, Path, Path], change: str
) -> None:
    directory, target, _ = validated
    if change in ("tracked", "new"):
        (target / ("app.txt" if change == "tracked" else "new_test.py")).write_text("changed")
    elif change == "approval":
        (directory / "approval.json").unlink()
    else:
        path = directory / "validation.json"
        data = json.loads(path.read_text())
        if change == "failed_validation":
            data["status"] = "FAILED"
        else:
            data.pop("snapshot_sha256")
        path.write_text(json.dumps(data))
    provider = reviewer()
    with pytest.raises(ValueError):
        asyncio.run(review(directory, registry(provider)))
    assert not provider.tasks


@pytest.mark.parametrize("mode", ["invalid", "failure", "cancel", "mutation"])
def test_failed_review_invalidates_latest(validated: tuple[Path, Path, Path], mode: str) -> None:
    directory, target, _ = validated
    original = asyncio.run(review(directory, registry(reviewer())))

    class FailedReviewer(FakeProvider):
        async def execute(self, task: AgentTask) -> AgentResult:
            if mode == "cancel":
                raise asyncio.CancelledError()
            if mode == "mutation":
                (target / "app.txt").write_text("unauthorized edit")
            return AgentResult(
                success=mode != "failure",
                exit_code=0,
                duration_seconds=0,
                output="invalid" if mode == "invalid" else '{"findings": []}',
            )

    with pytest.raises(asyncio.CancelledError if mode == "cancel" else ValueError):
        asyncio.run(review(directory, registry(FailedReviewer({}))))
    assert not (directory / "review.json").exists()
    status = json.loads((directory / "review-status.json").read_text())["status"]
    assert status == ("INTERRUPTED" if mode == "cancel" else "FAILED")
    assert (original / "review.json").exists()
    assert not (directory / "review.lock").exists()


def test_findings_are_advisory_and_cli_reports_counts(validated: tuple[Path, Path, Path]) -> None:
    directory, _, _ = validated
    findings = {
        "findings": [
            {
                "severity": "high",
                "category": "correctness",
                "file": "app.txt",
                "line": 1,
                "scenario": "A request fails",
                "impact": "Wrong result",
                "recommendation": "Handle the missing case",
            }
        ]
    }
    with patch(
        "dev_agent.cli.ProviderRegistry", return_value=registry(reviewer(json.dumps(findings)))
    ):
        result = CliRunner().invoke(app, ["review", str(directory)])
    assert result.exit_code == 0, result.output
    assert "high: 1" in result.output
    assert json.loads((directory / "review.json").read_text()) == findings


def test_snapshot_ignored_files_and_binary_additions(validated: tuple[Path, Path, Path]) -> None:
    _, target, source = validated
    base = git(target, "rev-parse", "HEAD")
    before = asyncio.run(capture_snapshot(target, base))
    exclude = source / ".git/info/exclude"
    with exclude.open("a") as stream:
        stream.write("\nignored.cache\n")
    (target / "ignored.cache").write_text("generated")
    assert asyncio.run(capture_snapshot(target, base)).fingerprint == before.fingerprint
    (target / "binary.dat").write_bytes(b"\x00\xff")
    after = asyncio.run(capture_snapshot(target, base))
    assert after.fingerprint != before.fingerprint
    assert "New binary" in after.diff


def test_snapshot_refuses_env_files(validated: tuple[Path, Path, Path]) -> None:
    _, target, _ = validated
    (target / ".env").write_text("PRIVATE=not-real")
    with pytest.raises(ValueError, match=".env"):
        asyncio.run(capture_snapshot(target, git(target, "rev-parse", "HEAD")))


def test_validation_cannot_pass_after_test_rewrites_code(
    implemented: tuple[Path, Path, Path],
) -> None:
    from test_validation import command

    directory, _, _ = implemented
    checks = commands(command('from pathlib import Path; Path("app.txt").write_text("mutated")'))
    with pytest.raises(ValueError, match="content changed"):
        asyncio.run(validate(directory, checks, ValidationConfig()))
    record = json.loads((directory / "validation.json").read_text())
    assert record["status"] == "FAILED"
    assert record["commands"][1]["status"] == "NOT_RUN"
