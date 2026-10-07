"""Execute one approved implementation attempt inside its isolated worktree."""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from dev_agent.git.worktree import clean_baseline, git
from dev_agent.models.agent import AgentTask
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.investigation import _write
from dev_agent.workflow.knowledge import knowledge_context
from dev_agent.workflow.locking import check_fix_lock
from dev_agent.workflow.planning import (
    _json,
    _local_file,
    approval_is_current,
    checked_plan,
    investigation_repository,
)


@dataclass(frozen=True)
class ImplementationRun:
    directory: Path
    success: bool
    changed_files: list[str]


async def _verify_identity(repository: Path, target: Path, branch: str, base: str) -> None:
    if (
        target == repository
        or target.is_relative_to(repository)
        or repository.is_relative_to(target)
    ):
        raise ValueError(
            "Implementation requires an isolated worktree outside the source checkout."
        )
    root = Path(await git(target, "rev-parse", "--show-toplevel")).resolve()
    common = Path(await git(target, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    expected = Path(
        await git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    if root != target or common.resolve() != expected.resolve():
        raise ValueError("Worktree does not belong to the recorded repository.")
    entries = (await git(repository, "worktree", "list", "--porcelain", "-z")).split("\x00")
    if f"worktree {target}" not in entries:
        raise ValueError("Worktree is not registered with the source repository.")
    if await git(target, "symbolic-ref", "--quiet", "--short", "HEAD") != branch:
        raise ValueError("Worktree branch changed; inspect it before implementation.")
    if await git(target, "rev-parse", "HEAD") != base:
        raise ValueError("Worktree HEAD differs from the approved base commit.")


async def _changed_files(target: Path, base: str) -> list[str]:
    tracked = await git(target, "diff", "--name-only", "-z", base, "--")
    untracked = await git(target, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set(name for name in (tracked + untracked).split("\x00") if name))


async def change_warnings(target: Path, base: str) -> list[str]:
    """Flag unusually destructive tracked-file changes for independent inspection."""
    stats = await git(target, "diff", "--numstat", "--no-renames", base, "--")
    warnings = []
    for line in stats.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        added, removed = int(fields[0]), int(fields[1])
        if removed >= 30 and removed > added * 2:
            warnings.append(
                f"{fields[2]}: {removed} lines removed, {added} added; "
                "verify unrelated content was preserved."
            )
    return warnings


def _instructions(target: Path) -> dict[str, str]:
    result = {}
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = _local_file(target, name)
        if path.exists():
            result[name] = path.read_text(encoding="utf-8")
    return result


async def implement(directory: Path, registry: ProviderRegistry) -> ImplementationRun:
    directory = directory.resolve(strict=True)
    check_fix_lock(directory)
    if not await approval_is_current(directory):
        raise ValueError("A current human plan approval is required.")
    run = directory.parent.parent
    repository = investigation_repository(run)
    metadata = _json(_local_file(directory, "planning.json"))
    record = _json(_local_file(directory, "worktree.json"))
    if metadata.get("clean_baseline") is not True or record.get("base_commit") != metadata.get(
        "base_commit"
    ):
        raise ValueError("Worktree record does not match the approved clean baseline.")
    if record.get("repository") != str(repository):
        raise ValueError("Worktree repository record does not match the plan.")
    raw_target, branch, base = record.get("path"), record.get("branch"), record.get("base_commit")
    if not all(isinstance(value, str) and value for value in (raw_target, branch, base)):
        raise ValueError("Invalid worktree path, branch or commit metadata.")
    assert isinstance(raw_target, str) and isinstance(branch, str) and isinstance(base, str)
    target = Path(raw_target).resolve(strict=True)
    await _verify_identity(repository, target, branch, base)
    if not await clean_baseline(repository):
        raise ValueError(
            "Source checkout has uncommitted changes; preserve them before implementation."
        )
    if await git(target, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Worktree has existing changes; inspect them rather than overwriting.")
    plan = await checked_plan(directory)
    bundle = {
        "ticket": _local_file(run, "ticket.md").read_text(encoding="utf-8"),
        "approved_plan": plan,
        "repository_instructions": _instructions(target),
        "knowledge": knowledge_context(directory, registry.config, investigation=True),
    }
    prompt = files("dev_agent").joinpath("prompts/implement.md").read_text(encoding="utf-8")
    task = AgentTask(
        role="implementer",
        prompt=prompt + "\nInput bundle:\n" + json.dumps(bundle),
        working_directory=target,
        read_only=False,
    )
    output = _local_file(directory, "implementation.json")
    if output.exists():
        raise ValueError(
            "Implementation attempt already recorded; inspect it. Automatic retry is disabled."
        )
    approval = _json(_local_file(directory, "approval.json"))
    lock = directory / "implementation.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("Implementation in progress; do not remove while the agent is running.\n")
    data: dict[str, object] = {
        "status": "RUNNING",
        "worktree": str(target),
        "branch": branch,
        "base_commit": base,
        "started_at": datetime.now(UTC).isoformat(),
        "validation_status": "NOT_RUN",
        "changed_files": [],
        "agent_summary": "",
        "summary_verified": False,
    }
    data["plan_sha256"] = approval.get("plan_sha256")
    data["input_hashes"] = approval.get("input_hashes")
    started = False
    try:
        check_fix_lock(directory)
        if output.exists():
            raise ValueError("Implementation attempt already recorded.")
        if not await approval_is_current(directory) or await checked_plan(directory) != plan:
            raise ValueError("Approval changed before implementation.")
        await _verify_identity(repository, target, branch, base)
        if await git(target, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("Worktree changed before implementation.")
        _write(output, json.dumps(data, indent=2) + "\n")
        started = True
        result = await registry.execute("implement", directory, task)
        changes = await _changed_files(target, base)
        data.update(
            {
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
                "agent_summary": result.output,
                "changed_files": changes,
                "change_warnings": await change_warnings(target, base),
                "provider_error": result.error,
            }
        )
        await _verify_identity(repository, target, branch, base)
        if await git(target, "diff", "--cached", "--name-only", "-z", "--"):
            raise ValueError("Agent staged changes; inspect the worktree manually.")
        if not await clean_baseline(repository):
            raise ValueError("Source checkout changed during implementation; inspect it manually.")
        if not await approval_is_current(directory) or await checked_plan(directory) != plan:
            raise ValueError(
                "Approval or plan changed during implementation; human inspection required."
            )
        success = result.success and result.exit_code == 0 and not result.timed_out
        data["status"] = "IMPLEMENTED" if success else "FAILED"
        if not success:
            data["failure_reason"] = "provider_timeout" if result.timed_out else "provider_failed"
        data["finished_at"] = datetime.now(UTC).isoformat()
        _write(output, json.dumps(data, indent=2) + "\n")
        return ImplementationRun(directory, success, changes)
    except BaseException as exc:
        if started:
            data["status"] = "INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
            data["error_type"] = type(exc).__name__
            data["finished_at"] = datetime.now(UTC).isoformat()
            try:
                data["changed_files"] = await _changed_files(target, base)
            except (OSError, ValueError):
                data["changed_files_available"] = False
            _write(output, json.dumps(data, indent=2) + "\n")
        raise
    finally:
        lock.unlink(missing_ok=True)
