"""Explicit human acceptance: commit reviewed contents and fast-forward the source."""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dev_agent.artifacts import parse_artifact
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import clean_baseline, git
from dev_agent.models.finding import Review
from dev_agent.validation.runner import ValidationRun
from dev_agent.workflow.feedback import feedback_history
from dev_agent.workflow.implementation import _verify_identity
from dev_agent.workflow.investigation import _write
from dev_agent.workflow.persistence import Journal, now
from dev_agent.workflow.planning import _json, _local_file, approval_is_current


@dataclass(frozen=True)
class MergePreview:
    target: Path
    branch: str
    destination: str
    base: str
    head: str
    fingerprint: str
    summary: str
    content_fingerprint: str


async def preview_merge(journal: Journal, ticket_id: str) -> MergePreview:
    run = journal.get(ticket_id)
    if not run["plan_path"]:
        raise ValueError("No completed implementation exists.")
    directory = Path(run["plan_path"])
    if run["state"] in {"MERGING", "MERGE_FAILED", "MERGED"}:
        raise ValueError("Merge already attempted; inspect merge.json and Git before continuing.")
    if not await approval_is_current(directory):
        raise ValueError("Plan approval is missing or stale.")
    feedback_history(directory)
    if any(
        (directory / name).exists()
        for name in (
            "implementation.lock",
            "validation.lock",
            "review.lock",
            "fix-cycle.lock",
            "revision.lock",
            "plan-review.lock",
        )
    ):
        raise ValueError("Another workflow stage is running.")
    record = _json(_local_file(directory, "worktree.json"))
    target = Path(str(record["path"])).resolve(strict=True)
    branch, base = str(record["branch"]), str(record["base_commit"])
    head = await git(target, "rev-parse", "HEAD")
    await _verify_identity(journal.repository, target, branch, head)
    if head != base:
        raise ValueError("Worktree already has commits; use Git to merge manually.")
    if await git(journal.repository, "rev-parse", "HEAD") != base:
        raise ValueError("Source HEAD advanced; rebase/revalidate manually before merging.")
    destination = await git(journal.repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    if destination == branch or not await clean_baseline(journal.repository):
        raise ValueError("Source checkout must be clean on a different branch.")
    if await git(target, "diff", "--cached", "--name-only"):
        raise ValueError("Worktree has staged changes; inspect them before merging.")
    snapshot = await capture_snapshot(target, base)
    validation = ValidationRun.model_validate_json(
        _local_file(directory, "validation.json").read_bytes()
    )
    review = _json(_local_file(directory, "review-status.json"))
    approval = _json(_local_file(directory, "approval.json"))
    revisions = directory / "revisions"
    if revisions.exists():
        for attempt in revisions.iterdir():
            started = datetime.fromisoformat(str(_json(attempt / "revision.json")["started_at"]))
            if (
                datetime.fromisoformat(validation.started_at) < started
                or datetime.fromisoformat(str(review["started_at"])) < started
            ):
                raise ValueError(
                    "Feedback is newer than validation/review; rerun both before merging."
                )
    findings_path = _local_file(directory, "review.json")
    findings = parse_artifact(findings_path.read_text(), Review)
    archive = Path(str(review.get("review_directory", ""))).resolve()
    if not archive.is_relative_to((directory / "reviews").resolve()):
        raise ValueError("Invalid archived review path.")
    if (
        not validation.success
        or validation.snapshot_sha256 != snapshot.fingerprint
        or validation.worktree != target
        or validation.base_commit != base
        or validation.plan_sha256 != approval.get("plan_sha256")
        or review.get("status") != "REVIEWED"
        or review.get("snapshot_sha256") != snapshot.fingerprint
        or review.get("plan_sha256") != approval.get("plan_sha256")
        or findings.findings
        or _local_file(archive, "review.json").read_bytes() != findings_path.read_bytes()
    ):
        raise ValueError(
            "Merge requires passing validation and a clear review of the current code."
        )
    summary = await git(target, "status", "--short")
    summary += "\n" + await git(target, "diff", "--stat", base)
    return MergePreview(
        target,
        branch,
        destination,
        base,
        head,
        snapshot.fingerprint,
        summary.strip(),
        snapshot.content_fingerprint,
    )


async def merge(journal: Journal, ticket_id: str, approved: MergePreview, message: str) -> str:
    if not message.strip():
        raise ValueError("Commit message must not be empty.")
    with journal.lock(ticket_id):
        source_lock = journal.path.parent / "merge.lock"
        with source_lock.open("x") as stream:
            stream.write(ticket_id)
        try:
            current = await preview_merge(journal, ticket_id)
            if current != approved:
                raise ValueError("Changes or branches changed after display; review again.")
            directory = Path(journal.get(ticket_id)["plan_path"] or "")
            # Standalone stages already respect this lock. No nested workflow is run here.
            stage_lock = directory / "revision.lock"
            with stage_lock.open("x") as stream:
                stream.write("Human-approved merge in progress")
            try:
                step = journal.begin(ticket_id, "merge")
                journal.update(ticket_id, state="MERGING")
                record = {
                    "status": "MERGING",
                    "source_branch": current.destination,
                    "worktree_branch": current.branch,
                    "base_commit": current.base,
                    "snapshot_sha256": current.fingerprint,
                    "approved_at": now(),
                }
                output = directory / "merge.json"
                _write(output, json.dumps(record, indent=2) + "\n")
                try:
                    if await git(current.target, "status", "--porcelain", "--untracked-files=all"):
                        await git(
                            current.target, "add", "--all", "--", ".", ":(exclude).dev-agent/**"
                        )
                        await git(
                            current.target,
                            "-c",
                            f"core.hooksPath={os.devnull}",
                            "-c",
                            "commit.gpgSign=false",
                            "commit",
                            "-m",
                            message,
                        )
                    commit = await git(current.target, "rev-parse", "HEAD")
                    record["commit"] = commit
                    _write(output, json.dumps(record, indent=2) + "\n")
                    if (
                        await capture_snapshot(current.target, current.base)
                    ).content_fingerprint != current.content_fingerprint:
                        raise ValueError(
                            "Code changed during commit; commit preserved, merge stopped."
                        )
                    if not await clean_baseline(current.target):
                        raise ValueError("Worktree changed during commit; merge stopped.")
                    if (
                        not await clean_baseline(journal.repository)
                        or await git(journal.repository, "rev-parse", "HEAD") != current.base
                        or await git(journal.repository, "symbolic-ref", "--short", "HEAD")
                        != current.destination
                    ):
                        raise ValueError("Source changed; commit preserved, merge stopped.")
                    await git(
                        journal.repository,
                        "-c",
                        f"core.hooksPath={os.devnull}",
                        "merge",
                        "--ff-only",
                        "--no-autostash",
                        commit,
                    )
                    record.update(status="MERGED", finished_at=now())
                    _write(output, json.dumps(record, indent=2) + "\n")
                    journal.finish(step, "COMPLETE", output)
                    journal.update(ticket_id, state="MERGED", current_step="complete", error=None)
                    return commit
                except BaseException as exc:
                    record.update(status="MERGE_FAILED", error=str(exc), finished_at=now())
                    _write(output, json.dumps(record, indent=2) + "\n")
                    journal.finish(step, "FAILED", output, str(exc))
                    journal.update(ticket_id, state="MERGE_FAILED", error=str(exc))
                    raise
            finally:
                stage_lock.unlink(missing_ok=True)
        finally:
            source_lock.unlink(missing_ok=True)
