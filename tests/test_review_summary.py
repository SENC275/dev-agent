import asyncio
import json
from pathlib import Path

import pytest
from test_persistence import setup
from test_worktree import repository as repository
from typer.testing import CliRunner

from dev_agent.cli import app
from dev_agent.review_summary import render, summarize
from dev_agent.workflow.planning import approve_plan
from dev_agent.workflow.runner import advance


def complete(repo):
    journal, config, provider, registry = setup(repo)
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    plan = Path(journal.get("DEMO-12")["plan_path"])
    asyncio.run(approve_plan(plan, (plan / "plan.md").read_text()))
    asyncio.run(advance(journal, "DEMO-12", config, registry))
    return journal, provider, plan, Path(journal.get("DEMO-12")["worktree_path"])


def test_completed_summary_and_json_are_readonly(repository, monkeypatch):
    journal, provider, plan, target = complete(repository)
    before = journal.get("DEMO-12")
    calls = len(provider.tasks)
    data = asyncio.run(summarize(journal, "DEMO-12"))
    assert data["merge_ready"] and data["validation"]["current"] and data["review"]["current"]
    assert data["review"]["findings"] == []
    assert data["files"] == [{"status": "M", "path": "app.txt"}]
    assert data["usage"]["input_tokens"] is None
    assert "unknown" in render(data) and "dev-agent revise DEMO-12" in render(data)
    monkeypatch.chdir(repository)
    output = CliRunner().invoke(app, ["summary", "DEMO-12", "--json"])
    assert output.exit_code == 0, output.output
    assert json.loads(output.output)["merge_ready"]
    assert journal.get("DEMO-12") == before and len(provider.tasks) == calls
    assert (target / "app.txt").read_text() == "implemented\n"


def test_code_changes_mark_old_success_stale_and_include_untracked(repository):
    journal, _, plan, target = complete(repository)
    (target / "app.txt").write_text("changed after validation")
    (target / "new file.txt").write_text("not in git diff")
    data = asyncio.run(summarize(journal, "DEMO-12"))
    assert not data["merge_ready"]
    assert not data["validation"]["current"] and not data["review"]["current"]
    assert {"status": "?", "path": "new file.txt"} in data["files"]
    assert "STALE" in render(data)
    assert "dev-agent merge DEMO-12" not in data["commands"]
    assert "omits untracked" in render(data)


@pytest.mark.parametrize("name", ["validation.json", "review.json", "review-status.json"])
def test_missing_corrupt_artifacts_never_claim_all_passed(repository, name):
    journal, _, plan, _ = complete(repository)
    (plan / name).write_text("bad json")
    data = asyncio.run(summarize(journal, "DEMO-12"))
    assert not data["merge_ready"] and data["warnings"]
    if name == "validation.json":
        assert data["validation"] is None
    else:
        assert data["review"] is None


def test_partial_usage_and_recorded_findings(repository):
    journal, _, plan, _ = complete(repository)
    # A historical finding is shown, but tampering with the latest copy cannot grant current status.
    finding = {"severity": "high", "category": "correctness", "file": "app.txt", "line": 1,
               "scenario": "Missing edge case", "impact": "Wrong output",
               "recommendation": "Handle empty input"}
    (plan / "review.json").write_text(json.dumps({"findings": [finding]}))
    records = list((repository / ".dev-agent").rglob("usage/*.json"))
    payload = json.loads(records[0].read_text())
    payload["usage"].update(input_tokens=100, output_tokens=10,
                            cache_read_tokens=80, cache_write_tokens=5, complete=True)
    records[0].write_text(json.dumps(payload))
    data = asyncio.run(summarize(journal, "DEMO-12"))
    assert data["usage"]["input_tokens"] == 100  # Do not add cached input twice.
    assert not data["usage"]["input_tokens_complete"]
    assert "known subtotal" in render(data)
    assert "Missing edge case" in render(data) and "Handle empty input" in render(data)
    assert not data["review"]["current"] and not data["merge_ready"]


def test_before_worktree_and_unknown_ticket(repository, monkeypatch):
    journal, _, _, _ = setup(repository)
    data = asyncio.run(summarize(journal, "DEMO-12"))
    assert data["validation"] is None and data["review"] is None
    assert data["usage"]["input_tokens"] is None
    assert "dev-agent resume DEMO-12" in data["commands"]
    monkeypatch.chdir(repository)
    assert CliRunner().invoke(app, ["summary", "missing"]).exit_code == 1
