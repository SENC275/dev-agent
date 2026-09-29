"""Opt-in live test: requires Claude Code login and consumes account usage."""

import asyncio
import os
import shutil
import subprocess

import pytest

from dev_agent.models.agent import AgentTask
from dev_agent.models.config import ProviderConfig
from dev_agent.providers.claude_cli import ClaudeCLIProvider


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("DEV_AGENT_RUN_CLAUDE_TEST") != "1",
    reason="Set DEV_AGENT_RUN_CLAUDE_TEST=1 to call real Claude Code",
)
def test_real_claude_read_and_edit(tmp_path):
    assert shutil.which("claude"), "Install and log in to Claude Code first"
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    marker = tmp_path / "example.txt"
    marker.write_text("before\n")
    provider = ClaudeCLIProvider(
        ProviderConfig(
            type="claude_cli",
            model=os.environ.get("DEV_AGENT_CLAUDE_MODEL"),
            timeout_seconds=180,
        )
    )
    readonly = asyncio.run(
        provider.execute(
            AgentTask(
                role="explorer",
                working_directory=tmp_path,
                prompt="Read example.txt and return exactly its contents. Do not edit any files.",
            )
        )
    )
    assert readonly.success, readonly.error
    assert readonly.output == "before" and marker.read_text() == "before\n"
    edited = asyncio.run(
        provider.execute(
            AgentTask(
                role="implementer",
                working_directory=tmp_path,
                read_only=False,
                prompt="Read example.txt. Replace its contents with after and a newline. "
                "Use Edit or Write, then finish with a brief summary.",
            )
        )
    )
    assert edited.success, edited.error
    assert marker.read_text() == "after\n"
