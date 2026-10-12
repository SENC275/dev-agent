"""Integrate a completed ticket onto an advanced source without rewriting its old worktree."""

import json
import os
import re
from contextvars import Token
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from dev_agent.artifacts import parse_artifact
from dev_agent.context import safe
from dev_agent.context import save as write_record
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import WorktreeResult, clean_baseline, create_approved_worktree, git
from dev_agent.models.agent import AgentTask
from dev_agent.models.artifact import ArtifactModel, Text
from dev_agent.models.config import GitConfig, ProjectConfig
from dev_agent.process import run_process
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.agent_plan_gate import review_plan_by_agent
from dev_agent.workflow.feedback import feedback_history
from dev_agent.workflow.implementation import _instructions, _verify_identity
from dev_agent.workflow.investigation import investigate
from dev_agent.workflow.locking import revision_owner
from dev_agent.workflow.persistence import Journal, now
from dev_agent.workflow.planning import _hash, _inputs, _json, approval_is_current, generate_plan


class ConflictResolution(ArtifactModel):
    status: Literal["resolved", "needs_human"]
    summary: Text


class ConflictResolutionRequired(ValueError):
    """Known completed conflict attempt; manual edits may be explicitly continued."""


STAGE_LOCKS = (
    "implementation.lock",
    "validation.lock",
    "review.lock",
    "fix-cycle.lock",
    "revision.lock",
    "plan-review.lock",
    "worktree.lock",
)


async def command(repo: Path, *args: str, index: Path | None = None) -> str:
    argv = ["git", "-c", f"core.hooksPath={os.devnull}", *args]
    if index is not None:
        argv = ["env", f"GIT_INDEX_FILE={index}", *argv]
    result = await run_process(argv, cwd=repo)
    if result.timed_out or result.exit_code:
        raise ValueError(f"Sync Git {args[0]} failed; inspect preserved sync artifacts.")
    return result.stdout.strip()


async def guard(journal: Journal, record: dict[str, Any]) -> None:
    repo = journal.repository
    if (
        not await clean_baseline(repo)
        or await git(repo, "rev-parse", "HEAD") != record["new_base"]
        or await git(repo, "symbolic-ref", "--short", "HEAD") != record["source_branch"]
    ):
        raise ValueError("Source changed during sync; inspect before starting another integration.")
    target = Path(record["old_worktree"])
    await _verify_identity(repo, target, record["old_branch"], record["old_base"])
    if (await capture_snapshot(target, record["old_base"])).fingerprint != record["old_snapshot"]:
        raise ValueError("Original worktree changed during sync; preserved for inspection.")
    old_plan = Path(record["old_plan"])
    if any((old_plan / name).exists() for name in STAGE_LOCKS):
        raise ValueError("An original workflow stage is running.")


async def synchronize(
    journal: Journal,
    ticket: str,
    config: ProjectConfig,
    registry: ProviderRegistry,
    *,
    continue_sync: bool = False,
) -> str:
    from dev_agent.workflow.runner import check_ready, repair_validation

    with journal.lock(ticket):
        run = journal.get(ticket)
        if ProjectConfig.model_validate_json(run["config"] or "{}") != config:
            raise ValueError("Configuration changed since start; restore it before syncing.")
        # Share the same project-wide serialization boundary as merge.
        source_lock = journal.path.parent / "merge.lock"
        if not safe(source_lock):
            raise ValueError("Unsafe source lock.")
        try:
            stream = source_lock.open("x")
        except FileExistsError:
            raise ValueError(
                "Another merge/sync holds merge.lock; inspect before retrying."
            ) from None
        with stream:
            stream.write(f"sync {ticket}\n")
        try:
            root = journal.path.parent / "sync" / ticket
            if not safe(root):
                raise ValueError("Unsafe sync directory.")
            root.mkdir(parents=True, exist_ok=True)
            active = root / "active.json"
            record: dict[str, Any]
            manual = False
            if active.exists():
                if not safe(active):
                    raise ValueError("Unsafe active sync record.")
                record = _json(active)
                manual = record.get("status") == "CONFLICTS_REQUIRE_HUMAN"
                if manual and not continue_sync:
                    raise ValueError(
                        "Conflict resolution requires inspection/manual edits, then "
                        f"dev-agent sync {ticket} --continue. No automatic replay."
                    )
                if record.get("status") not in {
                    "AWAITING_PLAN_APPROVAL",
                    "COMPLETE",
                    "CONFLICTS_REQUIRE_HUMAN",
                }:
                    raise ValueError(
                        "Previous sync is incomplete; inspect active.json and worktrees. "
                        "Uncertain writes are not replayed automatically."
                    )
                pending = manual or record["status"] == "AWAITING_PLAN_APPROVAL"
            else:
                pending = False
            if continue_sync and not manual:
                raise ValueError(
                    "--continue requires a recorded conflict awaiting manual resolution."
                )
            step: int | None = None
            stage_lock: Path | None = None
            owner: Token[str | None] | None = None
            if not pending:
                if run["state"] not in {"READY_FOR_HUMAN_REVIEW", "WAITING_FOR_HUMAN"}:
                    raise ValueError(
                        "Sync requires a completed, previously verified implementation."
                    )
                old_plan = Path(run["plan_path"] or "")
                if not safe(old_plan):
                    raise ValueError("Unsafe original plan path.")
                worktree = _json(old_plan / "worktree.json")
                target = Path(str(worktree["path"])).resolve(strict=True)
                old_base, branch = str(worktree["base_commit"]), str(worktree["branch"])
                await _verify_identity(journal.repository, target, branch, old_base)
                if any((old_plan / name).exists() for name in STAGE_LOCKS):
                    raise ValueError("Another workflow stage is running.")
                if await git(target, "diff", "--cached", "--name-only"):
                    raise ValueError("Staged changes require manual inspection before sync.")
                new_base = await git(journal.repository, "rev-parse", "HEAD")
                if new_base == old_base:
                    raise ValueError("Ticket already uses source HEAD; use resume/revise instead.")
                await command(journal.repository, "merge-base", "--is-ancestor", old_base, new_base)
                if not await clean_baseline(journal.repository):
                    raise ValueError("Source checkout must be clean before sync.")
                snapshot = await capture_snapshot(target, old_base)
                approval = _json(old_plan / "approval.json")
                validation = _json(old_plan / "validation.json")
                review = _json(old_plan / "review-status.json")
                findings = _json(old_plan / "review.json")
                archive = Path(str(review.get("review_directory", "")))
                if (
                    approval.get("decision") != "APPROVED"
                    or approval.get("base_commit") != old_base
                    or approval.get("plan_sha256") != _hash((old_plan / "plan.md").read_bytes())
                    or approval.get("input_hashes") != _inputs(old_plan.parent.parent)
                    or validation.get("plan_sha256") != approval.get("plan_sha256")
                    or review.get("plan_sha256") != approval.get("plan_sha256")
                    or validation.get("base_commit") != old_base
                    or validation.get("worktree") != str(target)
                    or validation.get("status") != "PASSED"
                    or validation.get("snapshot_sha256") != snapshot.fingerprint
                    or review.get("snapshot_sha256") != snapshot.fingerprint
                    or review.get("status") != "REVIEWED"
                    or findings.get("findings") != []
                    or not archive.resolve().is_relative_to((old_plan / "reviews").resolve())
                    or not safe(archive / "review.json")
                    or (archive / "review.json").read_bytes()
                    != (old_plan / "review.json").read_bytes()
                ):
                    raise ValueError(
                        "Old result changed or lacks passing checks/clear review; inspect it."
                    )
                attempt = root / uuid4().hex
                attempt.mkdir()
                record = {
                    "status": "PREPARING",
                    "started_at": now(),
                    "attempt": str(attempt),
                    "old_plan": str(old_plan),
                    "old_worktree": str(target),
                    "old_branch": branch,
                    "old_base": old_base,
                    "old_snapshot": snapshot.fingerprint,
                    "new_base": new_base,
                    "source_branch": await git(
                        journal.repository, "symbolic-ref", "--short", "HEAD"
                    ),
                }
            else:
                attempt = Path(record["attempt"])
                if not safe(attempt) or not attempt.is_relative_to(root):
                    raise ValueError("Invalid sync attempt path.")

            def save() -> None:
                write_record(attempt / "sync.json", record)
                write_record(active, record)

            try:
                await guard(journal, record)
                step = journal.begin(ticket, "sync")
                if not pending:
                    save()  # Persist intent before snapshots, model calls or worktree creation.
                    index = attempt / "index"
                    await command(target, "read-tree", old_base, index=index)
                    await command(
                        target, "add", "--all", "--", ".", ":(exclude).dev-agent/**", index=index
                    )
                    tree = await command(target, "write-tree", index=index)
                    commit = await command(
                        target,
                        "-c",
                        "user.name=dev-agent",
                        "-c",
                        "user.email=dev-agent@localhost",
                        "-c",
                        "commit.gpgSign=false",
                        "commit-tree",
                        tree,
                        "-p",
                        old_base,
                        "-m",
                        f"Preserve {ticket} before sync",
                    )
                    await command(
                        target, "update-ref", f"refs/dev-agent/sync/{attempt.name}", commit
                    )
                    record["snapshot_commit"] = commit
                    # merge-tree writes objects only; original worktree/index/branch stay intact.
                    result = await run_process(
                        [
                            "git",
                            "-c",
                            f"core.hooksPath={os.devnull}",
                            "merge-tree",
                            "--write-tree",
                            "--name-only",
                            "-z",
                            new_base,
                            commit,
                        ],
                        cwd=journal.repository,
                    )
                    if result.timed_out or result.exit_code not in {0, 1}:
                        raise ValueError(
                            "Git merge-tree failed (requires Git 2.38+); old worktree preserved."
                        )
                    fields = result.stdout.split("\0")
                    merged_tree = fields[0]
                    if not re.fullmatch(r"[0-9a-f]{40,64}", merged_tree):
                        raise ValueError("Invalid merge-tree output; inspect original worktree.")
                    conflicts = []
                    if result.exit_code == 1:
                        for field in fields[1:]:
                            if not field:
                                break
                            conflicts.append(field)
                    record.update(
                        merged_tree=merged_tree,
                        conflicts=conflicts,
                        merge_conflicted=result.exit_code == 1,
                    )
                    save()
                    feedback = feedback_history(old_plan)
                    request = (old_plan.parent.parent / "ticket.md").read_text()
                    if feedback:
                        request += "\n\nAccepted human revision history:\n" + json.dumps(feedback)
                    request += (
                        "\n\nIntegration task: preserve this ticket's intent on the latest source. "
                        "Other accepted changes must remain intact. Recheck compatibility; "
                        "do not silently choose between contradictory requirements.\n"
                        "Original approved plan (historical):\n"
                        + (old_plan / "plan.md").read_text()
                        + "\nTicket diff (historical):\n"
                        + snapshot.diff
                        + "\nUpstream changes since old base:\n"
                        + await git(
                            journal.repository,
                            "diff",
                            "--no-ext-diff",
                            "--no-textconv",
                            old_base,
                            new_base,
                            "--",
                        )
                    )
                    journal.update(ticket, current_step="sync:investigate")
                    research = await investigate(
                        ticket_id=ticket,
                        ticket=request,
                        repository=journal.repository,
                        registry=registry,
                    )
                    record["investigation"] = str(research.directory)
                    save()
                    await guard(journal, record)
                    if not research.success:
                        raise ValueError("Sync investigation failed; inspect preserved attempt.")
                    journal.update(ticket, current_step="sync:plan")
                    plan = await generate_plan(research.directory, registry)
                    record["plan"] = str(plan)
                    record["status"] = "AWAITING_PLAN_APPROVAL"
                    save()
                    journal.update(
                        ticket, plan_path=str(plan), investigation_path=str(research.directory)
                    )
                    if config.gates.plan_review == "agent":
                        journal.update(ticket, current_step="sync:plan_review")
                        await review_plan_by_agent(plan, registry)
                plan = Path(record["plan"])
                await guard(journal, record)
                if not await approval_is_current(plan):
                    journal.finish(step, "COMPLETE", attempt)
                    journal.update(ticket, state="AWAITING_PLAN_APPROVAL", current_step="sync")
                    return "AWAITING_PLAN_APPROVAL"
                candidate_lock = plan / "revision.lock"
                stage_token = uuid4().hex
                with candidate_lock.open("x") as stream:
                    stream.write(stage_token)
                stage_lock = candidate_lock
                owner = revision_owner.set(stage_token)
                record["status"] = "INTEGRATING"
                save()
                journal.update(ticket, current_step="sync:integrate")
                if manual:
                    saved_worktree = _json(plan / "worktree.json")
                    integrated_worktree = WorktreeResult(
                        repository=str(saved_worktree["repository"]),
                        path=str(saved_worktree["path"]),
                        branch=str(saved_worktree["branch"]),
                        base_commit=str(saved_worktree["base_commit"]),
                    )
                    integrated = Path(integrated_worktree.path)
                    await _verify_identity(
                        journal.repository,
                        integrated,
                        integrated_worktree.branch,
                        record["new_base"],
                    )
                else:
                    suffix = attempt.name[:12]
                    git_config = GitConfig(
                        branch_pattern=record["old_branch"] + "-sync-" + suffix,
                        worktree_directory=record["old_worktree"] + "-sync-" + suffix,
                    )
                    integrated_worktree = await create_approved_worktree(plan, git_config)
                    integrated = Path(integrated_worktree.path)
                    record["new_worktree"] = str(integrated)
                    save()
                    journal.update(
                        ticket, worktree_path=str(integrated), branch=integrated_worktree.branch
                    )
                    await command(integrated, "read-tree", "--reset", "-u", record["merged_tree"])
                    await command(integrated, "reset", "--mixed", record["new_base"])
                if record["merge_conflicted"]:
                    if not manual:
                        if config.limits.max_fix_cycles == 0:
                            raise ConflictResolutionRequired(
                                "Integration conflicts need manual resolution; automatic fixes "
                                "disabled."
                            )
                        journal.update(ticket, current_step="sync:resolve_conflicts")
                        resolution = await registry.execute(
                            "sync_conflicts",
                            plan,
                            AgentTask(
                                role="fixer",
                                working_directory=integrated,
                                read_only=False,
                                output_schema=ConflictResolution.model_json_schema(),
                                prompt=(
                                    "Resolve integration conflicts in this worktree only. "
                                    "Preserve both "
                                    "accepted upstream behavior and this ticket's requirements. "
                                    "Do not "
                                    "commit, stage, merge, deploy or alter workflow artifacts. "
                                    "If requirements "
                                    "contradict, leave conflict markers and explain the "
                                    "decision needed. "
                                    "Read the conflict files and relevant code. Return a "
                                    "JSON object matching the supplied schema. "
                                    "Use status=needs_human when any conflict "
                                    "or requirement choice "
                                    "remains unresolved, even if there are no textual markers.\n"
                                    + json.dumps(
                                        {
                                            "ticket": (
                                                plan.parent.parent / "ticket.md"
                                            ).read_text(),
                                            "plan": (plan / "plan.md").read_text(),
                                            "conflicts": record["conflicts"],
                                            "schema": ConflictResolution.model_json_schema(),
                                            "instructions": _instructions(integrated),
                                        }
                                    )
                                ),
                            ),
                        )
                        record["resolution_summary"] = resolution.output
                        save()
                        if not resolution.success or resolution.timed_out or resolution.exit_code:
                            raise ConflictResolutionRequired(
                                "Conflict resolver failed; inspect the preserved "
                                "integration worktree."
                            )
                        try:
                            decision = parse_artifact(resolution.output, ConflictResolution)
                        except ValueError as exc:
                            raise ConflictResolutionRequired(
                                "Conflict resolver returned an invalid decision; inspect manually."
                            ) from exc
                        if decision.status != "resolved":
                            raise ConflictResolutionRequired(
                                "Conflict resolver requests a human decision; inspect sync.json."
                            )
                    for path in record["conflicts"]:
                        relative = Path(path)
                        if relative.is_absolute() or ".." in relative.parts:
                            raise ValueError("Unsafe conflict path.")
                        file = integrated / relative
                        if file.is_symlink():
                            raise ConflictResolutionRequired(
                                "Symlink conflict requires manual inspection."
                            )
                        if file.is_file() and re.search(
                            r"(?m)^(<<<<<<< |=======\s*$|>>>>>>> )",
                            file.read_text(errors="replace"),
                        ):
                            raise ConflictResolutionRequired(
                                "Unresolved conflict markers; human decision required."
                            )
                await guard(journal, record)
                await _verify_identity(
                    journal.repository, integrated, integrated_worktree.branch, record["new_base"]
                )
                if await git(integrated, "diff", "--cached", "--name-only"):
                    raise ValueError("Resolver staged changes; inspect manually.")
                if not await approval_is_current(plan):
                    raise ValueError("Integration plan changed during execution.")
                approval = _json(plan / "approval.json")
                write_record(
                    plan / "implementation.json",
                    {
                        "status": "IMPLEMENTED",
                        "worktree": str(integrated),
                        "branch": integrated_worktree.branch,
                        "base_commit": record["new_base"],
                        "plan_sha256": approval["plan_sha256"],
                        "input_hashes": approval["input_hashes"],
                        "started_at": record["started_at"],
                        "finished_at": now(),
                        "origin": "sync",
                        "sync_record": str(attempt / "sync.json"),
                    },
                )
                journal.finish(step, "COMPLETE", attempt)
                step = None
                # Full validation, independent review and bounded repair on the NEW baseline.
                state = await repair_validation(
                    journal, ticket, plan, config, registry, revalidate=True, check_completion=False
                )
                revision_owner.reset(owner)
                owner = None
                stage_lock.unlink()
                stage_lock = None
                await guard(journal, record)
                if state == "READY_FOR_HUMAN_REVIEW":
                    state = await check_ready(journal, ticket)
                record.update(status="COMPLETE", result=state, finished_at=now())
                save()
                return state
            except BaseException as exc:
                needs_human = isinstance(exc, ConflictResolutionRequired)
                record.update(
                    status="CONFLICTS_REQUIRE_HUMAN" if needs_human else "FAILED",
                    error=str(exc),
                    finished_at=now(),
                )
                save()
                if step is not None:
                    journal.finish(step, "FAILED", attempt, str(exc))
                journal.update(
                    ticket,
                    state="WAITING_FOR_HUMAN" if needs_human else "SYNC_FAILED",
                    current_step="sync",
                    error=str(exc),
                )
                raise
            finally:
                if owner is not None:
                    revision_owner.reset(owner)
                if stage_lock is not None:
                    stage_lock.unlink(missing_ok=True)
        finally:
            source_lock.unlink(missing_ok=True)
