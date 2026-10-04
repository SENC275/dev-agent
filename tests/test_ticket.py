import asyncio
import json
from contextlib import chdir
from unittest.mock import patch

import pytest
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.models.agent import AgentResult
from dev_agent.models.config import ProjectConfig
from dev_agent.models.ticket import TicketDraft
from dev_agent.providers.fake import FakeProvider
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.ticket import generate_ticket

DRAFT = dict(
    title="增加任务筛选",
    goal="按完成状态筛选任务",
    current_behavior="列表目前没有筛选参数。",
    acceptance_criteria=["省略筛选条件时保留当前行为", "筛选后再分页"],
    expected_files=["app/main.py"],
    out_of_scope=["认证"],
    assumptions=[],
    open_questions=["是否需要组合其他筛选条件？"],
)


def registry(output=None, success=True):
    provider = FakeProvider(
        {
            "planner": AgentResult(
                success=success,
                output=json.dumps(DRAFT) if output is None else output,
                exit_code=0 if success else 1,
                duration_seconds=0,
            )
        }
    )
    return provider, ProviderRegistry(ProjectConfig(), {"codex_cli": lambda _: provider})


def test_readonly_draft_with_authoritative_commands(repository):
    provider, providers = registry()
    path = asyncio.run(generate_ticket(repository, "DEMO-1", "增加筛选", providers))
    text = path.read_text()
    assert path == repository / ".dev-agent/tickets/DEMO-1.md"
    assert "增加筛选" in text and "## Open Questions" in text
    assert "test: `pytest`" in text
    assert "是否需要组合其他筛选条件" in text
    assert provider.tasks[0].read_only and provider.tasks[0].role == "planner"
    assert provider.tasks[0].output_schema == TicketDraft.model_json_schema()
    assert "Use the smallest safe changes" in provider.tasks[0].prompt
    assert not (repository / ".dev-agent/state.sqlite3").exists()
    assert (repository / "app.txt").read_text() == "baseline\n"


@pytest.mark.parametrize("mode", ["exists", "symlink", "bad_id", "empty", "large"])
def test_preflight_without_model_call(repository, tmp_path, mode):
    provider, providers = registry()
    ticket_id, description = "DEMO-1", "description"
    if mode == "exists":
        (repository / ".dev-agent/tickets").mkdir(parents=True)
        (repository / ".dev-agent/tickets/DEMO-1.md").write_text("keep")
    elif mode == "symlink":
        (repository / ".dev-agent").mkdir(exist_ok=True)
        (repository / ".dev-agent/tickets").symlink_to(tmp_path, target_is_directory=True)
    elif mode == "bad_id":
        ticket_id = "../escape"
    elif mode == "empty":
        description = " "
    else:
        description = "x" * 32001
    with pytest.raises(ValueError):
        asyncio.run(generate_ticket(repository, ticket_id, description, providers))
    assert not provider.tasks


@pytest.mark.parametrize("output,success", [("{}", True), ("not JSON", True), ("", False)])
def test_no_draft_on_invalid_or_failed_result(repository, output, success):
    _, providers = registry(output, success)
    with pytest.raises(ValueError):
        asyncio.run(generate_ticket(repository, "DEMO-1", "description", providers))
    assert not (repository / ".dev-agent/tickets/DEMO-1.md").exists()


def test_readonly_violation_detected(repository):
    provider, providers = registry()
    original = provider.execute

    async def changed(task):
        (repository / "app.txt").write_text("unexpected change")
        return await original(task)

    with patch.object(provider, "execute", side_effect=changed):
        with pytest.raises(ValueError, match="Repository changed"):
            asyncio.run(generate_ticket(repository, "DEMO-1", "description", providers))
    assert not (repository / ".dev-agent/tickets/DEMO-1.md").exists()


def test_concurrent_output_is_never_overwritten(repository):
    provider, providers = registry()
    original = provider.execute

    async def changed(task):
        (repository / ".dev-agent/tickets").mkdir(parents=True)
        (repository / ".dev-agent/tickets/DEMO-1.md").write_text("human draft")
        return await original(task)

    with patch.object(provider, "execute", side_effect=changed):
        with pytest.raises(ValueError):
            asyncio.run(generate_ticket(repository, "DEMO-1", "description", providers))
    assert (repository / ".dev-agent/tickets/DEMO-1.md").read_text() == "human draft"


def test_cli_file_input_and_mutually_exclusive_options(repository, tmp_path):
    description = tmp_path / "request.txt"
    description.write_text("增加筛选")
    provider, providers = registry()
    with chdir(repository), patch("dev_agent.cli.ProviderRegistry", return_value=providers):
        runner = CliRunner()
        bad = runner.invoke(app, ["ticket", "DEMO-1", "-d", "other", "--file", str(description)])
        assert bad.exit_code == 1 and not provider.tasks
        missing = runner.invoke(app, ["ticket", "DEMO-1"])
        assert missing.exit_code == 1 and not provider.tasks
        result = runner.invoke(app, ["ticket", "DEMO-1", "--file", str(description)])
        assert result.exit_code == 0, result.output
        assert "dev-agent start DEMO-1" in result.output
        duplicate = runner.invoke(app, ["ticket", "DEMO-1", "-d", "another"])
        assert duplicate.exit_code == 1 and len(provider.tasks) == 1
