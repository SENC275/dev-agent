import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ContextConfig, ProjectConfig
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
    "test_researcher": {"existing_tests": [], "fixtures": [], "recommended_tests": ["test change"]},
}


def setup_repo(path: Path) -> None:
    (path / "app.py").write_text("value = 1\n")
    for args in [
        ["init", "-q"],
        ["add", "app.py"],
        ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"],
    ]:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def provider() -> FakeProvider:
    return FakeProvider(
        {
            role: AgentResult(
                success=True, output=json.dumps(data), exit_code=0, duration_seconds=0
            )
            for role, data in PAYLOADS.items()
        }
    )


def registry(fake: FakeProvider, *, enabled: bool = True) -> ProviderRegistry:
    config = ProjectConfig(
        context=ContextConfig(enabled=enabled, investigation_mode="explorer_first")
    )
    return ProviderRegistry(config, {"codex_cli": lambda _: fake})


def test_explorer_handoff_then_parallel_specialists(tmp_path: Path) -> None:
    setup_repo(tmp_path)

    class Ordered(FakeProvider):
        def __init__(self) -> None:
            super().__init__(provider().responses)
            self.explored = False
            self.started: set[str] = set()
            self.ready = asyncio.Event()

        async def execute(self, task: AgentTask) -> AgentResult:
            if task.role == "explorer":
                assert not self.started
                self.explored = True
            else:
                assert self.explored
                assert '"origin": "exploration"' in task.prompt
                assert '"path": "app.py", "state": "unchanged"' in task.prompt
                assert "app.py:1" in task.prompt
                assert "Expand search whenever" in task.prompt
                self.started.add(task.role)
                if len(self.started) == 2:
                    self.ready.set()
                await asyncio.wait_for(self.ready.wait(), 2)
            return await super().execute(task)

    async def exercise() -> None:
        fake = Ordered()
        result = await investigate(
            ticket_id="ORDER", ticket="Change value", repository=tmp_path, registry=registry(fake)
        )
        assert result.success
        assert len(fake.tasks) == 3
        context = json.loads((result.directory / "context.json").read_text())
        assert context["origin"] == "investigation"
        assert set(context["source_artifact_hashes"]) == {
            "exploration.json",
            "patterns.json",
            "tests.json",
        }
        manifest = json.loads((result.directory / "investigation.json").read_text())
        assert manifest["investigation_mode"] == "explorer_first"
        usage = [json.loads(p.read_text()) for p in (result.directory / "usage").glob("*.json")]
        assert sum(r["context"]["used"] for r in usage) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure", ["invalid", "changed_source", "changed_ticket", "changed_artifact"]
)
def test_untrusted_handoff_falls_back(tmp_path: Path, failure: str) -> None:
    setup_repo(tmp_path)

    class Broken(FakeProvider):
        async def execute(self, task: AgentTask) -> AgentResult:
            if task.role == "explorer":
                if failure == "invalid":
                    return AgentResult(
                        success=True, output="bad JSON", exit_code=0, duration_seconds=0
                    )
                if failure == "changed_source":
                    (tmp_path / "app.py").write_text("value = 2\n")
            else:
                assert '"origin": "exploration"' not in task.prompt
            return await super().execute(task)

    # Mutate persisted evidence before the registry prepares specialist input.
    class MutatingRegistry(ProviderRegistry):
        async def execute(self, stage: str, directory: Path, task: AgentTask) -> AgentResult:
            if task.role != "explorer":
                if failure == "changed_ticket":
                    (directory / "ticket.md").write_text("different ticket")
                if failure == "changed_artifact":
                    (directory / "exploration.json").write_text('{"changed": true}')
            return await super().execute(stage, directory, task)

    fake = Broken(provider().responses)
    reg = MutatingRegistry(registry(fake).config, {"codex_cli": lambda _: fake})
    result = asyncio.run(
        investigate(ticket_id="FALLBACK", ticket="Change value", repository=tmp_path, registry=reg)
    )
    assert len(fake.tasks) == (2 if failure == "invalid" else 3)
    if failure == "invalid":
        assert set(result.errors) == {"explorer"}
    assert (result.directory / "patterns.json").exists()
    assert (result.directory / "tests.json").exists()
    if failure != "invalid":
        assert json.loads((result.directory / "context.json").read_text())["origin"] == "none"


def test_cancellation_preserves_explorer(tmp_path: Path) -> None:
    setup_repo(tmp_path)

    class Waiting(FakeProvider):
        def __init__(self) -> None:
            super().__init__(provider().responses)
            self.active = 0
            self.ready = asyncio.Event()

        async def execute(self, task: AgentTask) -> AgentResult:
            if task.role == "explorer":
                return await super().execute(task)
            self.active += 1
            if self.active == 2:
                self.ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.active -= 1
            raise AssertionError("unreachable")

    async def exercise() -> None:
        fake = Waiting()
        task = asyncio.create_task(
            investigate(
                ticket_id="CANCEL", ticket="Change", repository=tmp_path, registry=registry(fake)
            )
        )
        await asyncio.wait_for(fake.ready.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.active == 0
        manifest_path = next(tmp_path.glob(".dev-agent/runs/CANCEL/*/investigation.json"))
        manifest = json.loads(manifest_path.read_text())
        assert manifest["status"] == "INTERRUPTED"
        assert manifest["steps"][0]["artifact"] == "exploration.json"

    asyncio.run(exercise())


def test_disabling_context_restores_parallel(tmp_path: Path) -> None:
    class Barrier(FakeProvider):
        def __init__(self) -> None:
            super().__init__(provider().responses)
            self.started = 0
            self.ready = asyncio.Event()

        async def execute(self, task: AgentTask) -> AgentResult:
            assert "Keep research bounded" not in task.prompt
            self.started += 1
            if self.started == 3:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), 2)
            return await super().execute(task)

    async def exercise() -> None:
        fake = Barrier()
        result = await investigate(
            ticket_id="OFF",
            ticket="Change",
            repository=tmp_path,
            registry=registry(fake, enabled=False),
        )
        assert result.success
        assert (
            json.loads((result.directory / "investigation.json").read_text())["investigation_mode"]
            == "parallel"
        )

    asyncio.run(exercise())
