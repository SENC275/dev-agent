import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from test_focused_investigation import PAYLOADS, setup_repo

from dev_agent.doctor import check_environment
from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ContextConfig, ProjectConfig, ProviderConfig, RoleConfig
from dev_agent.providers.fake import FakeProvider
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.investigation import investigate
from dev_agent.workflow.planning import HEADINGS, generate_plan


def combined() -> dict[str, object]:
    return {
        "exploration": PAYLOADS["explorer"],
        "patterns": PAYLOADS["pattern_researcher"],
        "tests": PAYLOADS["test_researcher"],
    }


def config() -> ProjectConfig:
    return ProjectConfig(context=ContextConfig(investigation_mode="single_pass"))


def registry(fake: FakeProvider) -> ProviderRegistry:
    return ProviderRegistry(config(), {"codex_cli": lambda _: fake})


def response(output: str) -> AgentResult:
    return AgentResult(success=True, output=output, exit_code=0, duration_seconds=0)


def test_one_call_three_reports_and_separate_planner(tmp_path: Path) -> None:
    setup_repo(tmp_path)
    fake = FakeProvider(
        {
            "explorer": response(json.dumps(combined())),
            "planner": response("\n\n".join(f"## {h}\nInspect app.py." for h in HEADINGS)),
        }
    )

    async def exercise() -> None:
        reg = registry(fake)
        run = await investigate(
            ticket_id="ONE", ticket="Change value", repository=tmp_path, registry=reg
        )
        assert run.success
        assert len(fake.tasks) == 1
        task = fake.tasks[0]
        assert task.role == "explorer" and task.read_only
        assert task.output_schema is not None
        assert set(task.output_schema["required"]) == {"exploration", "patterns", "tests"}
        assert "not independent reviews" in task.prompt
        assert "do not edit files" in task.prompt
        manifest = json.loads((run.directory / "investigation.json").read_text())
        assert manifest["investigation_mode"] == "single_pass"
        assert len(manifest["steps"]) == 1
        assert set(manifest["report_producers"].values()) == {"explorer"}
        assert (run.directory / "combined-investigation.json").is_file()
        for name in ["exploration.json", "patterns.json", "tests.json"]:
            assert (run.directory / name).is_file()
        context = json.loads((run.directory / "context.json").read_text())
        assert set(context["source_artifact_hashes"]) == {
            "exploration.json",
            "patterns.json",
            "tests.json",
        }
        assert len(list((run.directory / "usage").glob("*.json"))) == 1
        plan = await generate_plan(run.directory, reg)
        assert (plan / "plan.md").is_file()
        assert [t.role for t in fake.tasks] == ["explorer", "planner"]
        assert all(
            name in fake.tasks[1].prompt
            for name in ['"exploration.json"', '"patterns.json"', '"tests.json"']
        )

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["invalid", "missing", "nested", "timeout", "exit", "launch"])
def test_invalid_combined_output_does_not_publish_partial_or_fallback(
    tmp_path: Path, failure: str
) -> None:
    data = combined()
    if failure == "missing":
        del data["tests"]
    if failure == "nested":
        data["patterns"] = {"patterns": [{"name": "missing evidence"}]}
    result = response("not JSON" if failure == "invalid" else json.dumps(data))
    if failure == "timeout":
        result = result.model_copy(update={"timed_out": True})
    if failure == "exit":
        result = result.model_copy(update={"exit_code": 1})

    class Failing(FakeProvider):
        async def execute(self, task: AgentTask) -> AgentResult:
            if failure == "launch":
                self.tasks.append(task)
                raise FileNotFoundError("provider")
            return await super().execute(task)

    fake = Failing({"explorer": result})
    run = asyncio.run(
        investigate(ticket_id="BAD", ticket="Change", repository=tmp_path, registry=registry(fake))
    )
    assert len(fake.tasks) == 1
    assert set(run.errors) == {"explorer"}
    for name in ["combined-investigation.json", "exploration.json", "patterns.json", "tests.json"]:
        assert not (run.directory / name).exists()
    assert json.loads((run.directory / "investigation.json").read_text())["status"] == "FAILED"
    assert len(list((run.directory / "usage").glob("*.json"))) == 1


def test_unused_specialist_providers_not_resolved(tmp_path: Path) -> None:
    cfg = config()
    cfg.providers["other"] = ProviderConfig(type="claude_cli")
    for role in ["pattern_researcher", "test_researcher"]:
        cfg.roles[role] = RoleConfig(provider="other")
    fake = FakeProvider({"explorer": response(json.dumps(combined()))})
    reg = ProviderRegistry(cfg, {"codex_cli": lambda _: fake})
    run = asyncio.run(
        investigate(ticket_id="UNUSED", ticket="Change", repository=tmp_path, registry=reg)
    )
    assert run.success and len(fake.tasks) == 1
    (tmp_path / ".dev-agent.yaml").write_text(json.dumps(cfg.model_dump()))
    with (
        patch(
            "dev_agent.doctor.shutil.which",
            side_effect=lambda name: None if name == "claude" else "/bin/tool",
        ),
        patch("dev_agent.doctor.is_git_repository", return_value=True),
    ):
        checks = check_environment(tmp_path)
    assert not next(c for c in checks if c.name == "other").required
    for role in ["pattern_researcher", "test_researcher"]:
        check = next(c for c in checks if c.section == "Roles" and c.name == role)
        assert check.passed and not check.required and "single_pass" in check.detail


def test_cancel_single_call_does_not_publish_reports(tmp_path: Path) -> None:
    class Waiting(FakeProvider):
        def __init__(self) -> None:
            super().__init__({})
            self.ready = asyncio.Event()
            self.active = False

        async def execute(self, task: AgentTask) -> AgentResult:
            self.active = True
            self.ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.active = False
            raise AssertionError("unreachable")

    async def exercise() -> None:
        fake = Waiting()
        pending = asyncio.create_task(
            investigate(
                ticket_id="CANCEL", ticket="Change", repository=tmp_path, registry=registry(fake)
            )
        )
        await asyncio.wait_for(fake.ready.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not fake.active
        manifest = next(tmp_path.glob(".dev-agent/runs/CANCEL/*/investigation.json"))
        assert json.loads(manifest.read_text())["status"] == "INTERRUPTED"
        assert not (manifest.parent / "tests.json").exists()

    asyncio.run(exercise())


def test_disabled_context_restores_three_roles(tmp_path: Path) -> None:
    cfg = config()
    cfg.context.enabled = False
    fake = FakeProvider({role: response(json.dumps(data)) for role, data in PAYLOADS.items()})
    reg = ProviderRegistry(cfg, {"codex_cli": lambda _: fake})
    run = asyncio.run(
        investigate(ticket_id="OFF", ticket="Change", repository=tmp_path, registry=reg)
    )
    assert run.success
    assert {task.role for task in fake.tasks} == set(PAYLOADS)
    assert (
        json.loads((run.directory / "investigation.json").read_text())["investigation_mode"]
        == "parallel"
    )
