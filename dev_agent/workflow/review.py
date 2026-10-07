"""Independent read-only review bound to current approval, code and validation."""

import asyncio
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from dev_agent.artifacts import parse_artifact
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import clean_baseline
from dev_agent.models.agent import AgentTask
from dev_agent.models.finding import Review
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.validation.runner import ValidationRun
from dev_agent.workflow.feedback import feedback_history
from dev_agent.workflow.implementation import _verify_identity, change_warnings
from dev_agent.workflow.investigation import _write
from dev_agent.workflow.knowledge import knowledge_context, knowledge_findings
from dev_agent.workflow.locking import check_fix_lock
from dev_agent.workflow.planning import (
    _json,
    _local_file,
    approval_is_current,
    checked_plan,
    investigation_repository,
)


async def review(
    directory: Path, registry: ProviderRegistry, *, _cycle_token: str | None = None
) -> Path:
    directory = directory.resolve(strict=True)
    check_fix_lock(directory, _cycle_token)
    if not await approval_is_current(directory):
        raise ValueError("A current plan approval is required.")
    repository = investigation_repository(directory.parent.parent)
    record = _json(_local_file(directory, "worktree.json"))
    approval = _json(_local_file(directory, "approval.json"))
    base, branch, raw = record.get("base_commit"), record.get("branch"), record.get("path")
    if not all(isinstance(value, str) and value for value in (base, branch, raw)):
        raise ValueError("Invalid worktree record.")
    assert isinstance(base, str) and isinstance(branch, str) and isinstance(raw, str)
    target = Path(raw).resolve(strict=True)
    if record.get("repository") != str(repository) or base != approval.get("base_commit"):
        raise ValueError("Worktree record does not match approval.")
    await _verify_identity(repository, target, branch, base)
    if not await clean_baseline(repository):
        raise ValueError("Source checkout changed; inspect before review.")
    if any((directory / name).exists() for name in ("implementation.lock", "validation.lock")):
        raise ValueError("Implementation or validation is still running.")
    validation_path = _local_file(directory, "validation.json")
    validation_bytes = validation_path.read_bytes()
    validation = ValidationRun.model_validate_json(validation_bytes)
    if (
        not validation.success
        or validation.snapshot_sha256 is None
        or validation.worktree != target
        or validation.base_commit != base
        or validation.plan_sha256 != approval.get("plan_sha256")
        or [item.name for item in validation.commands] != ["test", "lint", "typecheck"]
        or any(
            item.status != "PASSED" or item.exit_code != 0 or item.timed_out
            for item in validation.commands
        )
    ):
        raise ValueError(
            "Current successful validation with a code fingerprint is required; rerun validate."
        )
    snapshot = await capture_snapshot(target, base)
    if snapshot.fingerprint != validation.snapshot_sha256:
        raise ValueError("Code changed since validation; rerun validate before review.")
    plan = await checked_plan(directory)
    prompt = files("dev_agent").joinpath("prompts/review.md").read_text(encoding="utf-8")
    bundle = {
        "ticket": _local_file(directory.parent.parent, "ticket.md").read_text(encoding="utf-8"),
        "approved_plan": plan,
        "human_feedback": feedback_history(directory),
        "feedback_policy": (
            "Human feedback amends the approved plan; "
            "later feedback takes precedence where requirements conflict."
        ),
        "git_diff": snapshot.diff,
        "change_warnings": await change_warnings(target, base),
        "validation": validation.model_dump(mode="json"),
        "schema": Review.model_json_schema(),
        "knowledge": knowledge_context(directory, registry.config),
    }
    output = directory / "reviews" / uuid4().hex
    if not output.resolve().is_relative_to(directory):
        raise ValueError("Review artifacts must stay inside the plan directory.")
    latest = _local_file(directory, "review-status.json")
    latest_review = _local_file(directory, "review.json")
    lock = directory / "review.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("Review running. Inspect the process before removing this lock.\n")
    metadata: dict[str, object] = {
        "status": "RUNNING",
        "started_at": datetime.now(UTC).isoformat(),
        "review_directory": str(output),
        "validation_run_id": validation.run_id,
        "snapshot_sha256": snapshot.fingerprint,
        "plan_sha256": approval["plan_sha256"],
    }

    def persist() -> None:
        data = json.dumps(metadata, indent=2) + "\n"
        _write(output / "review-status.json", data)
        _write(latest, data)

    try:
        check_fix_lock(directory, _cycle_token)
        output.mkdir(parents=True, exist_ok=False)
        latest_review.unlink(missing_ok=True)
        _write(output / "diff.patch", snapshot.diff)
        persist()
        if any((directory / name).exists() for name in ("implementation.lock", "validation.lock")):
            raise ValueError("Another stage started; retry review after it finishes.")
        if validation_path.read_bytes() != validation_bytes:
            raise ValueError("Validation changed before review; retry with the current result.")
        if (await capture_snapshot(target, base)).fingerprint != snapshot.fingerprint:
            raise ValueError("Code changed before review; rerun validation.")
        result = await registry.execute(
            "review",
            directory,
            AgentTask(
                role="reviewer",
                prompt=prompt + "\nInput bundle:\n" + json.dumps(bundle),
                working_directory=target,
                read_only=True,
                output_schema=Review.model_json_schema(),
            ),
        )
        metadata.update(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_seconds=result.duration_seconds,
        )
        if not result.success or result.exit_code != 0 or result.timed_out:
            raise ValueError(
                "Reviewer execution failed; inspect model, authentication and timeout."
            )
        findings = parse_artifact(result.output, Review)
        findings.findings.extend(
            await knowledge_findings(target, knowledge_context(directory, registry.config))
        )
        await _verify_identity(repository, target, branch, base)
        if (
            not await approval_is_current(directory)
            or await checked_plan(directory) != plan
            or validation_path.read_bytes() != validation_bytes
            or (await capture_snapshot(target, base)).fingerprint != snapshot.fingerprint
            or not await clean_baseline(repository)
        ):
            raise ValueError(
                "Code, approval or validation changed during review; rerun affected stages."
            )
        _write(output / "review.json", findings.model_dump_json(indent=2) + "\n")
        _write(latest_review, findings.model_dump_json(indent=2) + "\n")
        metadata["status"] = "REVIEWED"
        metadata["finding_count"] = len(findings.findings)
        metadata["finished_at"] = datetime.now(UTC).isoformat()
        persist()
        return output
    except BaseException as exc:
        if output.exists():
            latest_review.unlink(missing_ok=True)
            metadata["status"] = (
                "INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
            )
            metadata["error_type"] = type(exc).__name__
            metadata["finished_at"] = datetime.now(UTC).isoformat()
            persist()
        raise
    finally:
        lock.unlink(missing_ok=True)
