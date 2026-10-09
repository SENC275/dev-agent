import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from test_claude_provider import Runner
from test_providers import task

from dev_agent.diagnostics import failure_details, redact
from dev_agent.models.agent import AgentResult
from dev_agent.models.config import ProviderConfig
from dev_agent.providers.claude_cli import ClaudeCLIProvider
from dev_agent.usage import record_call


def result(**kwargs):
    return AgentResult(success=False, output="", exit_code=1, duration_seconds=1, **kwargs)


@pytest.mark.parametrize("secret", ["sk-example-secret", "ghp_example123", "xyzzy"])
def test_redaction_before_truncation(secret):
    text = f'Authorization: Bearer {secret}\napi_key="{secret}"\npassword={secret}\n'
    assert secret not in redact(text)
    assert len(redact("a" * 10000)) < 4100
    assert "private" not in redact("-----BEGIN PRIVATE KEY-----private")


def test_classification():
    assert failure_details(result(timed_out=True))["kind"] == "timeout"
    assert failure_details(result())["kind"] == "provider_execution"
    r = result().model_copy(update={"exit_code": 0, "error": "Invalid JSON"})
    assert failure_details(r)["kind"] == "model_output"


def test_claude_nonzero_retains_failure_fields_only(tmp_path):
    runner = Runner({"type": "result", "subtype": "error_max_turns",
                     "errors": ["api_key=xyzzy"], "result": "PRIVATE RESPONSE"}, code=1)
    r = asyncio.run(ClaudeCLIProvider(ProviderConfig(type="claude_cli"),
                                     runner=runner).execute(task(tmp_path)))
    assert not r.success and r.exit_code == 1
    assert "error_max_turns" in r.error
    assert "xyzzy" not in r.error and "PRIVATE RESPONSE" not in r.error


def test_failure_persisted_and_exception(tmp_path):
    provider = AsyncMock()
    provider.execute.return_value = result(error="api_key=xyzzy", stderr="password=hidden")
    asyncio.run(record_call(provider, task(tmp_path), tmp_path, "plan_review", "claude",
                            "claude_cli", None))
    records = [json.loads(p.read_text()) for p in (tmp_path / "usage").glob("*.json")]
    assert records[0]["diagnostic"]["kind"] == "provider_execution"
    assert "xyzzy" not in json.dumps(records) and "hidden" not in json.dumps(records)
    provider.execute.side_effect = OSError("token=secret-value")
    with pytest.raises(OSError):
        asyncio.run(record_call(provider, task(tmp_path), tmp_path, "plan_review", "claude",
                                "claude_cli", None))
    records = [json.loads(p.read_text()) for p in (tmp_path / "usage").glob("*.json")]
    assert any(r.get("diagnostic", {}).get("kind") == "provider_exception" for r in records)
    assert "secret-value" not in json.dumps(records)


def test_status_shows_failure_and_resume(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from dev_agent.cli import app
    from dev_agent.models.config import ProjectConfig
    from dev_agent.workflow.persistence import Journal

    monkeypatch.chdir(tmp_path)
    journal = Journal(tmp_path)
    journal.create("DIAG-1", "Test", ProjectConfig().model_dump_json())
    provider = AsyncMock()
    provider.execute.return_value = result(error="service unavailable", stderr="token=secret")
    asyncio.run(record_call(provider, task(tmp_path), tmp_path / ".dev-agent/runs/DIAG-1/run",
                            "plan_review", "claude", "claude_cli", None))
    output = CliRunner().invoke(app, ["status", "DIAG-1"])
    assert output.exit_code == 0, output.output
    assert "provider_execution" in output.output
    assert "service unavailable" in output.output
    assert "dev-agent resume DIAG-1" in output.output
    assert "secret" not in output.output
