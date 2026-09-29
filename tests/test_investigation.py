import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ProjectConfig
from dev_agent.providers.fake import FakeProvider
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.investigation import investigate

PAYLOADS = {
    "explorer": {
        "entry_points": ["app.py:1"],
        "call_chain": [],
        "database_writes": [],
        "external_calls": [],
        "important_files": ["app.py"],
        "risks": [],
    },
    "pattern_researcher": {"patterns": []},
    "test_researcher": {"existing_tests": [], "fixtures": [], "recommended_tests": ["test CRUD"]},
}


def fake() -> FakeProvider:
    return FakeProvider(
        {
            role: AgentResult(
                success=True, output=json.dumps(payload), exit_code=0, duration_seconds=0
            )
            for role, payload in PAYLOADS.items()
        }
    )


def registry(provider: FakeProvider) -> ProviderRegistry:
    return ProviderRegistry(ProjectConfig(), {"codex_cli": lambda config: provider})


def test_parallel_read_only_and_persisted_results(tmp_path: Path) -> None:
    class BarrierProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__(fake().responses)
            self.started: set[str] = set()
            self.ready = asyncio.Event()

        async def execute(self, task: AgentTask) -> AgentResult:
            self.started.add(task.role)
            if len(self.started) == 3:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), 2)
            return await super().execute(task)

    async def exercise() -> None:
        provider = BarrierProvider()
        source = tmp_path / "app.py"
        source.write_text("baseline")
        result = await investigate(
            ticket_id="DEMO-1",
            ticket="Add filter",
            repository=tmp_path,
            registry=registry(provider),
        )
        assert result.success
        assert source.read_text() == "baseline"
        assert len(provider.tasks) == 3
        for task in provider.tasks:
            assert task.read_only and task.working_directory == tmp_path
            assert "Add filter" in task.prompt and "Schema:" in task.prompt
            assert "do not edit" in task.prompt
        assert len({task.prompt for task in provider.tasks}) == 3
        for name in ("exploration.json", "patterns.json", "tests.json"):
            assert isinstance(json.loads((result.directory / name).read_text()), dict)
        assert (result.directory / "ticket.md").read_text() == "Add filter"
        manifest = json.loads((result.directory / "investigation.json").read_text())
        assert manifest["status"] == "COMPLETE"
        assert len(manifest["steps"]) == 3
        assert not list(result.directory.glob("*.tmp"))

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["invalid", "exit", "timeout", "launch"])
def test_partial_failure_preserves_valid_artifacts(tmp_path: Path, failure: str) -> None:
    class FailedProvider(FakeProvider):
        async def execute(self, task: AgentTask) -> AgentResult:
            if task.role == "explorer":
                if failure == "launch":
                    raise FileNotFoundError("codex")
                return AgentResult(
                    success=failure == "invalid",
                    output="bad JSON",
                    exit_code=1 if failure == "exit" else 0,
                    timed_out=failure == "timeout",
                    duration_seconds=0,
                )
            return await super().execute(task)

    result = asyncio.run(
        investigate(
            ticket_id="DEMO",
            ticket="Fix it",
            repository=tmp_path,
            registry=registry(FailedProvider(fake().responses)),
        )
    )
    assert not result.success and set(result.errors) == {"explorer"}
    assert not (result.directory / "exploration.json").exists()
    assert (result.directory / "patterns.json").exists()
    assert (result.directory / "tests.json").exists()
    assert json.loads((result.directory / "investigation.json").read_text())["status"] == "FAILED"


def test_repeated_ticket_does_not_overwrite(tmp_path: Path) -> None:
    first, second = [
        asyncio.run(
            investigate(
                ticket_id="DEMO", ticket="Fix it", repository=tmp_path, registry=registry(fake())
            )
        )
        for _ in range(2)
    ]
    assert first.directory != second.directory
    assert (first.directory / "ticket.md").exists()


@pytest.mark.parametrize("ticket_id,ticket", [("../escape", "text"), ("", "text"), ("OK", " ")])
def test_bad_input_does_not_launch(tmp_path: Path, ticket_id: str, ticket: str) -> None:
    provider = fake()
    with pytest.raises(ValueError):
        asyncio.run(
            investigate(
                ticket_id=ticket_id, ticket=ticket, repository=tmp_path, registry=registry(provider)
            )
        )
    assert provider.tasks == []
    assert not (tmp_path / ".dev-agent").exists()


def test_cancellation_cleans_up_all_roles(tmp_path: Path) -> None:
    class WaitingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__({})
            self.active = 0
            self.ready = asyncio.Event()

        async def execute(self, task: AgentTask) -> AgentResult:
            self.active += 1
            if self.active == 3:
                self.ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.active -= 1
            raise AssertionError("unreachable")

    async def exercise() -> None:
        provider = WaitingProvider()
        task = asyncio.create_task(
            investigate(
                ticket_id="CANCEL",
                ticket="Inspect",
                repository=tmp_path,
                registry=registry(provider),
            )
        )
        await asyncio.wait_for(provider.ready.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.active == 0
        manifests = list(tmp_path.glob(".dev-agent/runs/CANCEL/*/investigation.json"))
        assert json.loads(manifests[0].read_text())["status"] == "INTERRUPTED"

    asyncio.run(exercise())


def test_cli_invokes_investigation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".dev-agent.yaml").write_text("{}")
    (tmp_path / "ticket.md").write_text("Inspect CRUD")
    with (
        patch("dev_agent.cli.is_git_repository", return_value=True),
        patch("dev_agent.cli.ProviderRegistry", return_value=registry(fake())),
    ):
        result = CliRunner().invoke(app, ["investigate", "DEMO", "--file", "ticket.md"])
    assert result.exit_code == 0, result.output
    assert "Investigation complete" in result.output
    assert "Artifacts:" in result.output


def test_cli_failure_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".dev-agent.yaml").write_text("{}")
    (tmp_path / "ticket.md").write_text("Inspect CRUD")
    provider = fake()
    provider.responses["explorer"] = AgentResult(
        success=True,
        output="invalid",
        exit_code=0,
        duration_seconds=0,
    )
    with (
        patch("dev_agent.cli.is_git_repository", return_value=True),
        patch("dev_agent.cli.ProviderRegistry", return_value=registry(provider)),
    ):
        result = CliRunner().invoke(app, ["investigate", "DEMO", "--file", "ticket.md"])
    assert result.exit_code == 1
    assert "explorer" in result.output and "invalid JSON" in result.output
    assert "Investigation complete" not in result.output


def test_artifact_symlink_cannot_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".dev-agent").symlink_to(outside, target_is_directory=True)
    provider = fake()
    with pytest.raises(ValueError, match="inside the repository"):
        asyncio.run(
            investigate(
                ticket_id="DEMO", ticket="Inspect", repository=repo, registry=registry(provider)
            )
        )
    assert not provider.tasks and not list(outside.iterdir())
