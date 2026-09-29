"""Durable stage orchestration with explicit human approval and conservative recovery."""

from pathlib import Path

from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import clean_baseline, create_approved_worktree
from dev_agent.models.config import ProjectConfig
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.validation.runner import ValidationRun, validate
from dev_agent.workflow.agent_plan_gate import review_plan_by_agent
from dev_agent.workflow.fixing import failed_validation, fix
from dev_agent.workflow.implementation import _verify_identity, implement
from dev_agent.workflow.investigation import investigate
from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.planning import _json, approval_is_current, generate_plan
from dev_agent.workflow.review import review

STAGES = ("investigate", "plan", "worktree", "implement", "validate", "review", "fix")
RETRYABLE = {"investigate", "plan", "plan_review", "review"}


async def repair_validation(
    journal: Journal,
    ticket_id: str,
    plan: Path,
    config: ProjectConfig,
    registry: ProviderRegistry,
    *,
    revalidate: bool = False,
) -> str:
    step = journal.begin(ticket_id, "validation_fix")
    try:
        from_validation = True
        if revalidate:
            record = _json(plan / "worktree.json")
            await failed_validation(
                plan, config, Path(str(record["path"])), str(record["base_commit"])
            )
            validation = await validate(plan, config.commands, config.validation)
            if validation.success:
                await review(plan, registry)
                from_validation = False
        state = await fix(plan, config, registry, from_validation=from_validation)
        journal.finish(step, "COMPLETE", plan / "fix-cycle.json")
        journal.update(
            ticket_id,
            state=state,
            current_step="complete" if state == "READY_FOR_HUMAN_REVIEW" else "validation_fix",
            error=None
            if state == "READY_FOR_HUMAN_REVIEW"
            else str(_json(plan / "fix-cycle.json").get("reason", "Repair requires inspection.")),
        )
        if state == "READY_FOR_HUMAN_REVIEW":
            return await check_ready(journal, ticket_id)
        return state
    except BaseException as exc:
        status = "FAILED" if isinstance(exc, Exception) else "INTERRUPTED"
        journal.finish(step, status, plan, str(exc))
        journal.update(ticket_id, state=status, current_step="validation_fix", error=str(exc))
        raise


async def advance(
    journal: Journal, ticket_id: str, config: ProjectConfig, registry: ProviderRegistry
) -> str:
    with journal.lock(ticket_id):
        run = journal.get(ticket_id)
        if ProjectConfig.model_validate_json(run["config"] or "{}") != config:
            raise ValueError("Configuration changed since start; restore it before resuming.")
        if run["state"] in {"MERGED", "MERGING", "MERGE_FAILED"}:
            return run["state"]
        if run["state"] == "READY_FOR_HUMAN_REVIEW":
            return await check_ready(journal, ticket_id)
        if run["state"] == "WAITING_FOR_HUMAN":
            steps = journal.steps(ticket_id)
            if (
                run["current_step"] == "validation_fix"
                and run["plan_path"]
                and steps
                and steps[-1]["name"] == "validation_fix"
                and steps[-1]["status"] == "COMPLETE"
                and _json(Path(run["plan_path"]) / "validation.json").get("status") == "FAILED"
            ):
                return await repair_validation(
                    journal, ticket_id, Path(run["plan_path"]), config, registry, revalidate=True
                )
            return "WAITING_FOR_HUMAN"
        history = journal.steps(ticket_id)
        completed = {step["name"] for step in history if step["status"] == "COMPLETE"}
        if history and history[-1]["status"] != "COMPLETE":
            previous = history[-1]
            if (
                previous["name"] == "validate"
                and previous["status"] == "FAILED"
                and run["plan_path"]
            ):
                return await repair_validation(
                    journal, ticket_id, Path(run["plan_path"]), config, registry
                )
            if previous["name"] not in RETRYABLE:
                journal.update(
                    ticket_id,
                    state="WAITING_FOR_HUMAN",
                    error="Previous mutating/command stage did not complete. "
                    "Inspect its artifacts and worktree; automatic replay is disabled.",
                )
                return "WAITING_FOR_HUMAN"
        stages = list(STAGES)
        if config.gates.plan_review == "agent":
            stages.insert(2, "plan_review")
        for name in stages:
            if name in completed:
                continue
            run = journal.get(ticket_id)
            plan = Path(run["plan_path"]) if run["plan_path"] else None
            if name == "worktree":
                assert plan is not None
                if not await approval_is_current(plan):
                    journal.update(ticket_id, state="AWAITING_PLAN_APPROVAL", current_step=name)
                    return "AWAITING_PLAN_APPROVAL"
            step_id = journal.begin(ticket_id, name)
            artifact: Path | None = plan
            try:
                if name == "investigate":
                    result = await investigate(
                        ticket_id=ticket_id,
                        ticket=run["ticket"] or "",
                        repository=journal.repository,
                        registry=registry,
                    )
                    artifact = result.directory
                    journal.update(ticket_id, investigation_path=str(artifact))
                    if not result.success:
                        raise ValueError("Investigation failed; inspect investigation.json.")
                elif name == "plan":
                    assert run["investigation_path"]
                    artifact = await generate_plan(Path(run["investigation_path"]), registry)
                    journal.update(ticket_id, plan_path=str(artifact))
                else:
                    assert plan is not None
                    if name == "plan_review":
                        approved = await review_plan_by_agent(plan, registry)
                        journal.finish(step_id, "COMPLETE", plan / "plan-review.json")
                        if not approved:
                            journal.update(
                                ticket_id,
                                state="AWAITING_PLAN_APPROVAL",
                                current_step="plan_review",
                                error="Plan reviewer requested changes; inspect plan-review.json.",
                            )
                            return "AWAITING_PLAN_APPROVAL"
                        continue
                    elif name == "worktree":
                        worktree = await create_approved_worktree(plan, config.git)
                        journal.update(
                            ticket_id, worktree_path=str(worktree.path), branch=worktree.branch
                        )
                    elif name == "implement":
                        implementation = await implement(plan, registry)
                        if not implementation.success:
                            raise ValueError("Implementation failed; inspect implementation.json.")
                    elif name == "validate":
                        validation = await validate(plan, config.commands, config.validation)
                        artifact = validation.directory
                        if not validation.success:
                            journal.finish(
                                step_id,
                                "FAILED",
                                artifact,
                                "Validation failed; handing off to fixer.",
                            )
                            return await repair_validation(
                                journal, ticket_id, plan, config, registry
                            )
                    elif name == "review":
                        artifact = await review(plan, registry)
                    elif name == "fix":
                        state = await fix(plan, config, registry)
                        if state != "READY_FOR_HUMAN_REVIEW":
                            raise ValueError(f"Fix cycle stopped: {state}; inspect fix-cycle.json.")
                        receipt = _json(plan / "fix-cycle.json")
                        if receipt.get("status") != state:
                            raise ValueError("Missing final completion receipt.")
                journal.finish(step_id, "COMPLETE", artifact)
            except BaseException as exc:
                if journal.get(ticket_id)["current_step"] == "validation_fix":
                    raise
                status = "FAILED" if isinstance(exc, Exception) else "INTERRUPTED"
                journal.finish(step_id, status, artifact, str(exc))
                journal.update(ticket_id, state=status, error=str(exc))
                raise
        journal.update(ticket_id, state="READY_FOR_HUMAN_REVIEW", current_step="complete")
        return "READY_FOR_HUMAN_REVIEW"


async def check_ready(journal: Journal, ticket_id: str) -> str:
    run = journal.get(ticket_id)
    assert run["plan_path"]
    ready_plan = Path(run["plan_path"])
    record = _json(ready_plan / "worktree.json")
    receipt = _json(ready_plan / "fix-cycle.json")
    approval = _json(ready_plan / "approval.json")
    final = ValidationRun.model_validate_json((ready_plan / "validation.json").read_bytes())
    target = Path(str(record["path"]))
    base = str(record["base_commit"])
    await _verify_identity(journal.repository, target, str(record["branch"]), base)
    if (
        not await approval_is_current(ready_plan)
        or receipt.get("status") != "READY_FOR_HUMAN_REVIEW"
        or receipt.get("plan_sha256") != approval.get("plan_sha256")
        or receipt.get("base_commit") != base
        or not final.success
        or final.run_id != receipt.get("final_validation_run_id")
        or final.snapshot_sha256 != receipt.get("snapshot_sha256")
        or not await clean_baseline(journal.repository)
        or any(
            (ready_plan / name).exists()
            for name in (
                "revision.lock",
                "implementation.lock",
                "validation.lock",
                "review.lock",
                "fix-cycle.lock",
            )
        )
        or receipt.get("snapshot_sha256") != (await capture_snapshot(target, base)).fingerprint
    ):
        journal.update(ticket_id, state="WAITING_FOR_HUMAN", error="Final result is stale.")
        return "WAITING_FOR_HUMAN"
    return "READY_FOR_HUMAN_REVIEW"
