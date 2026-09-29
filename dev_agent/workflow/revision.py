"""Apply explicit human feedback to a completed managed run, preserving history."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import clean_baseline, git
from dev_agent.models.agent import AgentTask
from dev_agent.models.config import ProjectConfig
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.validation.runner import validate
from dev_agent.workflow.feedback import feedback_history
from dev_agent.workflow.fixing import fix
from dev_agent.workflow.implementation import _instructions, _verify_identity
from dev_agent.workflow.investigation import _write
from dev_agent.workflow.locking import check_fix_lock, revision_owner
from dev_agent.workflow.persistence import Journal, now
from dev_agent.workflow.planning import _hash, _json, approval_is_current, checked_plan
from dev_agent.workflow.review import review
from dev_agent.workflow.runner import check_ready


async def revise(
    journal: Journal,
    ticket_id: str,
    feedback: str,
    config: ProjectConfig,
    registry: ProviderRegistry,
) -> str:
    if not feedback.strip():
        raise ValueError("Feedback must not be empty.")
    with journal.lock(ticket_id):
        run = journal.get(ticket_id)
        if ProjectConfig.model_validate_json(run["config"] or "{}") != config:
            raise ValueError("Configuration changed since start; restore it before revising.")
        if run["state"] != "READY_FOR_HUMAN_REVIEW":
            raise ValueError("Revise requires a completed run ready for human review.")
        if await check_ready(journal, ticket_id) != "READY_FOR_HUMAN_REVIEW":
            raise ValueError("Completed result is stale; inspect the worktree before revising.")
        assert run["plan_path"]
        plan_dir = Path(run["plan_path"])
        check_fix_lock(plan_dir)
        plan = await checked_plan(plan_dir)
        worktree = _json(plan_dir / "worktree.json")
        target = Path(str(worktree["path"]))
        base, branch = str(worktree["base_commit"]), str(worktree["branch"])
        original_approval = (plan_dir / "approval.json").read_bytes()
        history_before = feedback_history(plan_dir)
        root = plan_dir / "revisions"
        root.mkdir(exist_ok=True)
        lock = plan_dir / "revision.lock"
        token = uuid4().hex
        with lock.open("x") as stream:
            stream.write(token)
        owner = revision_owner.set(token)
        step_id: int | None = None
        output = root / f"{len(history_before) + 1:04d}-{token}"
        record: dict[str, object] = {
            "status": "RUNNING",
            "started_at": now(),
            "feedback_sha256": _hash(feedback.encode()),
            "worktree": str(target),
        }

        def save() -> None:
            _write(output / "revision.json", json.dumps(record, indent=2) + "\n")

        async def guard() -> None:
            await _verify_identity(journal.repository, target, branch, base)
            if (
                not await approval_is_current(plan_dir)
                or await checked_plan(plan_dir) != plan
                or (plan_dir / "approval.json").read_bytes() != original_approval
                or feedback_history(plan_dir) != history_before + [feedback]
            ):
                raise ValueError("Approval or feedback changed during revision.")
            if not await clean_baseline(journal.repository):
                raise ValueError("Source checkout changed during revision.")
            if await git(target, "diff", "--cached", "--name-only", "-z", "--"):
                raise ValueError("Staged changes require manual inspection.")
            if any(
                (plan_dir / name).exists()
                for name in (
                    "implementation.lock",
                    "validation.lock",
                    "review.lock",
                    "fix-cycle.lock",
                )
            ):
                raise ValueError("Another stage is running.")

        try:
            # Persist uncertain-write intent before any provider call.
            step_id = journal.begin(ticket_id, "revise")
            output.mkdir()
            _write(output / "feedback.md", feedback)
            save()
            await guard()
            snapshot = await capture_snapshot(target, base)
            record["snapshot_before"] = snapshot.fingerprint
            save()
            prompt = (
                "Implement the human's requested revision in this existing worktree. "
                "The feedback is an explicit amendment to the approved plan; later feedback "
                "takes precedence over earlier requirements where they conflict. Preserve "
                "unrelated changes. Follow repository instructions. Add regression tests. "
                "Do not commit, stage, merge, deploy, or change workflow artifacts. "
                "Return a concise summary, not a claim that validation passed.\nInput bundle:\n"
                + json.dumps(
                    {
                        "approved_plan": plan,
                        "human_feedback": history_before + [feedback],
                        "git_diff": snapshot.diff,
                        "repository_instructions": _instructions(target),
                    }
                )
            )
            result = await registry.resolve("implementer").execute(
                AgentTask(
                    role="implementer", prompt=prompt, working_directory=target, read_only=False
                )
            )
            record.update(
                agent_summary=result.output,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_seconds=result.duration_seconds,
            )
            save()
            await guard()
            if not result.success or result.exit_code != 0 or result.timed_out:
                raise ValueError("Revision provider failed; inspect preserved changes.")
            record["snapshot_after"] = (await capture_snapshot(target, base)).fingerprint
            journal.update(ticket_id, current_step="revise:validate")
            checks = await validate(plan_dir, config.commands, config.validation)
            record["validation_run_id"] = checks.run_id
            save()
            if not checks.success:
                raise ValueError("Revision validation failed; inspect validation.json.")
            await guard()
            journal.update(ticket_id, current_step="revise:review")
            await review(plan_dir, registry)
            await guard()
            journal.update(ticket_id, current_step="revise:fix-and-final-validation")
            state = await fix(plan_dir, config, registry)
            await guard()
            record.update(status=state, finished_at=now())
            save()
            journal.finish(
                step_id, "COMPLETE" if state == "READY_FOR_HUMAN_REVIEW" else "FAILED", output
            )
            journal.update(
                ticket_id,
                state=state,
                current_step="complete" if state == "READY_FOR_HUMAN_REVIEW" else "revise",
                error=None,
            )
            return state
        except BaseException as exc:
            if step_id is not None:
                state = "INTERRUPTED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
                record.update(status=state, error=str(exc), finished_at=now())
                if output.exists():
                    save()
                journal.finish(step_id, state, output, str(exc))
                journal.update(ticket_id, state=state, error=str(exc))
            raise
        finally:
            revision_owner.reset(owner)
            lock.unlink(missing_ok=True)
