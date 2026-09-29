"""Opt-in LAN/local inference test; all file edits stay in a temporary repository."""

import asyncio
import json
import os
import subprocess

import pytest

from dev_agent.artifacts import parse_artifact
from dev_agent.models.agent import AgentTask
from dev_agent.models.config import ProviderConfig
from dev_agent.models.fix import FixReport
from dev_agent.providers.ollama import OllamaProvider


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("DEV_AGENT_OLLAMA_URL"),
    reason="Set DEV_AGENT_OLLAMA_URL to call a real Ollama server",
)
def test_real_ollama_edit_and_structured_report(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    source = tmp_path / "app.py"
    source.write_text("def add(a, b):\n    return a - b\n")
    provider = OllamaProvider(
        ProviderConfig(
            type="ollama",
            base_url=os.environ["DEV_AGENT_OLLAMA_URL"],
            model=os.environ.get("DEV_AGENT_OLLAMA_MODEL", "qwen2.5-coder:7b-instruct"),
            timeout_seconds=180,
        )
    )
    result = asyncio.run(
        provider.execute(
            AgentTask(
                role="fixer",
                working_directory=tmp_path,
                read_only=False,
                output_schema=FixReport.model_json_schema(),
                prompt="Read app.py. Fix add to return a + b instead of a - b. Save the file. "
                "No review findings are supplied: report decisions=[] with a truthful summary. "
                "Do not claim validation ran. Report schema: "
                + json.dumps(FixReport.model_json_schema()),
            )
        )
    )
    assert result.success, result.error
    report = parse_artifact(result.output, FixReport)
    assert report.decisions == [] and report.summary
    subprocess.run(
        ["python3", "-c", "from app import add; assert add(2, 3) == 5; assert add(-2, 3) == 1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
