import asyncio
from contextlib import chdir
from pathlib import Path

import pytest
from test_revision import ready as ready
from test_worktree import git
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.workflow.merging import merge, preview_merge


def test_commit_and_fast_forward_with_saved_failed_state(ready: tuple) -> None:
    journal, _, _, _ = ready
    git(journal.repository, "config", "user.name", "Test")
    git(journal.repository, "config", "user.email", "test@example.invalid")
    journal.update(
        "DEMO-12", state="FAILED"
    )  # Current artifacts, not stale status, govern acceptance.
    preview = asyncio.run(preview_merge(journal, "DEMO-12"))
    commit = asyncio.run(merge(journal, "DEMO-12", preview, "Accept ticket"))
    assert git(journal.repository, "rev-parse", "HEAD") == commit
    assert git(preview.target, "rev-parse", "HEAD") == commit
    assert (journal.repository / "app.txt").read_text() == "implemented\n"
    assert journal.get("DEMO-12")["state"] == "MERGED"
    assert not git(journal.repository, "status", "--porcelain", "--untracked-files=no")
    with pytest.raises(ValueError, match="already attempted"):
        asyncio.run(preview_merge(journal, "DEMO-12"))


def test_already_committed_worktree_can_merge(ready: tuple) -> None:
    journal, _, _, _ = ready
    target = Path(journal.get("DEMO-12")["worktree_path"])
    git(target, "add", "app.txt")
    git(
        target,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Manual acceptance",
    )
    with pytest.raises(ValueError, match="already has commits"):
        asyncio.run(preview_merge(journal, "DEMO-12"))


@pytest.mark.parametrize("kind", ["source_dirty", "worktree_dirty", "source_advanced", "staged"])
def test_unsafe_merge_refused(ready: tuple, kind: str) -> None:
    journal, _, _, _ = ready
    target = Path(journal.get("DEMO-12")["worktree_path"])
    if kind == "source_dirty":
        (journal.repository / "app.txt").write_text("user work")
    elif kind == "worktree_dirty":
        (target / "new.txt").write_text("unreviewed")
    elif kind == "staged":
        git(target, "add", "app.txt")
    else:
        git(
            journal.repository,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "Advanced",
        )
    before = git(journal.repository, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        asyncio.run(preview_merge(journal, "DEMO-12"))
    assert git(journal.repository, "rev-parse", "HEAD") == before


def test_changed_after_confirmation_refused(ready: tuple) -> None:
    journal, _, _, _ = ready
    preview = asyncio.run(preview_merge(journal, "DEMO-12"))
    (preview.target / "app.txt").write_text("later")
    with pytest.raises(ValueError):
        asyncio.run(merge(journal, "DEMO-12", preview, "should not commit"))
    assert git(journal.repository, "rev-parse", "HEAD") == preview.base


def test_cli_preview_and_noninteractive_require_consent(ready: tuple) -> None:
    journal, _, _, _ = ready
    before = git(journal.repository, "rev-parse", "HEAD")
    with chdir(journal.repository):
        result = CliRunner().invoke(app, ["merge", "DEMO-12", "--dry-run"])
        assert result.exit_code == 0, result.output
        result = CliRunner().invoke(app, ["merge", "DEMO-12"])
        assert result.exit_code == 1 and "--yes" in result.output
    assert git(journal.repository, "rev-parse", "HEAD") == before


def test_merge_includes_reviewed_new_files_and_deletions(ready: tuple) -> None:
    from dev_agent.validation.runner import validate
    from dev_agent.workflow.review import review

    journal, config, _, registry = ready
    target = Path(journal.get("DEMO-12")["worktree_path"])
    directory = Path(journal.get("DEMO-12")["plan_path"])
    (target / "app.txt").unlink()
    (target / "new migration.py").write_text("# reviewed migration\n")
    asyncio.run(validate(directory, config.commands, config.validation))
    asyncio.run(review(directory, registry))
    git(journal.repository, "config", "user.name", "Test")
    git(journal.repository, "config", "user.email", "test@example.invalid")
    preview = asyncio.run(preview_merge(journal, "DEMO-12"))
    asyncio.run(merge(journal, "DEMO-12", preview, "Accept files"))
    assert not (journal.repository / "app.txt").exists()
    assert (journal.repository / "new migration.py").read_text() == "# reviewed migration\n"


def test_new_feedback_cannot_reuse_old_review(ready: tuple) -> None:
    import hashlib
    import json

    from dev_agent.workflow.persistence import now

    journal, _, _, _ = ready
    directory = Path(journal.get("DEMO-12")["plan_path"])
    revision = directory / "revisions" / "0001"
    revision.mkdir(parents=True)
    feedback = b"New unreviewed requirement"
    (revision / "feedback.md").write_bytes(feedback)
    (revision / "revision.json").write_text(
        json.dumps(
            {
                "feedback_sha256": hashlib.sha256(feedback).hexdigest(),
                "started_at": now(),
                "status": "FAILED",
            }
        )
    )
    with pytest.raises(ValueError, match="Feedback is newer"):
        asyncio.run(preview_merge(journal, "DEMO-12"))
