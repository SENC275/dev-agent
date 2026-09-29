from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from dev_agent.cli import app

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output
    assert "init" in result.output
    assert "start" in result.output
    assert "resume" in result.output
    assert "status" in result.output


def test_init_preserves_existing_file(tmp_path: Path) -> None:
    with chdir(tmp_path):
        assert runner.invoke(app, ["init"]).exit_code == 0
        path = Path(".dev-agent.yaml")
        path.write_text("# custom\n{}\n")
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 1
        assert path.read_text() == "# custom\n{}\n"


def test_doctor_ready(tmp_path: Path) -> None:
    with chdir(tmp_path):
        runner.invoke(app, ["init"])
        with (
            patch("dev_agent.doctor.shutil.which", return_value="/bin/tool"),
            patch("dev_agent.doctor.is_git_repository", return_value=True),
        ):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "Ready." in result.output
        assert "reviewer" in result.output


def test_missing_codex(tmp_path: Path) -> None:
    with chdir(tmp_path):
        runner.invoke(app, ["init"])
        with (
            patch(
                "dev_agent.doctor.shutil.which",
                side_effect=lambda cmd: "/bin/git" if cmd == "git" else None,
            ),
            patch("dev_agent.doctor.is_git_repository", return_value=True),
        ):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "Required by:" in result.output
        assert "explorer" in result.output
        assert "Ready." not in result.output


def test_missing_configuration(tmp_path: Path) -> None:
    with chdir(tmp_path):
        with patch("dev_agent.doctor.shutil.which", return_value=None):
            result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 1
        assert "dev-agent init" in result.output
        assert "Traceback" not in result.output
