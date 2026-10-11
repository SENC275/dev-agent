import asyncio
import fcntl
import json
from contextlib import chdir
from unittest.mock import patch

import pytest
from test_ticket import registry
from test_worktree import git
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.models.agent import AgentResult
from dev_agent.workflow.ticket_batch import generate_batch


def inputs(tmp_path, names=("TF-001", "TF-002")):
    root = tmp_path / "requirements"
    root.mkdir()
    for name in names:
        (root / name).mkdir()
        (root / name / "description.md").write_text("Add something: " + name)
    return root


def statuses(report):
    return [item["status"] for item in report["items"]]


def test_generation_sources_and_repeat_without_calls(repository, tmp_path):
    root = inputs(tmp_path)
    (root / "TF-001" / "constraints.txt").write_text("Keep the existing API")
    provider, providers = registry()
    progress = []
    report = asyncio.run(generate_batch(repository, root, providers, progress.append))
    assert statuses(report) == ["GENERATED", "GENERATED"]
    assert len(provider.tasks) == 2 and all(t.read_only for t in provider.tasks)
    assert "Keep the existing API" in provider.tasks[0].prompt
    assert progress and "2/2" in progress[-1]
    saved = repository / ".dev-agent/tickets/TF-001.md"
    original = saved.read_bytes()
    repeat = asyncio.run(generate_batch(repository, root, providers))
    assert statuses(repeat) == ["SKIPPED", "SKIPPED"]
    assert len(provider.tasks) == 2 and saved.read_bytes() == original
    assert not git(repository, "status", "--porcelain")
    assert not (repository / ".dev-agent/state.sqlite3").exists()
    from pathlib import Path

    assert json.loads(Path(report["report_path"]).read_text())["finished_at"]


def test_one_failure_continues_and_retry_only_failed(repository, tmp_path):
    root = inputs(tmp_path)
    provider, providers = registry()
    original = provider.execute
    first = True

    async def execute(task):
        nonlocal first
        if first:
            first = False
            return AgentResult(
                success=False,
                output="",
                exit_code=1,
                duration_seconds=0,
                error="provider unavailable",
            )
        return await original(task)

    with patch.object(provider, "execute", side_effect=execute):
        report = asyncio.run(generate_batch(repository, root, providers))
        assert statuses(report) == ["FAILED", "GENERATED"]
        repeated = asyncio.run(generate_batch(repository, root, providers))
    assert statuses(repeated) == ["GENERATED", "SKIPPED"]


@pytest.mark.parametrize("change", ["description", "draft", "config", "code", "receipt"])
def test_changed_inputs_never_overwrite(repository, tmp_path, change):
    root = inputs(tmp_path, ("TF-001",))
    provider, providers = registry()
    asyncio.run(generate_batch(repository, root, providers))
    output = repository / ".dev-agent/tickets/TF-001.md"
    if change == "description":
        (root / "TF-001/description.md").write_text("different")
    elif change == "draft":
        output.write_text("human edits")
    elif change == "config":
        providers.config.providers["codex"].model = "different-model"
    elif change == "code":
        (repository / "app.txt").write_text("changed source")
    else:
        (repository / ".dev-agent/ticket-batches/receipts/TF-001.json").write_text("invalid")
    before = output.read_bytes()
    assert statuses(asyncio.run(generate_batch(repository, root, providers))) == ["CONFLICT"]
    assert len(provider.tasks) == 1 and output.read_bytes() == before


@pytest.mark.parametrize("problem", ["missing", "blank", "large", "binary", "link", "bad-id"])
def test_invalid_inputs_do_not_call_models(repository, tmp_path, problem):
    name = "bad id" if problem == "bad-id" else "TF-001"
    root = inputs(tmp_path, (name,))
    source = root / name / "description.md"
    if problem == "missing":
        source.unlink()
    elif problem == "blank":
        source.write_text(" ")
    elif problem == "large":
        source.write_text("a" * 32_001)
    elif problem == "binary":
        source.write_bytes(b"\x00private")
    elif problem == "link":
        outside = tmp_path / "external.txt"
        outside.write_text("private")
        source.unlink()
        source.symlink_to(outside)
    provider, providers = registry()
    assert statuses(asyncio.run(generate_batch(repository, root, providers))) == ["INVALID"]
    assert not provider.tasks


def test_input_changes_during_generation_are_not_reused(repository, tmp_path):
    root = inputs(tmp_path, ("TF-001",))
    provider, providers = registry()
    original = provider.execute

    async def execute(task):
        (root / "TF-001/description.md").write_text("new request")
        return await original(task)

    with patch.object(provider, "execute", side_effect=execute):
        assert statuses(asyncio.run(generate_batch(repository, root, providers))) == ["FAILED"]
    assert statuses(asyncio.run(generate_batch(repository, root, providers))) == ["CONFLICT"]
    assert len(provider.tasks) == 1


def test_lock_and_existing_single_draft(repository, tmp_path):
    root = inputs(tmp_path, ("TF-001",))
    provider, providers = registry()
    folder = repository / ".dev-agent/ticket-batches"
    folder.mkdir(parents=True)
    with (folder / "batch.lock").open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="Another ticket batch"):
            asyncio.run(generate_batch(repository, root, providers))
    output = repository / ".dev-agent/tickets/TF-001.md"
    output.parent.mkdir()
    output.write_text("existing single draft")
    assert statuses(asyncio.run(generate_batch(repository, root, providers))) == ["CONFLICT"]
    assert not provider.tasks


def test_cli_compatibility_and_failure_exit(repository, tmp_path):
    root = inputs(tmp_path)
    provider, providers = registry()
    (repository / ".dev-agent.yaml").write_text("{}")
    with chdir(repository), patch("dev_agent.cli.ProviderRegistry", return_value=providers):
        runner = CliRunner()
        result = runner.invoke(app, ["ticket", "batch", "--folder", str(root)])
        assert result.exit_code == 0, result.output
        assert "GENERATED" in result.output and "Batch report" in result.output
        result = runner.invoke(app, ["ticket", "batch", "-d", "a normal ticket named batch"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["ticket", "wrong", "--folder", str(root)])
        assert result.exit_code == 1
        (root / "TF-001/description.md").write_text("changed")
        result = runner.invoke(app, ["ticket", "batch", "--folder", str(root)])
        assert result.exit_code == 1 and "CONFLICT" in result.output and "SKIPPED" in result.output


def test_repository_mutation_blocks_later_model_calls(repository, tmp_path):
    root = inputs(tmp_path)
    provider, providers = registry()
    original = provider.execute

    async def execute(task):
        (repository / "app.txt").write_text("unexpected mutation")
        return await original(task)

    with patch.object(provider, "execute", side_effect=execute):
        result = asyncio.run(generate_batch(repository, root, providers))
    assert statuses(result) == ["FAILED", "FAILED"]
    assert len(provider.tasks) == 1


def test_invalid_item_does_not_block_valid_item(repository, tmp_path):
    root = inputs(tmp_path)
    (root / "TF-001/description.md").unlink()
    provider, providers = registry()
    assert statuses(asyncio.run(generate_batch(repository, root, providers))) == [
        "INVALID",
        "GENERATED",
    ]
    assert len(provider.tasks) == 1


def test_output_directory_symlink_is_rejected(repository, tmp_path):
    root = inputs(tmp_path)
    (repository / ".dev-agent").mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (repository / ".dev-agent/ticket-batches").symlink_to(external)
    provider, providers = registry()
    with pytest.raises(ValueError, match="Unsafe"):
        asyncio.run(generate_batch(repository, root, providers))
    assert not provider.tasks and not list(external.iterdir())


def test_unexpected_provider_exception_does_not_abort_batch(repository, tmp_path):
    root = inputs(tmp_path)
    provider, providers = registry()
    original = provider.execute
    first = True

    async def execute(task):
        nonlocal first
        if first:
            first = False
            raise RuntimeError("sensitive provider payload")
        return await original(task)

    with patch.object(provider, "execute", side_effect=execute):
        result = asyncio.run(generate_batch(repository, root, providers))
    assert statuses(result) == ["FAILED", "GENERATED"]
    assert "sensitive provider payload" not in json.dumps(result)
