import asyncio
import json
import os
import subprocess
from unittest.mock import patch

import httpx
import pytest

from dev_agent.doctor import check_environment
from dev_agent.models.agent import AgentTask
from dev_agent.models.config import ROLES, ProjectConfig, ProviderConfig, RoleConfig
from dev_agent.providers.ollama import FileTools, OllamaProvider
from dev_agent.providers.registry import ProviderRegistry


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "app.py").write_text("old\n")
    (tmp_path / ".gitignore").write_text("private/\n")
    return tmp_path


def config(**kwargs):
    return ProviderConfig(type="ollama", model="test:7b", **kwargs)


def task(repo, read_only=False, prompt="Fix app.py"):
    return AgentTask(role="implementer", prompt=prompt, working_directory=repo, read_only=read_only)


def response(action, path="", content=""):
    return {
        "done": True,
        "message": {"content": json.dumps(dict(action=action, path=path, content=content))},
    }


def run(repo, responses, **kwargs):
    async def transport(url, payload, timeout):
        return responses.pop(0)

    return asyncio.run(OllamaProvider(config(**kwargs), transport=transport).execute(task(repo)))


def test_real_file_loop(repo):
    result = run(
        repo,
        [
            response("read_file", "app.py"),
            response("write_file", "app.py", "new\n"),
            response("finish", content='```json\n{"ok":true}\n```'),
        ],
    )
    assert result.success and json.loads(result.output) == {"ok": True}
    assert (repo / "app.py").read_text() == "new\n"


def test_readonly_role_rejects_model_write(repo):
    async def transport(url, payload, timeout):
        assert "write_file" not in payload["format"]["properties"]["action"]["enum"]
        return response("write_file", "app.py", "bad")

    result = asyncio.run(OllamaProvider(config(), transport=transport).execute(task(repo, True)))
    assert not result.success
    assert (repo / "app.py").read_text() == "old\n"


@pytest.mark.parametrize(
    "name",
    ["../outside", "/tmp/outside", ".git/config", ".env", ".env.local", ".ssh/key", "private/file"],
)
def test_tool_rejects_sensitive_or_ignored_writes(repo, name):
    with pytest.raises(ValueError):
        asyncio.run(FileTools(repo, False).execute("write_file", name, "bad"))


def test_links_and_readonly(repo):
    (repo / "link").symlink_to(repo / "app.py")
    os.link(repo / "app.py", repo / "hard")
    for name in ("link", "hard"):
        with pytest.raises(ValueError):
            FileTools(repo, False).path(name)
    with pytest.raises(ValueError, match="read-only"):
        asyncio.run(FileTools(repo, True).execute("write_file", "new.py", "bad"))


def test_ignored_read_and_file_size(repo):
    (repo / "private").mkdir()
    (repo / "private" / "secret").write_text("secret")
    with pytest.raises(ValueError, match="ignored"):
        asyncio.run(FileTools(repo, True).execute("read_file", "private/secret", ""))
    (repo / "large").write_text("x" * 48001)
    with pytest.raises(ValueError, match="large"):
        asyncio.run(FileTools(repo, True).execute("read_file", "large", ""))


@pytest.mark.parametrize(
    "value",
    [
        {"done": True, "message": {"content": "invalid json"}},
        {"done": True, "done_reason": "length"},
        {"done": False},
        response("shell"),
        response("finish"),
    ],
)
def test_invalid_model_responses_fail(repo, value):
    assert not run(repo, [value]).success


def test_step_budget(repo):
    result = run(repo, [response("list_files")], max_steps=1)
    assert not result.success and "max_steps" in result.error


def test_timeout_cancels_transport(repo):
    cancelled = []

    async def transport(url, payload, timeout):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)

    result = asyncio.run(
        OllamaProvider(config(timeout_seconds=0.02), transport=transport).execute(task(repo))
    )
    assert result.timed_out and cancelled


def test_context_budget_prevents_request(repo):
    async def transport(url, payload, timeout):
        pytest.fail("must not call model")

    result = asyncio.run(
        OllamaProvider(config(), transport=transport).execute(task(repo, prompt="x" * 30000))
    )
    assert not result.success and "too large" in result.error


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "ftp://localhost"},
        {"base_url": "http://user:pass@localhost"},
        {"base_url": "http://localhost/path"},
        {"base_url": "http://localhost:99999"},
        {"num_ctx": 2048, "max_output_tokens": 2048},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        config(**kwargs)


def test_registry():
    project = ProjectConfig(
        providers={"local": config()}, roles={role: RoleConfig(provider="local") for role in ROLES}
    )
    assert isinstance(ProviderRegistry(project).resolve("implementer"), OllamaProvider)


@pytest.mark.parametrize("models,passed", [(["test:7b"], True), ([], False)])
def test_doctor_checks_installed_model(repo, models, passed):
    project = ProjectConfig(
        providers={"local": config()}, roles={role: RoleConfig(provider="local") for role in ROLES}
    )
    (repo / ".dev-agent.yaml").write_text(project.model_dump_json())
    reply = httpx.Response(
        200,
        json={"models": [{"name": m} for m in models]},
        request=httpx.Request("GET", "http://localhost/api/tags"),
    )
    with patch("dev_agent.doctor.httpx.Client.get", return_value=reply):
        checks = check_environment(repo)
    assert next(c for c in checks if c.name == "local").passed == passed


def test_root_instructions_and_tool_refusal_recovery(repo):
    (repo / "AGENTS.md").write_text("Preserve public interfaces.")
    calls = []

    async def transport(url, payload, timeout):
        calls.append(payload["messages"][-1]["content"])
        assert any("Preserve public interfaces." in m["content"] for m in payload["messages"])
        if len(calls) == 1:
            return response("read_file", "../secret")
        assert "Tool refused:" in calls[-1]
        return response("finish", content="Unable to read outside the repository.")

    result = asyncio.run(OllamaProvider(config(), transport=transport).execute(task(repo)))
    assert result.success and len(calls) == 2


def test_connection_error_is_actionable(repo):
    async def transport(url, payload, timeout):
        raise httpx.ConnectError("Connection refused")

    result = asyncio.run(OllamaProvider(config(), transport=transport).execute(task(repo)))
    assert not result.success and "Connection refused" in result.error


def test_existing_file_requires_read_before_write(repo):
    tools = FileTools(repo, False)
    with pytest.raises(ValueError, match="Read the existing"):
        asyncio.run(tools.execute("write_file", "app.py", "bad"))
    assert (repo / "app.py").read_text() == "old\n"


def test_truncation_and_concurrent_edit_refused(repo):
    (repo / "README.md").write_text("Preserve this line\n" * 100)
    tools = FileTools(repo, False)
    asyncio.run(tools.execute("read_file", "README.md", ""))
    with pytest.raises(ValueError, match="truncation"):
        asyncio.run(tools.execute("write_file", "README.md", "Only new content\n"))
    (repo / "README.md").write_text("human edits\n")
    with pytest.raises(ValueError, match="changed since read"):
        asyncio.run(tools.execute("write_file", "README.md", "replacement\n"))
    assert (repo / "README.md").read_text() == "human edits\n"


def report_response(text, truncated=False):
    return {
        "done": True,
        "done_reason": "length" if truncated else "stop",
        "message": {"content": text},
    }


@pytest.mark.parametrize(
    "bad",
    [
        report_response("not JSON"),
        report_response('{"summary": 123, "decisions": []}'),
        report_response('{"summary": "missing required decisions"}'),
        report_response('{"summary":"x","decisions":[],"unexpected":true}'),
        report_response('{"summary":"x","decisions":[{"finding_index":-1}]}'),
        report_response('{"summary":', truncated=True),
        response("write_file", "app.py", "must never execute"),
    ],
)
def test_structured_report_retry_never_replays_edits(repo, bad):
    from dev_agent.artifacts import parse_artifact
    from dev_agent.models.fix import FixReport

    schema = FixReport.model_json_schema()
    replies = [
        response("read_file", "app.py"),
        response("write_file", "app.py", "changed\n"),
        response("finish", content="Finished edits; report draft."),
        bad,
        report_response('{"summary":"Updated app.py","decisions":[]}'),
    ]
    requests = []

    async def transport(url, payload, timeout):
        requests.append(payload)
        return replies.pop(0)

    request = task(repo).model_copy(update={"output_schema": schema})
    with patch.object(
        FileTools, "execute", autospec=True, side_effect=FileTools.execute
    ) as execute:
        result = asyncio.run(OllamaProvider(config(), transport=transport).execute(request))
    assert result.success, result.error
    assert parse_artifact(result.output, FixReport).summary == "Updated app.py"
    assert len(requests) == 5
    assert requests[-1]["format"] == requests[-2]["format"] == schema
    assert "No tools are available" in requests[-1]["messages"][0]["content"]
    assert [call.args[1] for call in execute.call_args_list] == ["read_file", "write_file"]
    assert (repo / "app.py").read_text() == "changed\n"


def test_report_retries_are_bounded(repo):
    from dev_agent.models.finding import Review

    replies = [response("finish"), report_response("{}"), report_response("{}")]
    calls = []

    async def transport(url, payload, timeout):
        calls.append(payload)
        return replies.pop(0)

    request = task(repo, True).model_copy(update={"output_schema": Review.model_json_schema()})
    result = asyncio.run(OllamaProvider(config(max_steps=1), transport=transport).execute(request))
    assert not result.success and "after 2 attempts" in result.error
    assert len(calls) == 3 and not replies


def test_valid_nested_refs_report(repo):
    from dev_agent.artifacts import parse_artifact
    from dev_agent.models.fix import FixReport

    expected = {
        "summary": "fixed",
        "decisions": [
            {
                "finding_index": 0,
                "disposition": "fixed",
                "explanation": "Verified against code",
                "evidence": ["app.py:1"],
            }
        ],
    }
    replies = [response("finish"), report_response(json.dumps(expected))]

    async def transport(url, payload, timeout):
        return replies.pop(0)

    request = task(repo, True).model_copy(update={"output_schema": FixReport.model_json_schema()})
    result = asyncio.run(OllamaProvider(config(), transport=transport).execute(request))
    assert result.success
    assert parse_artifact(result.output, FixReport).decisions[0].finding_index == 0


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "not-a-type"},
        {"$ref": "https://example.invalid/schema"},
    ],
)
def test_invalid_schema_fails_before_tools_or_transport(repo, schema):
    async def transport(url, payload, timeout):
        pytest.fail("invalid schema must fail before inference")

    result = asyncio.run(
        OllamaProvider(config(), transport=transport).execute(
            task(repo).model_copy(update={"output_schema": schema})
        )
    )
    assert not result.success
    assert (repo / "app.py").read_text() == "old\n"


def test_report_timeout_does_not_restart_tools(repo):
    from dev_agent.models.finding import Review

    calls = []

    async def transport(url, payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            return response("finish")
        await asyncio.sleep(30)

    result = asyncio.run(
        OllamaProvider(config(timeout_seconds=0.02), transport=transport).execute(
            task(repo, True).model_copy(update={"output_schema": Review.model_json_schema()})
        )
    )
    assert result.timed_out and len(calls) == 2
