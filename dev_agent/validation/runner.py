"""Sequential, fail-fast repository checks with persisted command results."""

import asyncio
import shlex
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import clean_baseline, git
from dev_agent.models.config import CommandsConfig, ValidationConfig
from dev_agent.process import run_process
from dev_agent.workflow.implementation import _verify_identity
from dev_agent.workflow.investigation import _write
from dev_agent.workflow.locking import check_fix_lock
from dev_agent.workflow.planning import (
    _json,
    _local_file,
    approval_is_current,
    investigation_repository,
)


class CommandResult(BaseModel):
    name: str
    command: str
    arguments: list[str]
    cwd: str
    status: Literal["NOT_RUN", "RUNNING", "PASSED", "FAILED", "INTERRUPTED"] = "NOT_RUN"
    started_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None


class ValidationRun(BaseModel):
    run_id: str
    directory: Path
    worktree: Path
    base_commit: str
    plan_sha256: str
    status: Literal["RUNNING", "PASSED", "FAILED", "INTERRUPTED"] = "RUNNING"
    started_at: str
    finished_at: str | None = None
    timeout_seconds: float
    commands: list[CommandResult] = Field(default_factory=list)
    error: str | None = None
    snapshot_sha256: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "PASSED"


def infrastructure_failure(report: ValidationRun) -> bool:
    """Recognize a small set of explicit tool/daemon failures, not test assertions."""
    markers = (
        "cannot connect to the docker daemon",
        "is the docker daemon running?",
        "docker not found. install/start docker desktop",
    )
    return any(
        item.status == "FAILED"
        and (
            item.timed_out
            or item.error
            or any(marker in (item.stdout + item.stderr).lower() for marker in markers)
        )
        for item in report.commands
    )


def failure_summary(report: ValidationRun) -> str:
    parts = []
    for item in report.commands:
        if item.status != "FAILED":
            continue
        details = [
            line.strip()
            for line in item.stdout.splitlines()
            if line.startswith(("E ", "FAILED ", "ERROR ")) or " errors in " in line
        ]
        fallback = [
            line.strip() for line in (item.stderr or item.stdout).splitlines() if line.strip()
        ]
        detail = item.error or (details[-1] if details else (fallback[-1] if fallback else ""))
        parts.append(f"{item.name} failed (exit {item.exit_code}): {detail[:180]}")
    return "; ".join(parts) or report.error or report.status


async def validate(
    directory: Path,
    commands: CommandsConfig,
    config: ValidationConfig,
    *,
    _cycle_token: str | None = None,
) -> ValidationRun:
    directory = directory.resolve(strict=True)
    check_fix_lock(directory, _cycle_token)
    if not await approval_is_current(directory):
        raise ValueError("A current approved plan is required for validation.")
    repository = investigation_repository(directory.parent.parent)
    implementation = _json(_local_file(directory, "implementation.json"))
    worktree = _json(_local_file(directory, "worktree.json"))
    planning = _json(_local_file(directory, "planning.json"))
    approval = _json(_local_file(directory, "approval.json"))
    if implementation.get("status") != "IMPLEMENTED":
        raise ValueError("Validation requires a completed implementation attempt.")
    target_value, branch, base = (
        worktree.get("path"),
        worktree.get("branch"),
        worktree.get("base_commit"),
    )
    if not all(isinstance(value, str) and value for value in (target_value, branch, base)):
        raise ValueError("Invalid worktree record.")
    assert isinstance(target_value, str) and isinstance(branch, str) and isinstance(base, str)
    target = Path(target_value).resolve(strict=True)
    if (
        worktree.get("repository") != str(repository)
        or base != planning.get("base_commit")
        or implementation.get("worktree") != str(target)
        or implementation.get("branch") != branch
        or implementation.get("base_commit") != base
        or implementation.get("plan_sha256") != approval.get("plan_sha256")
        or implementation.get("input_hashes") != approval.get("input_hashes")
    ):
        raise ValueError("Implementation/worktree records do not match the current approval.")
    await _verify_identity(repository, target, branch, base)
    if not await clean_baseline(repository):
        raise ValueError("Source checkout changed; inspect it before validation.")
    if (directory / "implementation.lock").exists():
        raise ValueError("Implementation is still running; validation cannot start.")
    results = []
    for name, command in commands.model_dump().items():
        arguments = shlex.split(command)
        if not arguments:
            raise ValueError(f"Validation command '{name}' must not be empty.")
        results.append(
            CommandResult(name=name, command=command, arguments=arguments, cwd=str(target))
        )
    # Refuse symlinked artifacts/directories before writing either historical or latest results.
    latest = _local_file(directory, "validation.json")
    run_id = uuid4().hex
    output_directory = directory / "validations" / run_id
    if not output_directory.resolve().is_relative_to(directory):
        raise ValueError("Validation artifacts must stay inside the plan directory.")
    report = ValidationRun(
        run_id=run_id,
        directory=output_directory,
        worktree=target,
        base_commit=base,
        plan_sha256=str(approval["plan_sha256"]),
        started_at=datetime.now(UTC).isoformat(),
        timeout_seconds=config.timeout_seconds,
        commands=results,
    )
    lock = directory / "validation.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("Validation in progress; inspect processes before removing this lock.\n")

    def persist() -> None:
        contents = report.model_dump_json(indent=2) + "\n"
        _write(output_directory / "validation.json", contents)
        _write(latest, contents)

    started = False
    try:
        check_fix_lock(directory, _cycle_token)
        if (directory / "review.lock").exists():
            raise ValueError("Review is running; validation cannot overlap it.")
        output_directory.mkdir(parents=True, exist_ok=False)
        persist()
        started = True
        snapshot = await capture_snapshot(target, base)
        report.snapshot_sha256 = snapshot.fingerprint
        persist()
        for item in report.commands:
            # Validate identity/approval between checks, not just at initial entry.
            await _verify_identity(repository, target, branch, base)
            if not await approval_is_current(directory):
                raise ValueError("Plan approval changed during validation.")
            if not await clean_baseline(repository):
                raise ValueError("Source checkout changed during validation.")
            if await git(target, "diff", "--cached", "--name-only", "-z", "--"):
                raise ValueError("Validation found staged changes; inspect the worktree.")
            item.status = "RUNNING"
            item.started_at = datetime.now(UTC).isoformat()
            persist()
            began = monotonic()
            try:
                result = await run_process(
                    item.arguments, cwd=target, timeout=config.timeout_seconds
                )
                item.started_at = result.started_at.isoformat()
                item.duration_seconds = result.duration_seconds
                item.exit_code = result.exit_code
                item.stdout, item.stderr = result.stdout, result.stderr
                item.timed_out = result.timed_out
                item.status = (
                    "PASSED" if result.exit_code == 0 and not result.timed_out else "FAILED"
                )
                if result.timed_out:
                    item.error = "Command timed out."
            except OSError as exc:
                item.status = "FAILED"
                item.error = (
                    f"Command could not start ({type(exc).__name__}). Check executable and cwd."
                )
                item.duration_seconds = monotonic() - began
            except asyncio.CancelledError:
                item.status = "INTERRUPTED"
                item.duration_seconds = monotonic() - began
                raise
            persist()
            if (await capture_snapshot(target, base)).fingerprint != report.snapshot_sha256:
                raise ValueError("Worktree content changed during validation; inspect and rerun.")
            if item.status != "PASSED":
                report.status = "FAILED"
                break
        else:
            report.status = "PASSED"
        await _verify_identity(repository, target, branch, base)
        if not await approval_is_current(directory) or not await clean_baseline(repository):
            raise ValueError("Approval or source checkout changed during validation.")
        if await git(target, "diff", "--cached", "--name-only", "-z", "--"):
            raise ValueError("Validation found staged changes; inspect the worktree.")
        report.finished_at = datetime.now(UTC).isoformat()
        persist()
        return report
    except BaseException as exc:
        if started:
            report.status = "INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
            report.error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            report.finished_at = datetime.now(UTC).isoformat()
            persist()
        raise
    finally:
        lock.unlink(missing_ok=True)
