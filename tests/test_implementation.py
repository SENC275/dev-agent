import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from test_worktree import approved_plan as approved_plan
from test_worktree import git
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.git.worktree import create_approved_worktree
from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import GitConfig, ProjectConfig
from dev_agent.providers.fake import FakeProvider
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.implementation import implement


@pytest.fixture
def prepared(approved_plan: Path) -> tuple[Path, Path, Path]:
    result = asyncio.run(create_approved_worktree(approved_plan, GitConfig()))
    return approved_plan, Path(result.path), Path(result.repository)


class WritingProvider(FakeProvider):
    def __init__(self, mode: str = "success") -> None:
        super().__init__({})
        self.mode = mode

    async def execute(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        (task.working_directory / "app.txt").write_text("implemented\n")
        (task.working_directory / "new_test.py").write_text("# regression test placeholder\n")
        if self.mode == "launch":
            raise FileNotFoundError("provider executable")
        if self.mode == "cancel":
            raise asyncio.CancelledError()
        if self.mode == "stage":
            git(task.working_directory, "add", ".")
        if self.mode == "commit":
            git(task.working_directory, "add", ".")
            git(
                task.working_directory,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "unexpected commit",
            )
        return AgentResult(
            success=self.mode == "success",
            output="Changed app; validation not run.",
            exit_code=7 if self.mode == "failure" else 0,
            duration_seconds=0.2,
            timed_out=self.mode == "timeout",
        )


def registry(provider: FakeProvider) -> ProviderRegistry:
    return ProviderRegistry(ProjectConfig(), {"codex_cli": lambda config: provider})


def test_implementation_isolated_and_records_actual_changes(
    prepared: tuple[Path, Path, Path],
) -> None:
    directory, target, source = prepared
    provider = WritingProvider()
    original = git(source, "symbolic-ref", "HEAD")
    result = asyncio.run(implement(directory, registry(provider)))
    assert result.success and result.changed_files == ["app.txt", "new_test.py"]
    assert (source / "app.txt").read_text() == "baseline\n"
    assert git(source, "symbolic-ref", "HEAD") == original
    task = provider.tasks[0]
    assert task.role == "implementer" and not task.read_only
    assert task.working_directory == target
    assert "approved_plan" in task.prompt and "repository_instructions" in task.prompt
    assert "Do not stage, commit" in task.prompt
    assert "Use the smallest safe changes." in task.prompt
    record = json.loads((directory / "implementation.json").read_text())
    assert record["status"] == "IMPLEMENTED"
    assert record["validation_status"] == "NOT_RUN"
    assert record["summary_verified"] is False
    assert not (directory / "implementation.lock").exists()
    with pytest.raises(ValueError):
        asyncio.run(implement(directory, registry(provider)))
    assert len(provider.tasks) == 1


@pytest.mark.parametrize("mode", ["failure", "timeout", "launch", "cancel", "commit", "stage"])
def test_failures_preserve_partial_changes(prepared: tuple[Path, Path, Path], mode: str) -> None:
    directory, target, source = prepared
    provider = WritingProvider(mode)
    if mode in ("launch", "cancel", "commit", "stage"):
        expected = {
            "launch": FileNotFoundError,
            "cancel": asyncio.CancelledError,
            "commit": ValueError,
            "stage": ValueError,
        }
        with pytest.raises(expected[mode]):
            asyncio.run(implement(directory, registry(provider)))
    else:
        assert not asyncio.run(implement(directory, registry(provider))).success
    record = json.loads((directory / "implementation.json").read_text())
    assert record["status"] == ("INTERRUPTED" if mode == "cancel" else "FAILED")
    assert (target / "app.txt").read_text() == "implemented\n"
    assert (source / "app.txt").read_text() == "baseline\n"
    assert not (directory / "implementation.lock").exists()


@pytest.mark.parametrize(
    "case",
    [
        "approval",
        "plan",
        "dirty_target",
        "dirty_source",
        "branch",
        "head",
        "source_path",
        "other_repository",
        "record",
        "lock",
    ],
)
def test_preflight_blocks_provider(
    prepared: tuple[Path, Path, Path], tmp_path: Path, case: str
) -> None:
    directory, target, source = prepared
    if case == "approval":
        (directory / "approval.json").unlink()
    elif case == "plan":
        with (directory / "plan.md").open("a") as stream:
            stream.write("\nChanged scope")
    elif case == "dirty_target":
        (target / "user.txt").write_text("user work")
    elif case == "dirty_source":
        (source / "app.txt").write_text("user work")
    elif case == "branch":
        git(target, "switch", "-c", "another")
    elif case == "head":
        git(
            target,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "unexpected",
        )
    elif case in ("source_path", "other_repository"):
        path = source
        if case == "other_repository":
            path = tmp_path / "other"
            path.mkdir()
            git(path, "init")
        record = json.loads((directory / "worktree.json").read_text())
        record["path"] = str(path)
        (directory / "worktree.json").write_text(json.dumps(record))
    elif case == "record":
        (directory / "implementation.json").write_text('{"status": "FAILED"}')
    else:
        (directory / "implementation.lock").write_text("busy")
    provider = WritingProvider()
    with pytest.raises((ValueError, OSError)):
        asyncio.run(implement(directory, registry(provider)))
    assert not provider.tasks


def test_cli_implementation(prepared: tuple[Path, Path, Path]) -> None:
    directory, _, _ = prepared
    with patch("dev_agent.cli.ProviderRegistry", return_value=registry(WritingProvider())):
        result = CliRunner().invoke(app, ["implement", str(directory)])
    assert result.exit_code == 0, result.output
    assert "validation has not run" in result.output


@pytest.mark.parametrize("change", ["source", "plan"])
def test_changes_during_execution_fail_postchecks(
    prepared: tuple[Path, Path, Path],
    change: str,
) -> None:
    directory, _, source = prepared

    class ConcurrentChangeProvider(WritingProvider):
        async def execute(self, task: AgentTask) -> AgentResult:
            result = await super().execute(task)
            path = source / "app.txt" if change == "source" else directory / "plan.md"
            with path.open("a") as stream:
                stream.write("\nConcurrent change")
            return result

    with pytest.raises(ValueError, match="changed"):
        asyncio.run(implement(directory, registry(ConcurrentChangeProvider())))
    record = json.loads((directory / "implementation.json").read_text())
    assert record["status"] == "FAILED"
    assert record["changed_files"] == ["app.txt", "new_test.py"]
