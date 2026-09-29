import asyncio
import json
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from test_implementation import WritingProvider, registry
from test_implementation import prepared as prepared
from test_worktree import approved_plan as approved_plan
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.models.config import CommandsConfig, ProjectConfig, ValidationConfig
from dev_agent.validation.runner import validate
from dev_agent.workflow.implementation import implement


def command(code: str) -> str:
    return shlex.join([sys.executable, "-c", code])


def commands(test: str | None = None) -> CommandsConfig:
    return CommandsConfig(
        test=test or command("print('test output')"),
        lint=command("print('lint output')"),
        typecheck=command("print('type output')"),
    )


@pytest.fixture
def implemented(prepared: tuple[Path, Path, Path]) -> tuple[Path, Path, Path]:
    directory, _, _ = prepared
    asyncio.run(implement(directory, registry(WritingProvider())))
    return prepared


def test_pass_records_real_outputs_and_preserves_history(
    implemented: tuple[Path, Path, Path],
) -> None:
    directory, target, source = implemented
    checks = commands(command("import os,sys; print(os.getcwd()); print('err', file=sys.stderr)"))
    report = asyncio.run(validate(directory, checks, ValidationConfig()))
    assert report.success
    assert [item.name for item in report.commands] == ["test", "lint", "typecheck"]
    assert report.commands[0].stdout == str(target) + "\n"
    assert report.commands[0].stderr == "err\n"
    for item in report.commands:
        assert item.status == "PASSED" and item.exit_code == 0
        assert item.started_at and item.duration_seconds is not None
        assert item.cwd == str(target)
    assert (source / "app.txt").read_text() == "baseline\n"
    first = (report.directory / "validation.json").read_bytes()
    second = asyncio.run(validate(directory, commands(), ValidationConfig()))
    assert second.directory != report.directory
    assert (report.directory / "validation.json").read_bytes() == first
    latest = json.loads((directory / "validation.json").read_text())
    assert latest["run_id"] == second.run_id and latest["status"] == "PASSED"
    assert not (directory / "validation.lock").exists()


@pytest.mark.parametrize("mode", ["exit", "missing", "timeout"])
def test_failed_checks_stop_remaining_commands(
    implemented: tuple[Path, Path, Path], mode: str
) -> None:
    directory, target, _ = implemented
    code = {
        "exit": command("import sys; print('failure detail'); sys.exit(7)"),
        "missing": str(target / "missing-executable"),
        "timeout": command("import time; print('started', flush=True); time.sleep(60)"),
    }[mode]
    checks = commands(code)
    checks.lint = command("from pathlib import Path; Path('must-not-run').touch()")
    report = asyncio.run(validate(directory, checks, ValidationConfig(timeout_seconds=0.5)))
    assert not report.success
    assert [item.status for item in report.commands] == ["FAILED", "NOT_RUN", "NOT_RUN"]
    assert not (target / "must-not-run").exists()
    if mode == "exit":
        assert report.commands[0].exit_code == 7
        assert "failure detail" in report.commands[0].stdout
    elif mode == "missing":
        assert report.commands[0].exit_code is None
        assert "FileNotFoundError" in (report.commands[0].error or "")
    else:
        assert report.commands[0].timed_out
        assert report.commands[0].stdout == "started\n"
    assert json.loads((directory / "validation.json").read_text())["status"] == "FAILED"


def test_lint_failure_keeps_passed_test(implemented: tuple[Path, Path, Path]) -> None:
    directory, _, _ = implemented
    checks = commands()
    checks.lint = command("raise SystemExit(3)")
    report = asyncio.run(validate(directory, checks, ValidationConfig()))
    assert [item.status for item in report.commands] == ["PASSED", "FAILED", "NOT_RUN"]


@pytest.mark.parametrize(
    "case", ["approval", "implementation", "identity", "lock", "blank", "quote"]
)
def test_preflight_does_not_execute(implemented: tuple[Path, Path, Path], case: str) -> None:
    directory, _, _ = implemented
    checks = commands()
    if case == "approval":
        (directory / "approval.json").unlink()
    elif case in ("implementation", "identity"):
        path = directory / "implementation.json"
        data = json.loads(path.read_text())
        data["status" if case == "implementation" else "branch"] = "wrong"
        path.write_text(json.dumps(data))
    elif case == "lock":
        (directory / "validation.lock").write_text("busy")
    else:
        checks.lint = "  " if case == "blank" else "'unterminated"
    with patch("dev_agent.validation.runner.run_process", new_callable=AsyncMock) as runner:
        with pytest.raises((ValueError, OSError)):
            asyncio.run(validate(directory, checks, ValidationConfig()))
        runner.assert_not_called()


def test_cancel_records_interrupted_and_releases_lock(implemented: tuple[Path, Path, Path]) -> None:
    directory, _, _ = implemented
    with patch(
        "dev_agent.validation.runner.run_process",
        new_callable=AsyncMock,
        side_effect=asyncio.CancelledError,
    ):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(validate(directory, commands(), ValidationConfig()))
    record = json.loads((directory / "validation.json").read_text())
    assert record["status"] == "INTERRUPTED"
    assert record["commands"][0]["status"] == "INTERRUPTED"
    assert not (directory / "validation.lock").exists()


def test_shell_metacharacters_are_literal(implemented: tuple[Path, Path, Path]) -> None:
    directory, target, _ = implemented
    argv = [sys.executable, "-c", "import sys; print(sys.argv[1])", "$(touch unexpected); *"]
    report = asyncio.run(validate(directory, commands(shlex.join(argv)), ValidationConfig()))
    assert report.success
    assert report.commands[0].stdout == "$(touch unexpected); *\n"
    assert not (target / "unexpected").exists()


@pytest.mark.parametrize("succeed", [True, False])
def test_cli_reports_exit_code(implemented: tuple[Path, Path, Path], succeed: bool) -> None:
    directory, _, _ = implemented
    config = ProjectConfig(
        commands=commands(command("raise SystemExit(0)" if succeed else "raise SystemExit(9)"))
    )
    with patch("dev_agent.cli.load_config", return_value=config):
        result = CliRunner().invoke(app, ["validate", str(directory)])
    assert result.exit_code == (0 if succeed else 1), result.output
    assert ("Validation passed" in result.output) == succeed


def test_failed_rerun_replaces_latest_pass(implemented: tuple[Path, Path, Path]) -> None:
    directory, _, _ = implemented
    first = asyncio.run(validate(directory, commands(), ValidationConfig()))
    assert first.success
    second = asyncio.run(
        validate(directory, commands(command("raise SystemExit(1)")), ValidationConfig())
    )
    assert not second.success
    assert json.loads((directory / "validation.json").read_text())["status"] == "FAILED"
    assert json.loads((first.directory / "validation.json").read_text())["status"] == "PASSED"


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_timeout_configuration_rejects_invalid_values(value: float) -> None:
    with pytest.raises(ValueError):
        ValidationConfig(timeout_seconds=value)


def test_source_mutation_stops_before_next_check(implemented: tuple[Path, Path, Path]) -> None:
    directory, target, source = implemented
    checks = commands(
        command(
            f'from pathlib import Path; Path({str(source / "app.txt")!r}).write_text("changed")'
        )
    )
    checks.lint = command('from pathlib import Path; Path("must-not-run").touch()')
    with pytest.raises(ValueError, match="Source checkout changed"):
        asyncio.run(validate(directory, checks, ValidationConfig()))
    assert not (target / "must-not-run").exists()
    assert json.loads((directory / "validation.json").read_text())["status"] == "FAILED"
