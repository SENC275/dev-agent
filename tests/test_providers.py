import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ROLES, ProjectConfig, ProviderConfig
from dev_agent.process import ProcessResult
from dev_agent.providers.codex_cli import CodexCLIProvider
from dev_agent.providers.fake import FakeProvider
from dev_agent.providers.registry import ProviderRegistry


class StubRunner:
    def __init__(self, output: bytes | None = b"answer", code: int = 0, timed_out: bool = False):
        self.output = output
        self.code = code
        self.timed_out = timed_out
        self.calls: list[tuple[tuple[str, ...], Path, str | None, float]] = []
        self.paths: list[Path] = []

    async def __call__(
        self, command: Sequence[str], *, cwd: Path, stdin: str | None = None, timeout: float = 60.0
    ) -> ProcessResult:
        self.calls.append((tuple(command), cwd, stdin, timeout))
        path = Path(command[command.index("--output-last-message") + 1])
        self.paths.append(path)
        if self.output is not None:
            path.write_bytes(self.output)
        return ProcessResult(
            tuple(command),
            cwd,
            datetime.now(UTC),
            0.1,
            self.code,
            "progress, not the answer",
            "diagnostic",
            self.timed_out,
        )


def task(path: Path, read_only: bool = True) -> AgentTask:
    return AgentTask(
        role="explorer",
        prompt="a prompt with ; $(echo text)",
        working_directory=path,
        read_only=read_only,
    )


@pytest.mark.parametrize("read_only, sandbox", [(True, "read-only"), (False, "workspace-write")])
def test_codex_command_and_fresh_calls(tmp_path: Path, read_only: bool, sandbox: str) -> None:
    runner = StubRunner()
    provider = CodexCLIProvider(
        ProviderConfig(model="configured-model", timeout_seconds=12), runner=runner
    )
    for _ in range(2):
        result = asyncio.run(provider.execute(task(tmp_path, read_only)))
        assert result.success and result.output == "answer"
        assert result.stderr == "diagnostic"
    argv, cwd, stdin, timeout = runner.calls[0]
    assert argv[:5] == ("codex", "--ask-for-approval", "never", "exec", "--ephemeral")
    assert argv[argv.index("--sandbox") + 1] == sandbox
    assert argv[argv.index("--model") + 1] == "configured-model"
    assert argv[-1] == "-"
    assert stdin == task(tmp_path).prompt and stdin not in argv
    assert cwd == tmp_path and timeout == 12
    assert runner.paths[0] != runner.paths[1]
    assert all(not path.parent.exists() for path in runner.paths)


@pytest.mark.parametrize(
    "output,code,timed_out,error",
    [
        (None, 0, False, "did not produce"),
        (b"  ", 0, False, "empty"),
        (b"\xff", 0, False, "UTF-8"),
        (b"partial", 7, False, "code 7"),
        (b"partial", 0, True, "timed out"),
    ],
)
def test_codex_failed_results(
    tmp_path: Path, output: bytes | None, code: int, timed_out: bool, error: str
) -> None:
    runner = StubRunner(output, code, timed_out)
    result = asyncio.run(CodexCLIProvider(ProviderConfig(), runner=runner).execute(task(tmp_path)))
    assert not result.success
    assert result.error is not None and error in result.error
    assert result.exit_code == code and result.timed_out == timed_out
    assert "--model" not in runner.calls[0][0]


@pytest.mark.parametrize("error", [FileNotFoundError("codex"), asyncio.CancelledError()])
def test_launch_errors_and_cancellation_propagate(tmp_path: Path, error: BaseException) -> None:
    runner = AsyncMock(side_effect=error)
    provider = CodexCLIProvider(ProviderConfig(), runner=runner)
    with pytest.raises(type(error)):
        asyncio.run(provider.execute(task(tmp_path)))
    argv = runner.call_args.args[0]
    path = Path(argv[argv.index("--output-last-message") + 1])
    assert not path.parent.exists()


def test_registry_and_fake(tmp_path: Path) -> None:
    response = AgentResult(success=True, output="evidence", exit_code=0, duration_seconds=0)
    fake = FakeProvider({role: response for role in ROLES})
    registry = ProviderRegistry(ProjectConfig(), {"codex_cli": lambda config: fake})
    assert all(registry.resolve(role) is fake for role in ROLES)
    result = asyncio.run(registry.resolve("explorer").execute(task(tmp_path)))
    assert result == response and result is not response
    assert fake.tasks == [task(tmp_path)]
    with pytest.raises(ValueError, match="Unknown role"):
        registry.resolve("unknown")
    with pytest.raises(ValueError, match="No factory"):
        ProviderRegistry(ProjectConfig(), {}).resolve("explorer")
    with pytest.raises(ValueError, match="No fake response"):
        asyncio.run(FakeProvider({}).execute(task(tmp_path)))


def test_named_provider_resolution() -> None:
    config = ProjectConfig.model_validate(
        {
            "providers": {"local": {"type": "codex_cli", "model": "chosen"}},
            "roles": {role: {"provider": "local"} for role in ROLES},
        }
    )
    provider = ProviderRegistry(config).resolve("reviewer")
    assert isinstance(provider, CodexCLIProvider)
    assert provider.config.model == "chosen"


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_bad_provider_timeout(value: float) -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(timeout_seconds=value)


def test_task_rejects_coerced_permission(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        AgentTask.model_validate(
            {
                "role": "explorer",
                "prompt": "inspect",
                "working_directory": tmp_path,
                "read_only": "false",
            }
        )
