import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from dev_agent.git.worktree import clean_baseline, create_approved_worktree, create_worktree
from dev_agent.models.config import GitConfig
from dev_agent.workflow.planning import HEADINGS, _inputs, approve_plan


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init")
    (repo / "app.txt").write_text("baseline\n")
    (repo / ".dev-agent.yaml").write_text("{}")
    (repo / "AGENTS.md").write_text("Use the smallest safe changes.")
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "baseline",
    )
    return repo


def create(repo: Path, **config: str) -> object:
    return asyncio.run(
        create_worktree(
            repo,
            ticket="DEMO-7",
            base_commit=git(repo, "rev-parse", "HEAD"),
            config=GitConfig(**config),
        )
    )


def test_creates_isolated_worktree(repository: Path) -> None:
    before = git(repository, "status", "--porcelain"), git(repository, "symbolic-ref", "HEAD")
    result = asyncio.run(
        create_worktree(
            repository,
            ticket="DEMO-7",
            base_commit=git(repository, "rev-parse", "HEAD"),
            config=GitConfig(),
        )
    )
    target = Path(result.path)
    assert target == repository.parent / "worktrees/DEMO-7"
    assert git(target, "branch", "--show-current") == "codex/DEMO-7"
    assert git(target, "rev-parse", "HEAD") == result.base_commit
    (target / "app.txt").write_text("isolated change")
    assert (repository / "app.txt").read_text() == "baseline\n"
    assert before == (
        git(repository, "status", "--porcelain"),
        git(repository, "symbolic-ref", "HEAD"),
    )


def test_custom_names(repository: Path) -> None:
    create(
        repository, branch_pattern="custom/{ticket}", worktree_directory="../custom tree/{ticket}"
    )
    assert (
        git(repository.parent / "custom tree/DEMO-7", "branch", "--show-current") == "custom/DEMO-7"
    )


@pytest.mark.parametrize("kind", ["branch", "directory", "symlink", "registered"])
def test_conflicts_leave_source_untouched(repository: Path, kind: str) -> None:
    target = repository.parent / "worktrees/DEMO-7"
    target.parent.mkdir()
    if kind == "branch":
        git(repository, "branch", "codex/DEMO-7")
    elif kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(repository.parent / "missing")
    else:
        git(repository, "worktree", "add", "-b", "other", str(target))
    before = git(repository, "status", "--porcelain")
    with pytest.raises(ValueError):
        create(repository)
    assert git(repository, "status", "--porcelain") == before


@pytest.mark.parametrize("location", [".", "..", ".git/unsafe", "nested/tree"])
def test_overlapping_path_rejected(repository: Path, location: str) -> None:
    with pytest.raises(ValueError):
        create(repository, worktree_directory=location)
    assert "refs/heads/codex/DEMO-7" not in git(repository, "show-ref")


@pytest.mark.parametrize(
    "branch", ["-bad", "bad name", "x/../y", "{unknown}", "{ticket.__class__}"]
)
def test_invalid_branch_or_template(repository: Path, branch: str) -> None:
    with pytest.raises(ValueError):
        create(repository, branch_pattern=branch)


@pytest.mark.parametrize("kind", ["tracked", "staged", "untracked"])
def test_dirty_source_rejected_without_changes(repository: Path, kind: str) -> None:
    path = repository / ("extra.txt" if kind == "untracked" else "app.txt")
    path.write_text("user changes")
    if kind == "staged":
        git(repository, "add", ".")
    before = git(repository, "status", "--porcelain")
    with pytest.raises(ValueError, match="uncommitted"):
        create(repository)
    assert path.read_text() == "user changes"
    assert git(repository, "status", "--porcelain") == before


def test_untracked_artifacts_do_not_dirty_baseline(repository: Path) -> None:
    (repository / ".dev-agent").mkdir()
    (repository / ".dev-agent/run.json").write_text("{}")
    assert asyncio.run(clean_baseline(repository))


@pytest.fixture
def approved_plan(repository: Path) -> Path:
    run = repository / ".dev-agent/runs/DEMO-7/run1"
    plan = run / "plans/plan1"
    plan.mkdir(parents=True)
    (run / "investigation.json").write_text(
        json.dumps(
            {
                "status": "COMPLETE",
                "repository": str(repository),
                "ticket_id": "DEMO-7",
            }
        )
    )
    for name in ["ticket.md", "exploration.json", "patterns.json", "tests.json"]:
        (run / name).write_text("{}")
    text = "\n\n".join(f"## {heading}\nDetails." for heading in HEADINGS)
    (plan / "plan.md").write_text(text)
    (plan / "planning.json").write_text(
        json.dumps(
            {
                "base_commit": git(repository, "rev-parse", "HEAD"),
                "input_hashes": _inputs(run),
                "clean_baseline": True,
            }
        )
    )
    asyncio.run(approve_plan(plan, text))
    return plan


def test_approved_worktree_records_result_and_refuses_repeat(approved_plan: Path) -> None:
    result = asyncio.run(create_approved_worktree(approved_plan, GitConfig()))
    metadata = json.loads((approved_plan / "worktree.json").read_text())
    assert metadata["path"] == result.path
    assert not (approved_plan / "worktree.lock").exists()
    with pytest.raises(ValueError, match="already has"):
        asyncio.run(create_approved_worktree(approved_plan, GitConfig()))


@pytest.mark.parametrize("kind", ["absent", "stale", "dirty_plan", "legacy", "lock"])
def test_invalid_approval_or_baseline(approved_plan: Path, kind: str) -> None:
    if kind == "absent":
        (approved_plan / "approval.json").unlink()
    elif kind == "stale":
        with (approved_plan / "plan.md").open("a") as stream:
            stream.write("\nChanged scope")
    elif kind == "lock":
        (approved_plan / "worktree.lock").write_text("busy")
    else:
        path = approved_plan / "planning.json"
        value = json.loads(path.read_text())
        if kind == "legacy":
            del value["clean_baseline"]
        else:
            value["clean_baseline"] = False
        path.write_text(json.dumps(value))
    with pytest.raises((ValueError, FileExistsError)):
        asyncio.run(create_approved_worktree(approved_plan, GitConfig()))
    assert not (approved_plan / "worktree.json").exists()


def test_staged_and_unstaged_changes_cannot_cancel_out(repository: Path) -> None:
    (repository / "app.txt").write_text("staged change")
    git(repository, "add", "app.txt")
    (repository / "app.txt").write_text("baseline\n")
    assert not asyncio.run(clean_baseline(repository))
    with pytest.raises(ValueError, match="uncommitted"):
        create(repository)


def test_stale_base_commit_rejected(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD")
    git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "next",
    )
    with pytest.raises(ValueError, match="HEAD changed"):
        asyncio.run(
            create_worktree(repository, ticket="DEMO", base_commit=base, config=GitConfig())
        )


def test_checkout_hooks_are_not_run(repository: Path) -> None:
    hook = repository / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\ntouch hook-was-run\n")
    hook.chmod(0o755)
    create(repository)
    assert not (repository / "hook-was-run").exists()
    assert not (repository.parent / "worktrees/DEMO-7/hook-was-run").exists()


def test_cli_creates_worktree(approved_plan: Path) -> None:
    from typer.testing import CliRunner

    from dev_agent.cli import app

    result = CliRunner().invoke(app, ["worktree", str(approved_plan)])
    assert result.exit_code == 0, result.output
    assert "Implementation has not started" in result.output
    assert (approved_plan / "worktree.json").exists()
