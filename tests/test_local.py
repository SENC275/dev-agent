import asyncio
from pathlib import Path

from test_worktree import git

from dev_agent.config import initialize_config, load_config
from dev_agent.git.worktree import clean_baseline
from dev_agent.local import protect_local_files


def test_local_files_do_not_enter_git_add(tmp_path: Path):
    git(tmp_path, "init")
    (tmp_path / "app.txt").write_text("code")
    git(tmp_path, "add", ".")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "baseline",
    )
    path = initialize_config(tmp_path)
    assert not load_config(path).knowledge.enabled
    records = tmp_path / ".dev-agent/tickets"
    records.mkdir(parents=True)
    (records / "DEMO.md").write_text("local ticket")
    assert asyncio.run(clean_baseline(tmp_path))
    git(tmp_path, "add", ".")
    assert git(tmp_path, "status", "--porcelain") == ""
    assert not (tmp_path / ".gitignore").exists()
    exclude = tmp_path / ".git/info/exclude"
    original = exclude.read_text()
    protect_local_files(tmp_path)
    assert exclude.read_text() == original
    # Shared exclusion rules also cover linked worktrees.
    target = tmp_path.parent / (tmp_path.name + "-worktree")
    git(tmp_path, "worktree", "add", "-b", "test-local", str(target))
    (target / ".dev-agent.yaml").write_text("{}")
    (target / ".dev-agent").mkdir()
    (target / ".dev-agent/result.json").write_text("{}")
    protect_local_files(target)
    assert git(target, "status", "--porcelain") == ""
