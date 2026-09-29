import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dev_agent.doctor import check_environment, is_git_repository


@pytest.mark.parametrize("error", [OSError("failed"), TimeoutError("git")])
def test_git_failure_is_reported(tmp_path: Path, error: Exception) -> None:
    (tmp_path / ".dev-agent.yaml").write_text("{}")
    with (
        patch("dev_agent.doctor.shutil.which", return_value="/bin/tool"),
        patch("dev_agent.doctor.is_git_repository", side_effect=error),
    ):
        checks = check_environment(tmp_path)
    assert any(check.name == "Git repository" and not check.passed for check in checks)


def test_real_git_repository(tmp_path: Path) -> None:
    assert not is_git_repository(tmp_path)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    assert is_git_repository(tmp_path)


def test_unused_provider_not_required(tmp_path: Path) -> None:
    (tmp_path / ".dev-agent.yaml").write_text("providers:\n  codex: {}\n  optional: {}\n")
    with (
        patch("dev_agent.doctor.shutil.which", return_value=None),
        patch("dev_agent.doctor.is_git_repository", return_value=False),
    ):
        checks = check_environment(tmp_path)
    optional = next(check for check in checks if check.name == "optional")
    assert not optional.required
