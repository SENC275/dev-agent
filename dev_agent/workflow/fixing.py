"""Bounded verification/fix/validation/review cycles; never auto-approve code."""

import asyncio
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from dev_agent.artifacts import parse_artifact
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import clean_baseline, git
from dev_agent.models.agent import AgentTask
from dev_agent.models.config import ProjectConfig
from dev_agent.models.finding import Review
from dev_agent.models.fix import FixReport
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.validation.runner import ValidationRun, infrastructure_failure, validate
from dev_agent.workflow.feedback import feedback_history
from dev_agent.workflow.implementation import _instructions, _verify_identity, change_warnings
from dev_agent.workflow.investigation import _write
from dev_agent.workflow.knowledge import knowledge_context
from dev_agent.workflow.locking import check_revision_lock
from dev_agent.workflow.planning import (
    _json,
    _local_file,
    approval_is_current,
    checked_plan,
    investigation_repository,
)
from dev_agent.workflow.review import review


async def _current_review(directory: Path, target: Path, base: str) -> Review:
    status = _json(_local_file(directory, "review-status.json"))
    validation = ValidationRun.model_validate_json(
        _local_file(directory, "validation.json").read_bytes()
    )
    approval = _json(_local_file(directory, "approval.json"))
    snapshot = await capture_snapshot(target, base)
    if (
        status.get("status") != "REVIEWED"
        or not validation.success
        or validation.base_commit != base
        or validation.worktree != target
        or validation.plan_sha256 != approval.get("plan_sha256")
        or status.get("plan_sha256") != approval.get("plan_sha256")
        or validation.snapshot_sha256 != snapshot.fingerprint
        or status.get("snapshot_sha256") != snapshot.fingerprint
        or status.get("validation_run_id") != validation.run_id
    ):
        raise ValueError(
            "A current successful review and validation are required; rerun those stages."
        )
    raw = status.get("review_directory")
    if not isinstance(raw, str):
        raise ValueError("Missing archived review directory.")
    archive = Path(raw).resolve(strict=True)
    if not archive.is_relative_to(directory / "reviews"):
        raise ValueError("Review archive is outside this plan.")
    contents = _local_file(directory, "review.json").read_text(encoding="utf-8")
    if contents != _local_file(archive, "review.json").read_text(encoding="utf-8"):
        raise ValueError("Latest findings differ from archived review; rerun review.")
    return parse_artifact(contents, Review)


async def failed_validation(
    directory: Path, config: ProjectConfig, target: Path, base: str
) -> ValidationRun:
    """Only completed, unchanged command failures can enter automatic repair."""
    report = ValidationRun.model_validate_json(
        _local_file(directory, "validation.json").read_bytes()
    )
    approval = _json(_local_file(directory, "approval.json"))
    archive = directory / "validations" / report.run_id / "validation.json"
    if (
        not archive.resolve().is_relative_to((directory / "validations").resolve())
        or archive.read_bytes() != (directory / "validation.json").read_bytes()
        or report.status != "FAILED"
        or not report.finished_at
        or report.error
        or report.worktree != target
        or report.base_commit != base
        or report.plan_sha256 != approval.get("plan_sha256")
        or report.snapshot_sha256 != (await capture_snapshot(target, base)).fingerprint
        or {item.name: item.command for item in report.commands} != config.commands.model_dump()
        or not any(item.status == "FAILED" for item in report.commands)
        or any(
            item.status in {"RUNNING", "INTERRUPTED"}
            or item.timed_out
            or item.error
            or (item.status == "FAILED" and item.exit_code in {None, 0})
            for item in report.commands
        )
    ):
        raise ValueError("Validation is stale, interrupted, or unsafe to repair automatically.")
    return report


def fix_budget(directory: Path, config: ProjectConfig) -> int:
    """Explicit grants persist independently of the frozen project configuration."""
    path = _local_file(directory, "fix-budget.json")
    if not path.exists():
        return config.limits.max_fix_cycles
    receipt = _json(path)
    approval = _json(_local_file(directory, "approval.json"))
    extra = receipt.get("additional_cycles")
    if (
        type(extra) is not int
        or extra < 0
        or receipt.get("plan_sha256") != approval.get("plan_sha256")
        or receipt.get("base_commit") != approval.get("base_commit")
    ):
        raise ValueError("Invalid fix budget receipt; inspect manually.")
    return config.limits.max_fix_cycles + extra


async def fix(
    directory: Path,
    config: ProjectConfig,
    registry: ProviderRegistry,
    *,
    from_validation: bool = False,
) -> str:
    directory = directory.resolve(strict=True)
    repository = investigation_repository(directory.parent.parent)
    worktree = _json(_local_file(directory, "worktree.json"))
    raw, branch, base = worktree.get("path"), worktree.get("branch"), worktree.get("base_commit")
    if not all(isinstance(value, str) and value for value in (raw, branch, base)):
        raise ValueError("Invalid worktree record.")
    assert isinstance(raw, str) and isinstance(branch, str) and isinstance(base, str)
    target = Path(raw).resolve(strict=True)
    plan = await checked_plan(directory)

    async def guard() -> None:
        check_revision_lock(directory)
        if not await approval_is_current(directory) or await checked_plan(directory) != plan:
            raise ValueError("Plan approval changed; human inspection required.")
        approval = _json(_local_file(directory, "approval.json"))
        if worktree.get("repository") != str(repository) or approval.get("base_commit") != base:
            raise ValueError("Worktree does not match approval.")
        await _verify_identity(repository, target, branch, base)
        if not await clean_baseline(repository):
            raise ValueError("Source checkout changed; human inspection required.")
        if await git(target, "diff", "--cached", "--name-only", "-z", "--"):
            raise ValueError("Staged changes require human inspection.")
        if any(
            (directory / name).exists()
            for name in ("implementation.lock", "validation.lock", "review.lock")
        ):
            raise ValueError("Another stage is still running.")

    await guard()
    initial_feedback = None
    if from_validation:
        initial_feedback = (await failed_validation(directory, config, target, base)).model_dump(
            mode="json"
        )
        findings = Review(findings=[])
    else:
        findings = await _current_review(directory, target, base)
    fixes = directory / "fixes"
    if not fixes.resolve().is_relative_to(directory):
        raise ValueError("Fix artifacts must stay inside the plan directory.")
    approved_hash = _json(_local_file(directory, "approval.json"))["plan_sha256"]
    budget = fix_budget(directory, config)
    token = uuid4().hex
    lock = directory / "fix-cycle.lock"
    latest = _local_file(directory, "fix-cycle.json")
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(token)
    summary: dict[str, object] = {
        "status": "RUNNING",
        "plan_sha256": approved_hash,
        "base_commit": base,
        "worktree": str(target),
        "max_fix_cycles": budget,
        "started_at": datetime.now(UTC).isoformat(),
    }
    current: Path | None = None
    attempt: dict[str, object] = {}

    def save(status: str, reason: str = "") -> str:
        summary.update(status=status, reason=reason, updated_at=datetime.now(UTC).isoformat())
        _write(latest, json.dumps(summary, indent=2) + "\n")
        return status

    try:
        await guard()
        if from_validation:
            initial_feedback = (
                await failed_validation(directory, config, target, base)
            ).model_dump(mode="json")
        else:
            findings = await _current_review(directory, target, base)
        fixes.mkdir(exist_ok=True)
        entries = list(fixes.iterdir())
        if any(
            not item.name.isdigit() or not item.is_dir() or item.is_symlink() for item in entries
        ):
            raise ValueError("Unexpected fix history contents; inspect manually.")
        used = max((int(item.name) for item in entries), default=0)
        summary["cycles_used"] = used
        save("RUNNING")
        feedback: dict[str, object] | None = initial_feedback
        expected_snapshot = (await capture_snapshot(target, base)).fingerprint
        while True:
            await guard()
            if (await capture_snapshot(target, base)).fingerprint != expected_snapshot:
                raise ValueError(
                    "Code changed between fix stages; inspect and rerun validation/review."
                )
            if feedback is None:
                findings = await _current_review(directory, target, base)
            if not findings.findings and feedback is None:
                final = await validate(
                    directory, config.commands, config.validation, _cycle_token=token
                )
                _write(
                    _local_file(directory, "final-validation.json"),
                    final.model_dump_json(indent=2) + "\n",
                )
                if not final.success:
                    return save("WAITING_FOR_HUMAN", "Final validation failed.")
                await guard()
                if final.snapshot_sha256 != expected_snapshot:
                    raise ValueError("Final validation does not match the reviewed code.")
                summary["snapshot_sha256"] = final.snapshot_sha256
                summary["final_validation_run_id"] = final.run_id
                summary["review_directory"] = _json(
                    _local_file(directory, "review-status.json")
                ).get("review_directory")
                return save(
                    "READY_FOR_HUMAN_REVIEW",
                    "Independent review is clear and final validation passed.",
                )
            if feedback is not None and infrastructure_failure(
                ValidationRun.model_validate(feedback)
            ):
                return save(
                    "WAITING_FOR_HUMAN",
                    "Validation environment unavailable; restore it and resume to revalidate.",
                )
            if used >= budget:
                return save(
                    "WAITING_FOR_HUMAN",
                    "Fix cycle limit reached with unresolved findings or validation failures.",
                )
            used += 1
            current = fixes / f"{used:04d}"
            current.mkdir(exist_ok=False)  # Persist budget consumption before any provider call.
            summary["cycles_used"] = used
            summary["current_cycle"] = str(current)
            before = await capture_snapshot(target, base)
            attempt = {
                "status": "RUNNING",
                "cycle": used,
                "snapshot_before": before.fingerprint,
                "started_at": datetime.now(UTC).isoformat(),
            }
            _write(current / "attempt.json", json.dumps(attempt, indent=2) + "\n")
            save("RUNNING")
            _write(current / "input-review.json", findings.model_dump_json(indent=2) + "\n")
            if feedback is not None:
                _write(current / "input-validation.json", json.dumps(feedback, indent=2) + "\n")
            bundle = {
                "ticket": _local_file(directory.parent.parent, "ticket.md").read_text(
                    encoding="utf-8"
                ),
                "approved_plan": plan,
                "human_feedback": feedback_history(directory),
                "feedback_policy": (
                    "Human feedback amends the approved plan; "
                    "later feedback takes precedence where requirements conflict."
                ),
                "findings": findings.model_dump(),
                "git_diff": before.diff,
                "validation_failures": feedback,
                "change_warnings": await change_warnings(target, base),
                "repository_instructions": _instructions(target),
                "schema": FixReport.model_json_schema(),
                "knowledge": knowledge_context(directory, config),
            }
            prompt = files("dev_agent").joinpath("prompts/fix.md").read_text(encoding="utf-8")
            result = await registry.execute(
                "fix",
                directory,
                AgentTask(
                    role="fixer",
                    prompt=prompt + "\nInput bundle:\n" + json.dumps(bundle),
                    working_directory=target,
                    read_only=False,
                    output_schema=FixReport.model_json_schema(),
                ),
            )
            attempt.update(
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_seconds=result.duration_seconds,
            )
            if not result.success or result.exit_code != 0 or result.timed_out:
                raise ValueError(
                    "Fixer execution failed; inspect partial changes. No automatic retry."
                )
            report = parse_artifact(result.output, FixReport)
            if sorted(item.finding_index for item in report.decisions) != list(
                range(len(findings.findings))
            ):
                raise ValueError("Fixer must report exactly one decision for every finding.")
            await guard()
            attempt.update(
                status="FIXED", snapshot_after=(await capture_snapshot(target, base)).fingerprint
            )
            expected_snapshot = str(attempt["snapshot_after"])
            _write(current / "fix.json", report.model_dump_json(indent=2) + "\n")
            _write(current / "attempt.json", json.dumps(attempt, indent=2) + "\n")
            checks = await validate(
                directory, config.commands, config.validation, _cycle_token=token
            )
            attempt["validation_run_id"] = checks.run_id
            if not checks.success:
                feedback = checks.model_dump(mode="json")
                attempt["status"] = "VALIDATION_FAILED"
                _write(current / "attempt.json", json.dumps(attempt, indent=2) + "\n")
                if any(item.disposition == "deferred" for item in report.decisions):
                    return save(
                        "WAITING_FOR_HUMAN", "Fixer deferred findings and validation failed."
                    )
                continue
            feedback = None
            reviewed = await review(directory, registry, _cycle_token=token)
            findings = parse_artifact(
                (reviewed / "review.json").read_text(encoding="utf-8"), Review
            )
            attempt.update(status="REVIEWED", review_directory=str(reviewed))
            _write(current / "attempt.json", json.dumps(attempt, indent=2) + "\n")
            if any(item.disposition == "deferred" for item in report.decisions):
                return save("WAITING_FOR_HUMAN", "Fixer deferred findings for human judgment.")
    except BaseException as exc:
        status = "INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
        if current is not None:
            attempt.update(status=status, error_type=type(exc).__name__)
            _write(current / "attempt.json", json.dumps(attempt, indent=2) + "\n")
        save(status, f"{type(exc).__name__}; inspect artifacts and preserve partial edits.")
        raise
    finally:
        lock.unlink(missing_ok=True)
