"""Create new worktrees without replacing branches or developer files."""

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from string import Formatter

from dev_agent.models.config import GitConfig
from dev_agent.process import run_process


async def git(repository: Path, *arguments: str) -> str:
    result = await run_process(["git", *arguments], cwd=repository, timeout=60)
    if result.timed_out or result.exit_code != 0:
        raise ValueError(
            f"Git {arguments[0]} failed (exit {result.exit_code}, "
            f"timeout={result.timed_out}). {result.stderr.strip()}"
        )
    return result.stdout.rstrip("\n")


async def clean_baseline(repository: Path) -> bool:
    """All tracked changes count; only untracked run artifacts are excluded."""
    dirty = False
    for options in ([], ["--cached"]):
        tracked = await run_process(
            ["git", "diff", "--quiet", "--ignore-submodules=none", *options, "--"],
            cwd=repository,
        )
        if tracked.timed_out or tracked.exit_code not in (0, 1):
            raise ValueError("Cannot check tracked Git changes.")
        dirty = dirty or tracked.exit_code != 0
    untracked = await git(
        repository,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
        ":(exclude).dev-agent/**",
    )
    return not dirty and not untracked


def _render(pattern: str, ticket: str) -> str:
    try:
        for _, field, spec, conversion in Formatter().parse(pattern):
            if field is not None and (field != "ticket" or spec or conversion):
                raise ValueError("Only the {ticket} placeholder is supported.")
        value = pattern.format(ticket=ticket)
    except (KeyError, IndexError) as exc:
        raise ValueError("Invalid Git naming template.") from exc
    if not value.strip() or "\x00" in value:
        raise ValueError("Git naming templates must produce nonempty values without NUL.")
    return value


@dataclass(frozen=True)
class WorktreeResult:
    repository: str
    path: str
    branch: str
    base_commit: str


async def create_worktree(
    repository: Path,
    *,
    ticket: str,
    base_commit: str,
    config: GitConfig,
) -> WorktreeResult:
    repository = repository.resolve(strict=True)
    root = Path(await git(repository, "rev-parse", "--show-toplevel")).resolve()
    if root != repository:
        raise ValueError("Worktree creation requires the repository root.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", ticket):
        raise ValueError("Invalid ticket ID.")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_commit):
        raise ValueError("Expected a full Git commit ID.")
    if await git(repository, "rev-parse", "HEAD") != base_commit:
        raise ValueError("Git HEAD changed; generate and approve a new plan.")
    if not await clean_baseline(repository):
        raise ValueError(
            "Repository has uncommitted changes; commit or preserve them manually first."
        )
    branch = _render(config.branch_pattern, ticket)
    if branch.startswith("-"):
        raise ValueError("Branch name cannot start with '-'.")
    await git(repository, "check-ref-format", f"refs/heads/{branch}")
    target_raw = repository / _render(config.worktree_directory, ticket)
    if target_raw.exists() or target_raw.is_symlink():
        raise ValueError(f"Worktree path already exists: {target_raw}")
    target = target_raw.resolve()
    registered = await git(repository, "worktree", "list", "--porcelain", "-z")
    for field in registered.split("\x00"):
        if field.startswith("worktree "):
            existing = Path(field.removeprefix("worktree ")).resolve()
            if target.is_relative_to(existing) or existing.is_relative_to(target):
                raise ValueError("Worktree path overlaps an existing worktree.")
    refs = await git(repository, "for-each-ref", "--format=%(refname)", "refs/heads/")
    if f"refs/heads/{branch}" in refs.splitlines():
        raise ValueError(f"Branch already exists: {branch}")
    # Reserve the directory exclusively; never reuse an existing empty directory.
    target.mkdir(parents=True, exist_ok=False)
    # Disable checkout hooks; creation should not execute project-specific scripts.
    # Failures preserve partial state for manual inspection, never destructive cleanup.
    await git(
        repository,
        "-c",
        f"core.hooksPath={os.devnull}",
        "worktree",
        "add",
        "-b",
        branch,
        "--",
        str(target),
        base_commit,
    )
    if await git(target, "rev-parse", "HEAD") != base_commit:
        raise ValueError("Created worktree verification failed; inspect it manually.")
    return WorktreeResult(str(repository), str(target), branch, base_commit)


async def create_approved_worktree(directory: Path, config: GitConfig) -> WorktreeResult:
    # Import here to keep the low-level Git helper usable by planning.
    from dev_agent.workflow.investigation import _write
    from dev_agent.workflow.planning import (
        _json,
        _local_file,
        approval_is_current,
        investigation_repository,
    )

    directory = directory.resolve(strict=True)
    if not await approval_is_current(directory):
        raise ValueError("A current human plan approval is required.")
    metadata = _json(_local_file(directory, "planning.json"))
    if metadata.get("clean_baseline") is not True:
        raise ValueError(
            "Plan lacks a clean Git baseline; regenerate and approve from a clean tree."
        )
    run = directory.parent.parent
    repository = investigation_repository(run)
    info = _json(_local_file(run, "investigation.json"))
    ticket, base = info.get("ticket_id"), metadata.get("base_commit")
    if not isinstance(ticket, str) or not isinstance(base, str):
        raise ValueError("Missing ticket ID or base commit.")
    record = _local_file(directory, "worktree.json")
    if record.exists():
        raise ValueError(
            "This plan already has a worktree record; inspect it instead of recreating."
        )
    # Exclusive lock prevents two calls for one plan from creating different trees.
    lock = directory / "worktree.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("Worktree creation in progress. Inspect Git state before retrying.\n")
    try:
        if record.exists():
            raise ValueError("This plan already has a worktree record.")
        if not await approval_is_current(directory):
            raise ValueError("Plan approval changed before worktree creation.")
        result = await create_worktree(repository, ticket=ticket, base_commit=base, config=config)
        _write(record, json.dumps(asdict(result), indent=2) + "\n")
        return result
    finally:
        lock.unlink(missing_ok=True)
