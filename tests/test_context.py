import asyncio
import json
import subprocess

import pytest

from dev_agent.context import digest, finish, initialize, prepare, read, save, save_seed
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.models.agent import AgentTask
from dev_agent.models.config import ContextConfig


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "app.py").write_text("old implementation\n")
    (tmp_path / "test_app.py").write_text("old test\n")
    (tmp_path / ".gitignore").write_text(".dev-agent/\n.env\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "baseline",
        ],
        check=True,
        capture_output=True,
    )
    return tmp_path


def base(repo):
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def run_dir(repo):
    path = repo / ".dev-agent/runs/TICKET-1/run1"
    path.mkdir(parents=True)
    (path / "ticket.md").write_text("the ticket")
    return path


def seed(repo):
    sha = base(repo)
    snapshot = asyncio.run(capture_snapshot(repo, sha))
    asyncio.run(
        save_seed(
            repo,
            "TICKET-1",
            "the ticket",
            {
                "current_behavior": "app.py:1 contains the old implementation",
                "expected_files": ["app.py", "test_app.py", ".env", "../outside"],
            },
            sha,
            snapshot.content_fingerprint,
        )
    )


def request(repo):
    return AgentTask(role="implementer", prompt="Implement ticket.", working_directory=repo)


def populate(repo, run):
    asyncio.run(initialize(repo, "TICKET-1", "the ticket", run, ContextConfig()))
    for name, data in {
        "exploration.json": {"important_files": ["app.py:1"], "risks": ["Check compatibility"]},
        "patterns.json": {"patterns": [{"files": ["app.py"], "description": "Pattern hint"}]},
        "tests.json": {
            "existing_tests": ["test_app.py:1"],
            "recommended_tests": ["Add regression"],
        },
    }.items():
        (run / name).write_text(json.dumps(data))
    asyncio.run(finish(repo, run, ContextConfig()))


def test_generated_seed_is_reused_and_sensitive_paths_not_indexed(repo):
    (repo / ".env").write_text("SECRET")
    seed(repo)
    run = run_dir(repo)
    asyncio.run(initialize(repo, "TICKET-1", "the ticket", run, ContextConfig()))
    task, info = prepare(request(repo), run, "investigate", ContextConfig())
    assert info["used"] and info["origin"] == "ticket_generation"
    assert "app.py" in task.prompt and "SECRET" not in task.prompt
    assert ".env" not in task.prompt and "../outside" not in task.prompt


@pytest.mark.parametrize("change", ["ticket", "source", "new_file"])
def test_seed_invalidates_when_inputs_change(repo, change):
    seed(repo)
    ticket = "the ticket"
    if change == "ticket":
        ticket += " updated"
    elif change == "source":
        (repo / "app.py").write_text("new implementation")
    else:
        (repo / "new.py").write_text("new dependency")
    run = run_dir(repo)
    asyncio.run(initialize(repo, "TICKET-1", ticket, run, ContextConfig()))
    assert read(run / "context.json")["origin"] == "none"


def test_research_reaches_later_roles_and_changed_worktree_files_are_flagged(repo):
    run = run_dir(repo)
    populate(repo, run)
    plan = run / "plans/plan1"
    plan.mkdir(parents=True)
    for stage in ["implement", "review", "fix", "revise"]:
        task, info = prepare(request(repo), plan, stage, ContextConfig())
        assert info["used"] and info["changed_files"] == 0
        assert "Pattern hint" in task.prompt and "Add regression" in task.prompt
        assert "independently verify" in task.prompt
    (repo / "app.py").write_text("new implementation")
    task, info = prepare(request(repo), plan, "review", ContextConfig())
    assert info["changed_files"] == 1 and '"state": "changed"' in task.prompt


def test_do_not_duplicate_planner_inputs_or_enabled_knowledge(repo):
    run = run_dir(repo)
    populate(repo, run)
    for stage in ["plan", "plan_review", "plan_revision", "ticket"]:
        task, info = prepare(request(repo), run, stage, ContextConfig())
        assert not info["used"] and task.prompt == request(repo).prompt
    task, info = prepare(request(repo), run, "implement", ContextConfig(enabled=False))
    assert not info["used"] and task.prompt == request(repo).prompt


def test_budget_keeps_whole_json_and_old_runs_fall_back(repo):
    run = run_dir(repo)
    assert not prepare(request(repo), run, "implement", ContextConfig())[1]["used"]
    populate(repo, run)
    data = read(run / "context.json")
    data["evidence"] *= 100
    save(run / "context.json", data)
    task, info = prepare(request(repo), run, "implement", ContextConfig(max_characters=2000))
    assert info["used"] and len(task.prompt) - len(request(repo).prompt) <= 2000
    json.loads(task.prompt.split("Shared ticket context:\n")[1])


def test_modified_research_is_not_silently_reused(repo):
    run = run_dir(repo)
    populate(repo, run)
    (run / "tests.json").write_text('{"recommended_tests":["different"]}')
    _, info = prepare(request(repo), run, "implement", ContextConfig())
    assert not info["used"] and info["reason"] == "research_changed"


def test_repository_change_during_investigation_discards_context(repo):
    run = run_dir(repo)
    asyncio.run(initialize(repo, "TICKET-1", "the ticket", run, ContextConfig()))
    (repo / "app.py").write_text("concurrent edit")
    asyncio.run(finish(repo, run, ContextConfig()))
    assert read(run / "context.json")["origin"] == "none"


def test_other_ticket_and_symlink_cannot_supply_shared_context(repo):
    seed(repo)
    run = run_dir(repo)
    asyncio.run(initialize(repo, "OTHER", "the ticket", run, ContextConfig()))
    assert read(run / "context.json")["origin"] == "none"
    target = repo / "outside.json"
    target.write_text(
        json.dumps({"origin": "ticket_generation", "ticket_sha256": digest("the ticket")})
    )
    (run / "context.json").unlink()
    (run / "context.json").symlink_to(target)
    assert not prepare(request(repo), run, "implement", ContextConfig())[1]["used"]


def test_json_input_bundle_remains_one_document(repo):
    run = run_dir(repo)
    populate(repo, run)
    original = request(repo).model_copy(
        update={
            "prompt": "Instructions\nInput bundle:\n"
            + json.dumps({"ticket": "text", "validation": {"passed": False}})
        }
    )
    task, info = prepare(original, run, "fix", ContextConfig())
    bundle = json.loads(task.prompt.split("Input bundle:\n", 1)[1])
    assert bundle["ticket"] == "text" and bundle["validation"] == {"passed": False}
    assert bundle["shared_ticket_context"]["context"]["origin"] == "investigation"
    assert info["used"] and len(task.prompt) - len(original.prompt) <= 4000


def test_corrupt_context_shape_falls_back(repo):
    run = run_dir(repo)
    save(
        run / "context.json",
        {"ticket_sha256": digest("the ticket"), "origin": "investigation", "files": 123},
    )
    task, info = prepare(request(repo), run, "review", ContextConfig())
    assert not info["used"] and task.prompt == request(repo).prompt
