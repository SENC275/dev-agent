from pathlib import Path

import pytest
from pydantic import ValidationError

from dev_agent.config import ConfigurationError, load_config
from dev_agent.models.config import ROLES, ProjectConfig


def test_defaults() -> None:
    config = ProjectConfig()
    assert set(config.roles) == set(ROLES)
    assert all(role.provider == "codex" for role in config.roles.values())


@pytest.mark.parametrize(
    "contents", ["", "[]", "test: [", "unknown: true", "limits:\n  max_fix_cycles: -1"]
)
def test_invalid_configuration(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(contents)
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_undefined_provider() -> None:
    with pytest.raises(ValidationError, match="undefined provider"):
        ProjectConfig.model_validate({"providers": {"other": {"type": "codex_cli"}}})


def test_missing_roles() -> None:
    with pytest.raises(ValidationError, match="missing"):
        ProjectConfig.model_validate({"roles": {}})


def test_unsupported_provider() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate({"providers": {"codex": {"type": "unknown_provider"}}})


def test_secrets_not_echoed(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("providers:\n  codex:\n    api_key: secret-value\n")
    with pytest.raises(ConfigurationError) as error:
        load_config(path)
    assert "secret-value" not in str(error.value)


def test_example_loads() -> None:
    assert load_config(Path(".dev-agent.example.yaml")).roles["reviewer"].provider == "codex"
