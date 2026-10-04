"""Load configuration without exposing raw configuration values in errors."""

from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import ValidationError

from dev_agent.local import protect_local_files
from dev_agent.models.config import ProjectConfig

CONFIG_NAME = ".dev-agent.yaml"


class ConfigurationError(Exception):
    """An actionable configuration failure."""


def load_config(path: Path) -> ProjectConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"{path.name} not found. Run 'dev-agent init'.") from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(
            f"Cannot read {path.name}; check permissions and UTF-8 encoding."
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"Invalid YAML in {path.name}; check indentation and syntax."
        ) from exc
    if not isinstance(data, dict):
        raise ConfigurationError(
            f"{path.name} must contain a YAML mapping (use {{}} for defaults)."
        )
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'configuration'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigurationError(f"Invalid configuration: {details}") from exc


def initialize_config(directory: Path) -> Path:
    protect_local_files(directory)
    path = directory / CONFIG_NAME
    template = files("dev_agent").joinpath("default.yaml").read_text(encoding="utf-8")
    with path.open("x", encoding="utf-8") as stream:
        stream.write(template)
    return path
