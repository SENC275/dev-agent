"""Read-only environment checks; no provider sessions are started."""

import asyncio
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from dev_agent.config import CONFIG_NAME, ConfigurationError, load_config
from dev_agent.process import run_process


@dataclass(frozen=True)
class Check:
    section: str
    name: str
    passed: bool
    detail: str
    required: bool = True


def is_git_repository(directory: Path) -> bool:
    result = asyncio.run(
        run_process(["git", "rev-parse", "--is-inside-work-tree"], cwd=directory, timeout=10)
    )
    return not result.timed_out and result.exit_code == 0 and result.stdout.strip() == "true"


def check_environment(directory: Path) -> list[Check]:
    checks = [
        Check(
            "Environment",
            "Python >= 3.12",
            sys.version_info >= (3, 12),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    ]
    git_available = shutil.which("git") is not None
    checks.append(
        Check(
            "Environment",
            "Git",
            git_available,
            "available" if git_available else "Install Git and add it to PATH.",
        )
    )
    try:
        repository = git_available and is_git_repository(directory)
        repository_detail = str(directory) if repository else "Run inside a Git working tree."
    except (OSError, TimeoutError):
        repository = False
        repository_detail = "Git check failed or timed out; verify Git works in this directory."
    checks.append(Check("Environment", "Git repository", repository, repository_detail))
    try:
        config = load_config(directory / CONFIG_NAME)
    except ConfigurationError as exc:
        checks.append(Check("Environment", "Configuration", False, str(exc)))
        return checks
    checks.append(Check("Environment", "Configuration", True, CONFIG_NAME))
    available: dict[str, bool] = {}
    for name in config.providers:
        users = [role for role, value in config.roles.items() if value.provider == name]
        provider = config.providers[name]
        if provider.type == "ollama":
            if not users:
                available[name], detail = True, "Not checked (unused Ollama provider)."
            else:
                try:
                    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                        response = client.get(provider.base_url.rstrip("/") + "/api/tags")
                        response.raise_for_status()
                        models = {item["name"] for item in response.json()["models"]}
                    available[name] = (
                        provider.model in models or f"{provider.model}:latest" in models
                    )
                    detail = (
                        f"Ollama reachable; model {provider.model} available."
                        if available[name]
                        else f"Model missing; pull {provider.model} on the Ollama server."
                    )
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    available[name], detail = (
                        False,
                        "Cannot query Ollama; check base_url and server/firewall.",
                    )
        elif provider.type == "claude_cli":
            available[name] = shutil.which("claude") is not None
            detail = (
                "Claude CLI available; login/model access not checked. Run claude auth status."
                if available[name]
                else "Claude CLI unavailable; install Claude Code and add claude to PATH."
            )
        else:
            available[name] = shutil.which("codex") is not None
            detail = "available" if available[name] else "Codex CLI unavailable; add codex to PATH."
        if users and not available[name]:
            detail += f" Required by: {', '.join(users)}."
        if not users:
            detail += " Optional: no roles use this provider."
        checks.append(Check("Providers", name, available[name], detail, required=bool(users)))
    for role, value in config.roles.items():
        checks.append(Check("Roles", role, available[value.provider], f"→ {value.provider}"))
    return checks
