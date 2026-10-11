import asyncio
import json

import pytest
from test_persistence import setup
from test_review_summary import complete
from test_worktree import git
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.corpus import collect, inspect, verify_patch


def entry(content, mode="100644"):
    return {"mode": mode, "content": content}


def test_replay_add_delete_empty_no_newline_and_mode():
    before = {
        "old.txt": entry("delete\n"),
        "script.sh": entry("old"),
        "folder/file.txt": entry("before\n"),
    }
    after = {
        "script.sh": entry("new", "100755"),
        "empty.txt": entry(""),
        "new file.txt": entry("a\r\nb\r\n"),
        "folder/file.txt": entry("after\n"),
    }
    patch = verify_patch(before, after)
    assert "new file mode" in patch and "deleted file mode" in patch
    assert verify_patch(before, after, patch) == patch
    with pytest.raises(ValueError):
        verify_patch(before, after, patch.replace("+new", "+wrong"))


def test_collect_replay_is_immutable_and_does_not_call_models(repository):
    journal, provider, _, target = complete(repository)
    calls, state = len(provider.tasks), journal.get("DEMO-12")
    first = asyncio.run(collect(journal, "DEMO-12"))
    original = (first / "bundle.json").read_bytes()
    result = inspect(journal, "DEMO-12")
    assert result["patch_replay"] == "verified"
    assert result["quality"]["validation"] == "PASSED"
    assert result["quality"]["review"] == "REVIEWED"
    assert result["quality"]["human_acceptance"] == "unknown"
    assert not result["quality"]["training_eligible"]
    assert result["provenance"] == "reconstructed"
    assert "app.txt" in result["changed_files"]
    second = asyncio.run(collect(journal, "DEMO-12"))
    assert first != second and (first / "bundle.json").read_bytes() == original
    assert inspect(journal, "DEMO-12")["samples"] == 2
    assert len(provider.tasks) == calls and journal.get("DEMO-12") == state
    assert (target / "app.txt").read_text() == "implemented\n"
    assert not git(repository, "status", "--porcelain")


def test_untracked_empty_file_and_stale_evidence(repository):
    journal, _, _, target = complete(repository)
    (target / "new empty.txt").write_text("")
    path = asyncio.run(collect(journal, "DEMO-12"))
    data = json.loads((path / "bundle.json").read_text())
    assert "new empty.txt" in data["after"]
    report = inspect(journal, "DEMO-12")
    assert report["patch_replay"] == "verified"
    assert report["quality"]["validation"] == "unknown"
    assert "passing_validation_not_bound_to_snapshot" in report["gaps"]


def test_missing_artifacts_and_no_worktree_make_partial_candidate(repository):
    journal, _, _, _ = setup(repository)
    asyncio.run(collect(journal, "DEMO-12"))
    report = inspect(journal, "DEMO-12")
    assert report["patch_replay"] == "unavailable"
    assert "missing_plan_or_worktree" in report["gaps"]


def test_secret_is_not_copied_and_not_silently_rewritten(repository):
    journal, _, _, target = complete(repository)
    secret = "sk-EXAMPLEPRIVATEVALUE"
    (target / "app.txt").write_text("api_key=" + secret)
    path = asyncio.run(collect(journal, "DEMO-12"))
    raw = (path / "bundle.json").read_text()
    assert secret not in raw
    report = inspect(journal, "DEMO-12")
    assert report["patch_replay"] == "unavailable"
    assert "changed_file_withheld:app.txt" in report["gaps"]
    assert (target / "app.txt").read_text() == "api_key=" + secret


def test_tampering_and_symlinks_rejected(repository, tmp_path):
    journal, _, _, _ = complete(repository)
    path = asyncio.run(collect(journal, "DEMO-12"))
    (path / "bundle.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        inspect(journal, "DEMO-12")
    (path / "bundle.json").unlink()
    outside = tmp_path / "outside"
    outside.write_text("private")
    (path / "bundle.json").symlink_to(outside)
    with pytest.raises(ValueError, match="Unsafe"):
        inspect(journal, "DEMO-12")
    assert outside.read_text() == "private"


@pytest.mark.parametrize("name", ["../escape", "/absolute", ".git/config", ".GIT/config", ".env"])
def test_replay_rejects_unsafe_paths(name):
    with pytest.raises(ValueError):
        verify_patch({}, {name: entry("bad")})


def test_cli_and_unknown_ticket(repository, monkeypatch):
    journal, _, _, _ = setup(repository)
    monkeypatch.chdir(repository)
    runner = CliRunner()
    assert runner.invoke(app, ["corpus", "inspect", "DEMO-12"]).exit_code == 1
    saved = runner.invoke(app, ["corpus", "collect", "DEMO-12"])
    assert saved.exit_code == 0, saved.output
    result = runner.invoke(app, ["corpus", "inspect", "DEMO-12"])
    assert result.exit_code == 0 and json.loads(result.output)["integrity"] == "verified"
    assert runner.invoke(app, ["corpus", "collect", "missing"]).exit_code == 1


def test_saved_sample_replays_without_original_worktree(repository):
    journal, _, _, target = complete(repository)
    asyncio.run(collect(journal, "DEMO-12"))
    git(repository, "worktree", "remove", "--force", str(target))
    assert inspect(journal, "DEMO-12")["patch_replay"] == "verified"


def test_recorded_merge_acceptance(repository):
    from dev_agent.workflow.merging import merge, preview_merge

    journal, _, _, _ = complete(repository)
    preview = asyncio.run(preview_merge(journal, "DEMO-12"))
    asyncio.run(merge(journal, "DEMO-12", preview, "Accept demo"))
    asyncio.run(collect(journal, "DEMO-12"))
    report = inspect(journal, "DEMO-12")
    assert report["patch_replay"] == "verified"
    assert report["quality"]["human_acceptance"] == "recorded_merge"
    assert not report["quality"]["training_eligible"]


@pytest.mark.parametrize("name", ["validation.json", "review.json", "approval.json"])
def test_corrupt_evidence_cannot_claim_validated(repository, name):
    journal, _, plan, _ = complete(repository)
    (plan / name).write_text("bad json")
    asyncio.run(collect(journal, "DEMO-12"))
    report = inspect(journal, "DEMO-12")
    assert report["quality"]["validation"] != "PASSED" or report["quality"]["review"] != "REVIEWED"
    assert not report["quality"]["training_eligible"]
