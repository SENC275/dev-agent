import asyncio
import json

from test_providers import task
from test_worktree import git
from test_worktree import repository as repository

from dev_agent.models.config import ProjectContextConfig
from dev_agent.project_context import build, prepare


def fixture_files(repo):
    (repo / "tests").mkdir()
    (repo / "tests/test_tasks.py").write_text("def test_tasks(): pass")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=['tests']")
    git(repo, "add", ".")


def test_shared_across_tickets_and_auto_refresh(repository):
    fixture_files(repository)
    first = asyncio.run(build(repository))
    second = asyncio.run(build(repository))
    assert first["cache_status"] == "refreshed" and second["cache_status"] == "hit"
    config = ProjectContextConfig(enabled=True)
    for ticket in ["Add task pagination", "Validate task title"]:
        request = task(repository).model_copy(update={"prompt": ticket})
        supplied, info = asyncio.run(prepare(request, "investigate", config))
        assert info["used"] and info["cache_status"] == "hit"
        assert "tests/test_tasks.py" in supplied.prompt
        assert "pyproject.toml" in supplied.prompt
        assert supplied.prompt.endswith(ticket)
    (repository / "AGENTS.md").write_text("Use unittest now.")
    third = asyncio.run(build(repository))
    assert third["cache_status"] == "refreshed" and third["identity"] != first["identity"]
    assert third["conventions"][0]["excerpt"] == "Use unittest now."
    (repository / "AGENTS.md").write_text("Use pytest again.")
    assert asyncio.run(build(repository))["identity"] != third["identity"]


def test_added_deleted_and_branch_changes(repository):
    fixture_files(repository)
    first = asyncio.run(build(repository))
    (repository / "tests/test_tasks.py").unlink()
    changed = asyncio.run(build(repository))
    assert not any(e["path"] == "tests/test_tasks.py" for e in changed["entries"])
    git(repository, "add", "-A")
    git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "new test structure")
    committed = asyncio.run(build(repository))
    assert committed["base_commit"] != first["base_commit"]
    git(repository, "checkout", "--detach", first["base_commit"])
    assert asyncio.run(build(repository))["base_commit"] == first["base_commit"]


def test_corrupt_tampered_and_symlink_cache(repository, tmp_path):
    asyncio.run(build(repository))
    path = repository / ".dev-agent/project-context.json"
    path.write_text("invalid json")
    assert asyncio.run(build(repository))["cache_status"] == "refreshed"
    cached = json.loads(path.read_text())
    cached["conventions"][0]["excerpt"] = "injected text"
    path.write_text(json.dumps(cached))
    assert "injected text" not in json.dumps(asyncio.run(build(repository)))
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    path.symlink_to(outside)
    original = task(repository)
    supplied, info = asyncio.run(
        prepare(original, "investigate", ProjectContextConfig(enabled=True))
    )
    assert supplied == original and not info["used"]
    assert outside.read_text() == "untouched"


def test_credentials_and_symlink_documents_excluded(repository, tmp_path):
    (repository / ".env").write_text("api_key=TOPSECRET")
    (repository / "CONTRIBUTING.md").write_text("Use tests. api_key=TOPSECRET")
    (repository / "CLAUDE.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("PRIVATE OUTSIDE")
    git(repository, "add", ".")
    data = asyncio.run(build(repository))
    assert "TOPSECRET" not in json.dumps(data) and "PRIVATE OUTSIDE" not in json.dumps(data)
    assert not any(e["path"] in {".env", "CLAUDE.md"} for e in data["entries"])


def test_opt_in_stage_and_budget(repository):
    fixture_files(repository)
    original = task(repository)
    for config, stage in [(ProjectContextConfig(), "investigate"),
                          (ProjectContextConfig(enabled=True), "review")]:
        supplied, info = asyncio.run(prepare(original, stage, config))
        assert supplied == original and not info["used"]
    assert not (repository / ".dev-agent/project-context.json").exists()
    config = ProjectContextConfig(enabled=True, max_characters=1500)
    supplied, info = asyncio.run(prepare(original, "ticket", config))
    assert info["used"] and 0 < info["characters"] <= 1500
    assert len(supplied.prompt) - len(original.prompt) == info["characters"]


def test_no_extra_provider_calls_and_usage_metadata(repository):
    from test_persistence import PipelineProvider

    from dev_agent.models.config import ProjectConfig
    from dev_agent.providers.registry import ProviderRegistry
    from dev_agent.usage import usage_report

    fixture_files(repository)
    provider = PipelineProvider()
    config = ProjectConfig(project_context=ProjectContextConfig(enabled=True))
    registry = ProviderRegistry(config, {"codex_cli": lambda _: provider})
    for tid in ["A", "B"]:
        asyncio.run(registry.execute("investigate", repository / ".dev-agent/runs" / tid / "r",
                                     task(repository).model_copy(update={"role": "explorer"})))
    assert len(provider.tasks) == 2
    records = usage_report(repository)["records"]
    assert all(r["context"]["project"]["used"] for r in records)
    assert {r["context"]["project"]["cache_status"] for r in records} == {"refreshed", "hit"}


def test_refresh_command(repository, monkeypatch):
    from typer.testing import CliRunner

    from dev_agent.cli import app

    monkeypatch.chdir(repository)
    output = CliRunner().invoke(app, ["project-context", "--refresh"])
    assert output.exit_code == 0 and "refreshed" in output.output
    assert "project-context.json" in git(repository, "ls-files", "--others", "--ignored",
                                         "--exclude-standard")
