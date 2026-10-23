import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from test_persistence import setup
from test_worktree import git
from test_worktree import repository as repository

from dev_agent.dashboard import Dashboard
from dev_agent.dashboard_actions import execute, preview
from dev_agent.workflow.runner import advance


def configured(repository):
    journal, config, provider, registry = setup(repository)
    (repository / ".dev-agent.yaml").write_text(config.model_dump_json())
    git(repository, "add", ".dev-agent.yaml")
    git(
        repository,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "config",
    )
    return journal, config, provider, registry


def request(action, accepted=None, payload=None):
    return dict(action=action, ticket="DEMO-12", accepted=accepted, payload=payload or {})


def test_ui_plan_feedback_approve_revise_merge(repository: Path) -> None:
    journal, config, provider, registry = configured(repository)
    assert asyncio.run(advance(journal, "DEMO-12", config, registry)) == "AWAITING_PLAN_APPROVAL"
    with patch("dev_agent.dashboard_actions.ProviderRegistry", return_value=registry):
        revised = asyncio.run(preview(repository, "DEMO-12", "request-changes"))
        result = asyncio.run(
            execute(repository, request("request-changes", revised, {"text": "Add edge cases"}))
        )
        assert result["state"] == "AWAITING_PLAN_APPROVAL"
        accepted = asyncio.run(preview(repository, "DEMO-12", "approve-plan"))
        result = asyncio.run(execute(repository, request("approve-plan", accepted)))
        assert result["state"] == "READY_FOR_HUMAN_REVIEW"
        # Revise uses existing worktree, then fresh checks and review.
        result = asyncio.run(
            execute(repository, request("revise", payload={"text": "Keep behavior"}))
        )
        assert result["state"] == "READY_FOR_HUMAN_REVIEW"
        accepted = asyncio.run(preview(repository, "DEMO-12", "merge"))
        assert accepted["text"] and accepted["destination"]
        result = asyncio.run(execute(repository, request("merge", accepted)))
        assert result["state"] == "MERGED"
    assert (repository / "app.txt").read_text() == "implemented\n"


def test_stale_plan_and_merge_are_rejected(repository: Path) -> None:
    journal, config, provider, registry = configured(repository)
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    accepted = asyncio.run(preview(repository, "DEMO-12", "approve-plan"))
    path = Path(accepted["plan_path"]) / "plan.md"
    path.write_text(path.read_text().replace("Small change.", "New plan."))
    with patch("dev_agent.dashboard_actions.ProviderRegistry", return_value=registry):
        with pytest.raises(ValueError, match="changed after display"):
            asyncio.run(execute(repository, request("approve-plan", accepted)))
        assert journal.get("DEMO-12")["worktree_path"] is None
        accepted = asyncio.run(preview(repository, "DEMO-12", "approve-plan"))
        asyncio.run(execute(repository, request("approve-plan", accepted)))
        accepted = asyncio.run(preview(repository, "DEMO-12", "merge"))
        Path(journal.get("DEMO-12")["worktree_path"], "app.txt").write_text("tampered")
        with pytest.raises(ValueError):
            asyncio.run(execute(repository, request("merge", accepted)))
    assert (repository / "app.txt").read_text() == "baseline\n"


def test_preview_tokens_and_duplicate_actions(repository: Path) -> None:
    dashboard = Dashboard([repository])
    with pytest.raises(ValueError, match="Preview"):
        dashboard.action("0", "TASK", "save-draft", {"text": "body"})
    accepted = dashboard.make_preview("0", "TASK", "save-draft")
    with pytest.raises(ValueError, match="mismatched"):
        dashboard.action("0", "OTHER", "save-draft", {"text": "body"}, accepted["confirmation"])
    with patch("dev_agent.dashboard.threading.Thread"):
        dashboard.action("0", "TASK", "save-draft", {"text": "body"}, accepted["confirmation"])
        with pytest.raises(ValueError, match="already running"):
            dashboard.action("0", "TASK", "resume", {})
    with pytest.raises(ValueError):
        dashboard.action("0", "../escape", "draft", {})
    recovered = Dashboard([repository]).overview()["jobs"]["0"]
    assert recovered["status"] == "UNKNOWN" and not recovered["running"]


def test_draft_start_binds_reviewed_text(repository: Path) -> None:
    journal, config, provider, registry = configured(repository)
    value = asyncio.run(preview(repository, "NEW", "save-draft"))
    asyncio.run(
        execute(
            repository,
            {
                "action": "save-draft",
                "ticket": "NEW",
                "accepted": value,
                "payload": {"text": "Change app"},
            },
        )
    )
    start = asyncio.run(preview(repository, "NEW", "start"))
    (repository / ".dev-agent/tickets/NEW.md").write_text("changed requirement")
    with pytest.raises(ValueError, match="Draft changed"):
        asyncio.run(execute(repository, dict(action="start", ticket="NEW", accepted=start)))
    assert "NEW" not in [
        r["ticket_id"] for r in Dashboard([repository]).overview()["projects"][0]["tickets"]
    ]


def test_real_background_worker_and_recovery(repository: Path, monkeypatch) -> None:
    import threading

    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    configured(repository)
    dashboard = Dashboard([repository])
    accepted = dashboard.make_preview("0", "UI-SMOKE", "save-draft")
    finished = threading.Event()
    original = dashboard._action

    def worker(key, folder):
        try:
            original(key, folder)
        finally:
            finished.set()

    with patch.object(dashboard, "_action", side_effect=worker):
        dashboard.action(
            "0", "UI-SMOKE", "save-draft", {"text": "Persist this ticket"}, accepted["confirmation"]
        )
        assert finished.wait(20), "worker did not finish"
    job = dashboard.overview()["jobs"]["0"]
    assert job["status"] == "COMPLETE", job
    assert (repository / ".dev-agent/tickets/UI-SMOKE.md").read_text() == "Persist this ticket"
    assert Dashboard([repository]).overview()["jobs"]["0"]["status"] == "COMPLETE"
