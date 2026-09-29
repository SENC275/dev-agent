"""Opt-in only: uses existing authentication and consumes provider usage."""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dev_agent.models.agent import AgentTask
from dev_agent.models.config import ProviderConfig
from dev_agent.providers.codex_cli import CodexCLIProvider


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("DEV_AGENT_RUN_CODEX_TEST") != "1",
    reason="Set DEV_AGENT_RUN_CODEX_TEST=1 to call real Codex",
)
def test_real_codex(tmp_path: Path) -> None:
    assert shutil.which("codex"), "Codex CLI must be on PATH"
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    marker = tmp_path / "example.txt"
    marker.write_text("unchanged\n")
    provider = CodexCLIProvider(
        ProviderConfig(
            model=os.environ.get("DEV_AGENT_CODEX_MODEL"),
            timeout_seconds=120,
        )
    )
    result = asyncio.run(
        provider.execute(
            AgentTask(
                role="explorer",
                working_directory=tmp_path,
                prompt="Reply with exactly DEV_AGENT_OK. Do not use tools or modify any files.",
            )
        )
    )
    assert result.success, result.error
    assert result.output.strip() == "DEV_AGENT_OK"
    assert marker.read_text() == "unchanged\n"
