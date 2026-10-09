"""Validate read-only checkpoints before resuming; never replay uncertain writes."""

import json
from pathlib import Path
from typing import Any

from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import clean_baseline, git
from dev_agent.workflow.investigation import _write
from dev_agent.workflow.persistence import Journal, now
from dev_agent.workflow.planning import _inputs, _json, validate_plan

READ_ONLY = {"investigate", "plan", "plan_review"}


def active_history(journal: Journal, ticket: str) -> list[dict[str, str | None]]:
    steps = journal.steps(ticket)
    resets = [i for i, step in enumerate(steps) if step["name"] == "recovery_reset"]
    return steps[resets[-1] + 1:] if resets else steps


async def source_identity(repository: Path) -> dict[str, str]:
    base = await git(repository, "rev-parse", "HEAD")
    snapshot = await capture_snapshot(repository, base)
    return {"base_commit": base, "fingerprint": snapshot.fingerprint}


def checkpoint_path(journal: Journal, ticket: str) -> Path:
    run = journal.get(ticket)
    return journal.path.parent / f"recovery-{run['run_id']}.json"


def read_checkpoint(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("Refusing symlink recovery checkpoint.")
    try:
        return _json(path)
    except (OSError, ValueError):
        return {}


async def prepare_resume(journal: Journal, ticket: str) -> None:
    run = journal.get(ticket)
    history = active_history(journal, ticket)
    # A worktree attempt itself can have side effects even before its journal receipt.
    if run["worktree_path"] or any(s["name"] not in READ_ONLY for s in history):
        return
    if run["state"] in {"MERGED", "MERGING", "MERGE_FAILED", "READY_FOR_HUMAN_REVIEW",
                        "WAITING_FOR_HUMAN"}:
        return
    path = checkpoint_path(journal, ticket)
    saved = read_checkpoint(path)
    current = await source_identity(journal.repository)
    reason = None
    keep_investigation = False
    if history:
        if saved.get("source") != current:
            reason = "Source changed or checkpoint unavailable; repeat read-only stages."
        elif run["investigation_path"] and any(
            s["name"] == "investigate" and s["status"] == "COMPLETE" for s in history
        ):
            try:
                if _inputs(Path(run["investigation_path"])) != saved.get("investigation_hashes"):
                    reason = "Investigation artifacts changed; repeat read-only stages."
            except (OSError, ValueError):
                reason = "Investigation artifacts missing or invalid; repeat read-only stages."
            if reason is None and run["plan_path"]:
                try:
                    plan = Path(run["plan_path"])
                    validate_plan((plan / "plan.md").read_text())
                    metadata = _json(plan / "planning.json")
                    if (metadata.get("input_hashes") != saved.get("investigation_hashes")
                            or metadata.get("base_commit") != current["base_commit"]):
                        raise ValueError("Stale plan metadata")
                except (OSError, ValueError):
                    reason = "Plan missing or invalid; reuse investigation and regenerate plan."
                    keep_investigation = True
    if reason:
        if not await clean_baseline(journal.repository):
            raise ValueError(
                "Source changed before resume. Commit or stash source changes, then resume; "
                "the old investigation and plan will be preserved and regenerated."
            )
        # Keep every old step and artifact. A marker defines a new active generation.
        with journal.connect() as db:
            db.execute(
                "INSERT INTO steps(run_id,name,status,started_at,finished_at,artifact_path,error) "
                "VALUES(?,'recovery_reset','COMPLETE',?,?,?,?)",
                (run["run_id"], now(), now(), run["investigation_path"], reason),
            )
            if keep_investigation:
                db.execute(
                    "INSERT INTO steps(run_id,name,status,started_at,finished_at,"
                    "artifact_path,error) "
                    "VALUES(?,'investigate','COMPLETE',?,?,?,'Reused verified investigation')",
                    (run["run_id"], now(), now(), run["investigation_path"]),
                )
            db.execute(
                "UPDATE runs SET state='NEW',current_step=NULL,investigation_path=?,"
                "plan_path=NULL,error=NULL,updated_at=? WHERE ticket_id=?",
                (run["investigation_path"] if keep_investigation else None, now(), ticket),
            )
        if not keep_investigation:
            saved = {}
    if not saved:
        _write(path, json.dumps({"source": current}, indent=2) + "\n")


async def save_checkpoint(journal: Journal, ticket: str) -> None:
    path = checkpoint_path(journal, ticket)
    saved = read_checkpoint(path)
    if saved.get("source") != await source_identity(journal.repository):
        raise ValueError("Source changed during read-only work; restore clean source and resume.")
    run = journal.get(ticket)
    if run["investigation_path"]:
        saved["investigation_hashes"] = _inputs(Path(run["investigation_path"]))
    _write(path, json.dumps(saved, indent=2) + "\n")
