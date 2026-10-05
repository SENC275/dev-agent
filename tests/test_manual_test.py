import shlex
import sys
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from dev_agent.cli import app

runner = CliRunner()


def configure(path: Path, command: str, timeout: float = 5) -> Path:
    config = path / ".dev-agent.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "commands": {"test": command, "lint": "missing-lint", "typecheck": "missing-types"},
                "validation": {"timeout_seconds": timeout},
            }
        )
    )
    return config


def python(code: str) -> str:
    return shlex.join([sys.executable, "-c", code])


def test_success_without_git_ticket_or_provider(tmp_path):
    configure(tmp_path, python('print("[literal] passed")'))
    with chdir(tmp_path), patch("dev_agent.cli.ProviderRegistry", side_effect=AssertionError):
        result = runner.invoke(app, ["test"])
    assert result.exit_code == 0, result.output
    assert "[literal] passed" in result.output and "Test passed" in result.output
    assert not (tmp_path / ".dev-agent").exists()


def test_real_failure_preserves_exit_and_both_streams(tmp_path):
    configure(
        tmp_path, python('import sys; print("OUT"); print("ERR", file=sys.stderr); sys.exit(7)')
    )
    with chdir(tmp_path):
        result = runner.invoke(app, ["test"])
    assert result.exit_code == 7
    assert "OUT" in result.stdout and "ERR" in result.stderr
    assert "Test failed" in result.output


def test_external_config_does_not_change_execution_directory(tmp_path):
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source.mkdir()
    worktree.mkdir()
    config = configure(
        source, python('from pathlib import Path; print(Path("marker").read_text())')
    )
    (source / "marker").write_text("wrong tree")
    (worktree / "marker").write_text("selected worktree")
    with chdir(worktree):
        result = runner.invoke(app, ["test", "--config", str(config)])
    assert result.exit_code == 0 and "selected worktree" in result.output
    assert "wrong tree" not in result.output


def test_timeout_preserves_partial_output(tmp_path):
    configure(tmp_path, python('import time; print("started", flush=True); time.sleep(10)'), 0.2)
    with chdir(tmp_path):
        result = runner.invoke(app, ["test"])
    assert result.exit_code == 124
    assert "started" in result.output and "timed out" in result.output


@pytest.mark.parametrize("command", [" ", "'unterminated", "no-such-dev-agent-test-executable"])
def test_invalid_commands_are_actionable(tmp_path, command):
    configure(tmp_path, command)
    with chdir(tmp_path):
        result = runner.invoke(app, ["test"])
    assert result.exit_code == 1 and "Test could not run" in result.output
    assert "Traceback" not in result.output


def test_missing_configuration(tmp_path):
    with chdir(tmp_path):
        result = runner.invoke(app, ["test"])
    assert result.exit_code == 1 and "dev-agent init" in result.output


def test_explicit_shell_chain_stops_on_failure(tmp_path):
    configure(tmp_path, shlex.join(["sh", "-c", "exit 9 && touch should-not-exist"]))
    with chdir(tmp_path):
        result = runner.invoke(app, ["test"])
    assert result.exit_code == 9
    assert not (tmp_path / "should-not-exist").exists()
