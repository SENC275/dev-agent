import asyncio
import json
from contextlib import chdir
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.models.agent import AgentResult, AgentTask
from dev_agent.models.config import ProjectConfig
from dev_agent.models.usage import TokenUsage, claude_usage, codex_usage, combine, ollama_usage
from dev_agent.providers.fake import FakeProvider
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.usage import record_call, usage_report


def test_claude_cache_input_normalization_and_model_totals_not_double_counted():
    payload = {
        "type": "result",
        "usage": {"input_tokens": 999999},
        "modelUsage": {
            "model-a": {
                "inputTokens": 20,
                "outputTokens": 10,
                "cacheReadInputTokens": 80,
                "cacheCreationInputTokens": 30,
            },
            "model-b": {
                "inputTokens": 5,
                "outputTokens": 2,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            },
        },
    }
    usage = claude_usage(json.dumps(payload))
    assert usage.input_tokens == 135 and usage.output_tokens == 12
    assert usage.cache_read_tokens == 80 and usage.cache_write_tokens == 30
    assert usage.models == ["model-a", "model-b"] and usage.complete


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"type":"result"}'])
def test_missing_claude_usage_is_unknown(raw):
    assert claude_usage(raw).input_tokens is None
    assert not claude_usage(raw).complete


def test_zero_is_not_missing_and_invalid_fields_not_coerced():
    usage = claude_usage(
        json.dumps(
            {
                "type": "result",
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            }
        )
    )
    assert usage.input_tokens == 0 and usage.complete
    for value in [-1, True, 1.2, "15"]:
        assert ollama_usage({"prompt_eval_count": value}).input_tokens is None


def test_codex_only_usage_events_and_cache_is_subset():
    stream = "\n".join(
        [
            "noise",
            "[]",
            json.dumps({"type": "item.completed", "usage": {"input_tokens": 100000}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "cached_input_tokens": 90, "output_tokens": 12},
                }
            ),
        ]
    )
    usage = codex_usage(stream)
    assert usage.input_tokens == 100 and usage.output_tokens == 12
    assert usage.cache_read_tokens == 90 and usage.cache_write_tokens is None
    assert usage.complete
    assert codex_usage("").input_tokens is None


def test_ollama_all_requests_including_report_retry_and_missing_fields():
    a = ollama_usage({"prompt_eval_count": 20, "eval_count": 5, "model": "local"})
    b = ollama_usage({"prompt_eval_count": 30, "eval_count": 7, "model": "local"})
    usage = combine([a, b], "ollama.chat")
    assert usage.input_tokens == 50 and usage.output_tokens == 12
    assert usage.reported_requests == 2 and usage.cache_read_tokens is None
    assert combine([a, TokenUsage()], "partial").input_tokens is None


def task(tmp_path):
    return AgentTask(role="planner", prompt="private prompt", working_directory=tmp_path)


def result():
    return AgentResult(
        success=True,
        output="private answer",
        exit_code=0,
        duration_seconds=0.1,
        usage=TokenUsage(input_tokens=10, output_tokens=2, complete=True),
    )


def test_records_aggregate_retries_and_exclude_other_tickets(tmp_path):
    directory = tmp_path / ".dev-agent/runs/A/run1"
    provider = AsyncMock()
    provider.execute.return_value = result()

    async def run():
        await asyncio.gather(
            *[
                record_call(
                    provider, task(tmp_path), directory, "plan", "claude", "claude_cli", "model"
                )
                for _ in range(3)
            ]
        )
        provider.execute.return_value = result().model_copy(
            update={"success": False, "usage": TokenUsage()}
        )
        await record_call(
            provider, task(tmp_path), directory, "plan", "claude", "claude_cli", "model"
        )
        await record_call(
            provider,
            task(tmp_path),
            tmp_path / ".dev-agent/tickets/B",
            "ticket",
            "claude",
            "claude_cli",
            "model",
        )

    asyncio.run(run())
    report = usage_report(tmp_path, "A")
    row = report["groups"][0]
    assert report["recorded_calls"] == 4 and row["input_tokens"] == 30
    assert row["input_tokens_known_calls"] == 3 and row["complete_calls"] == 3
    assert row["failed_calls"] == 1
    assert len(list((directory / "usage").glob("*.json"))) == 4
    assert "private prompt" not in json.dumps(report) and "private answer" not in json.dumps(report)
    with chdir(tmp_path):
        response = CliRunner().invoke(app, ["usage", "A", "--json"])
    assert response.exit_code == 0
    assert json.loads(response.output)["recorded_calls"] == 4


@pytest.mark.parametrize("error", [OSError("private error"), asyncio.CancelledError()])
def test_launch_error_and_cancellation_remain_recorded(tmp_path, error):
    provider = AsyncMock()
    provider.execute.side_effect = error
    directory = tmp_path / ".dev-agent/runs/A/run1"
    with pytest.raises(type(error)):
        asyncio.run(record_call(provider, task(tmp_path), directory, "plan", "x", "x", None))
    record = usage_report(tmp_path)["records"][0]
    assert record["status"] in {"ERROR", "INTERRUPTED"}
    assert record["duration_seconds"] is not None and record["usage"]["input_tokens"] is None
    assert "private error" not in json.dumps(record)


def test_symlink_usage_directory_refused_before_model(tmp_path):
    directory = tmp_path / "plan"
    directory.mkdir()
    other = tmp_path / "outside"
    other.mkdir()
    (directory / "usage").symlink_to(other, target_is_directory=True)
    provider = AsyncMock()
    with pytest.raises(ValueError, match="symlink"):
        asyncio.run(record_call(provider, task(tmp_path), directory, "plan", "x", "x", None))
    provider.execute.assert_not_called()


def test_registry_routes_and_records_without_provider_contract_change(tmp_path):
    fake = FakeProvider({"planner": result()})
    registry = ProviderRegistry(ProjectConfig(), {"codex_cli": lambda _: fake})
    response = asyncio.run(
        registry.execute("ticket", tmp_path / ".dev-agent/tickets/A", task(tmp_path))
    )
    assert response.success
    assert usage_report(tmp_path, "A")["recorded_calls"] == 1


def test_unknown_historical_usage_cli(tmp_path):
    with chdir(tmp_path):
        response = CliRunner().invoke(app, ["usage", "OLD"])
    assert response.exit_code == 0 and "cannot be reconstructed" in response.output


def test_claude_failure_envelope_still_retains_usage(tmp_path):
    from test_claude_provider import Runner

    from dev_agent.models.config import ProviderConfig
    from dev_agent.providers.claude_cli import ClaudeCLIProvider

    provider = ClaudeCLIProvider(
        ProviderConfig(type="claude_cli"),
        runner=Runner(
            {
                "type": "result",
                "is_error": True,
                "subtype": "error_max_turns",
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 0,
                },
            }
        ),
    )
    response = asyncio.run(provider.execute(task(tmp_path)))
    assert not response.success and response.usage.input_tokens == 12


def test_ollama_real_adapter_accumulates_tool_and_report_retries(tmp_path):
    import subprocess

    from test_ollama import config, report_response, response

    from dev_agent.models.finding import Review
    from dev_agent.providers.ollama import OllamaProvider

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    replies = [
        response("finish", content="done"),
        report_response("invalid json"),
        report_response('{"findings":[]}'),
    ]
    for reply in replies:
        reply.update(prompt_eval_count=10, eval_count=2)

    async def transport(url, payload, timeout):
        return replies.pop(0)

    provider = OllamaProvider(config(), transport=transport)
    request = task(tmp_path).model_copy(update={"output_schema": Review.model_json_schema()})
    response = asyncio.run(provider.execute(request))
    assert response.success, response.error
    assert response.usage.input_tokens == 30 and response.usage.output_tokens == 6
    assert response.usage.reported_requests == 3


def test_codex_adapter_json_events_do_not_replace_final_answer(tmp_path):
    from datetime import UTC, datetime

    from dev_agent.models.config import ProviderConfig
    from dev_agent.process import ProcessResult
    from dev_agent.providers.codex_cli import CodexCLIProvider

    async def run(command, *, cwd, stdin=None, timeout=60):
        assert "--json" in command
        Path(command[command.index("--output-last-message") + 1]).write_text("final answer")
        return ProcessResult(
            tuple(command),
            cwd,
            datetime.now(UTC),
            0.1,
            0,
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 5},
                }
            ),
            "",
            False,
        )

    response = asyncio.run(CodexCLIProvider(ProviderConfig(), runner=run).execute(task(tmp_path)))
    assert response.output == "final answer"
    assert response.usage.input_tokens == 100 and response.usage.cache_read_tokens == 80
