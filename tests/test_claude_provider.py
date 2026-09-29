import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from test_providers import task

from dev_agent.doctor import check_environment
from dev_agent.models.config import ROLES, ProjectConfig, ProviderConfig, RoleConfig
from dev_agent.process import ProcessResult
from dev_agent.providers.claude_cli import ClaudeCLIProvider
from dev_agent.providers.registry import ProviderRegistry


class Runner:
    def __init__(self, payload=None, code=0, timed_out=False):
        self.payload = (
            payload
            if payload is not None
            else {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": '{"findings":[]}',
                "permission_denials": [],
            }
        )
        self.code, self.timed_out, self.calls = code, timed_out, []

    async def __call__(self, command, *, cwd, stdin=None, timeout=60):
        self.calls.append((command, cwd, stdin, timeout))
        return ProcessResult(
            tuple(command),
            cwd,
            datetime.now(UTC),
            0.1,
            self.code,
            json.dumps(self.payload),
            "diagnostic",
            self.timed_out,
        )


@pytest.mark.parametrize("readonly", [True, False])
def test_claude_restricted_session_and_exact_result(tmp_path, readonly):
    runner = Runner()
    config = ProviderConfig(type="claude_cli", model="sonnet", max_steps=32, timeout_seconds=90)
    provider = ClaudeCLIProvider(config, runner=runner)
    result = asyncio.run(provider.execute(task(tmp_path, readonly)))
    assert result.success and result.output == '{"findings":[]}'
    argv, cwd, stdin, timeout = runner.calls[0]
    assert cwd == tmp_path and stdin == task(tmp_path).prompt and stdin not in argv
    assert timeout == 90
    assert "--restricted" in argv and "--safe-mode" in argv
    assert "--no-session-persistence" in argv and "--continue" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--max-turns") + 1] == "32"
    assert argv[argv.index("--model") + 1] == "sonnet"
    allowed_tools = argv[argv.index("--tools") + 1].split(",")
    assert allowed_tools == (
        ["Read", "Glob", "Grep"] if readonly else ["Read", "Glob", "Grep", "Edit", "Write"]
    )
    assert "mcp__*" in argv[argv.index("--disallowedTools") + 1]
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["disableAllHooks"]
    assert "Write(./.git/**)" in settings["permissions"]["deny"]


@pytest.mark.parametrize(
    "payload,code,timed_out",
    [
        ([], 0, False),
        ({"type": "assistant", "result": "partial"}, 0, False),
        ({"type": "result", "subtype": "error_max_turns", "is_error": True}, 0, False),
        ({"type": "result", "subtype": "success", "is_error": False, "result": ""}, 0, False),
        (
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "permission_denials": [{"tool_name": "Edit"}],
            },
            0,
            False,
        ),
        ({}, 1, False),
        ({}, 0, True),
    ],
)
def test_fail_closed_on_incomplete_error_or_denied_results(tmp_path, payload, code, timed_out):
    runner = Runner(payload, code, timed_out)
    result = asyncio.run(
        ClaudeCLIProvider(ProviderConfig(type="claude_cli"), runner=runner).execute(task(tmp_path))
    )
    assert not result.success and result.error and result.output == ""
    assert result.timed_out == timed_out


@pytest.mark.parametrize("error", [FileNotFoundError("claude"), asyncio.CancelledError()])
def test_launch_failure_and_cancellation_propagate(tmp_path, error):
    provider = ClaudeCLIProvider(
        ProviderConfig(type="claude_cli"), runner=AsyncMock(side_effect=error)
    )
    with pytest.raises(type(error)):
        asyncio.run(provider.execute(task(tmp_path)))


@pytest.mark.parametrize("installed", [True, False])
def test_doctor_and_role_routing(tmp_path, installed):
    config = ProjectConfig(
        providers={"claude": ProviderConfig(type="claude_cli")},
        roles={role: RoleConfig(provider="claude") for role in ROLES},
    )
    (tmp_path / ".dev-agent.yaml").write_text(config.model_dump_json())
    assert isinstance(ProviderRegistry(config).resolve("reviewer"), ClaudeCLIProvider)
    with (
        patch(
            "dev_agent.doctor.shutil.which",
            side_effect=lambda cmd: "/bin/tool" if cmd != "claude" or installed else None,
        ),
        patch("dev_agent.doctor.is_git_repository", return_value=True),
    ):
        checks = check_environment(tmp_path)
    check = next(c for c in checks if c.name == "claude")
    assert check.passed == installed
    assert all(c.passed == installed for c in checks if c.section == "Roles")


def test_structured_output_uses_schema_instead_of_prose(tmp_path):
    from dev_agent.artifacts import parse_artifact
    from dev_agent.models.finding import Review

    runner = Runner(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Here is my report.",
            "structured_output": {"findings": []},
        }
    )
    request = task(tmp_path).model_copy(update={"output_schema": Review.model_json_schema()})
    result = asyncio.run(
        ClaudeCLIProvider(ProviderConfig(type="claude_cli"), runner=runner).execute(request)
    )
    assert result.success and parse_artifact(result.output, Review).findings == []
    argv = runner.calls[0][0]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == Review.model_json_schema()


def test_missing_structured_output_is_failure(tmp_path):
    runner = Runner()
    request = task(tmp_path).model_copy(update={"output_schema": {"type": "object"}})
    result = asyncio.run(
        ClaudeCLIProvider(ProviderConfig(type="claude_cli"), runner=runner).execute(request)
    )
    assert not result.success and "structured output" in result.error
