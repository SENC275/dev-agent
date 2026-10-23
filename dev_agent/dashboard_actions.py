"""Dashboard workers delegate to the same workflow functions as the CLI."""

import asyncio
import fcntl
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dev_agent.config import CONFIG_NAME, load_config
from dev_agent.context import safe, save
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.git.worktree import clean_baseline
from dev_agent.models.config import ProjectConfig
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.merging import MergePreview
from dev_agent.workflow.persistence import Journal, now
from dev_agent.workflow.planning import approve_plan, checked_plan, edit_plan
from dev_agent.workflow.runner import advance

ACTIONS = {
    "draft",
    "save-draft",
    "start",
    "resume",
    "approve-plan",
    "edit-plan",
    "request-changes",
    "revise",
    "sync",
    "sync-continue",
    "merge",
    "rag-index",
    "doctor",
    "init",
    "configure",
}
PREVIEWS = {
    "approve-plan",
    "edit-plan",
    "request-changes",
    "merge",
    "start",
    "save-draft",
    "configure",
}


def ticket_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise ValueError("Invalid ticket ID")
    return value


def draft_path(repository: Path, ticket: str) -> Path:
    path = repository / ".dev-agent/tickets" / (ticket_id(ticket) + ".md")
    if not safe(path):
        raise ValueError("Unsafe draft path")
    return path


def text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 64000:
        raise ValueError("Provide nonempty text of at most 64 KB")
    return value


async def preview(repository: Path, ticket: str, action: str) -> dict[str, Any]:
    if action == "configure":
        path = repository / CONFIG_NAME
        if not safe(path):
            raise ValueError("Unsafe config path")
        return {"text": path.read_text() if path.exists() else "", "action": action}
    if action in {"start", "save-draft"}:
        path = draft_path(repository, ticket)
        return {"text": path.read_text() if path.exists() else "", "action": action}
    journal = Journal(repository)
    run = journal.get(ticket_id(ticket))
    if action == "merge":
        from dev_agent.workflow.merging import preview_merge

        value = await preview_merge(journal, ticket)
        snapshot = await capture_snapshot(value.target, value.base)
        return {
            "action": action,
            "merge": {**asdict(value), "target": str(value.target)},
            "text": snapshot.diff,
            "summary": value.summary,
            "destination": value.destination,
            "plan_path": run["plan_path"],
        }
    if action not in {"approve-plan", "edit-plan", "request-changes"}:
        raise ValueError("Unknown preview action")
    if not run["plan_path"]:
        raise ValueError("No plan yet")
    path = Path(run["plan_path"])
    if (path / "worktree.json").exists() or (path / "worktree.lock").exists():
        raise ValueError("Implementation already started; use revise after review.")
    return {"action": action, "plan_path": str(path), "text": await checked_plan(path)}


async def current_diff(repository: Path, ticket: str) -> dict[str, str]:
    from dev_agent.git.worktree import git
    from dev_agent.workflow.planning import _json

    run = Journal(repository).get(ticket_id(ticket))
    if not run["plan_path"] or not run["worktree_path"]:
        raise ValueError("No worktree yet")
    plan = Path(run["plan_path"])
    record = _json(plan / "worktree.json")
    target = Path(run["worktree_path"])
    if str(target) != record["path"]:
        raise ValueError("Worktree identity mismatch")
    if await git(target, "rev-parse", "--path-format=absolute", "--git-common-dir") != await git(
        repository, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ):
        raise ValueError("Worktree does not belong to this project")
    snapshot = await capture_snapshot(target, str(record["base_commit"]))
    if len(snapshot.diff.encode()) > 1_000_000:
        raise ValueError("Diff too large; inspect with Git.")
    return {"text": snapshot.diff, "fingerprint": snapshot.fingerprint}


async def execute(repository: Path, request: dict[str, Any]) -> dict[str, Any]:
    action = request["action"]
    if action not in ACTIONS:
        raise ValueError("Unsupported action")
    payload = request.get("payload", {})
    accepted = request.get("accepted")
    if action in PREVIEWS and not isinstance(accepted, dict):
        raise ValueError("Preview and confirmation required")
    if action == "init":
        from dev_agent.config import initialize_config

        return {"path": str(initialize_config(repository))}
    if action == "configure":
        import yaml

        assert accepted is not None
        current = await preview(repository, "", action)
        if current != accepted:
            raise ValueError("Configuration changed after display; reload.")
        content = text(payload.get("text"))
        ProjectConfig.model_validate(yaml.safe_load(content))
        from dev_agent.workflow.investigation import _write

        _write(repository / CONFIG_NAME, content)
        return {"saved": True, "note": "Existing runs keep their frozen configuration."}
    if action == "doctor":
        from dataclasses import asdict

        from dev_agent.doctor import check_environment

        return {"checks": [asdict(c) for c in check_environment(repository)]}
    config = load_config(repository / CONFIG_NAME)
    if action == "rag-index":
        from dev_agent.rag import refresh

        return await refresh(repository, config.rag)
    ticket = ticket_id(request.get("ticket"))
    if action == "save-draft":
        assert accepted is not None
        if await preview(repository, ticket, action) != accepted:
            raise ValueError("Draft changed after display; reload.")
        path = draft_path(repository, ticket)
        path.parent.mkdir(parents=True, exist_ok=True)
        from dev_agent.workflow.investigation import _write

        _write(path, text(payload.get("text")))
        return {"path": str(path)}
    registry = ProviderRegistry(config)
    if action == "draft":
        from dev_agent.workflow.ticket import generate_ticket

        path = await generate_ticket(repository, ticket, text(payload.get("text")), registry)
        return {"path": str(path), "draft_only": True}
    journal = Journal(repository)
    if action == "start":
        assert accepted is not None
        if await preview(repository, ticket, action) != accepted:
            raise ValueError("Draft changed after display; reload.")
        if not await clean_baseline(repository):
            raise ValueError("Start requires a clean committed project baseline.")
        journal.create(ticket, text(accepted["text"]), config.model_dump_json())
    elif ProjectConfig.model_validate_json(journal.get(ticket)["config"] or "{}") != config:
        raise ValueError("Configuration changed since start; restore it before continuing.")
    if action in {"approve-plan", "edit-plan", "request-changes"}:
        assert accepted is not None
        with journal.lock(ticket):
            if await preview(repository, ticket, action) != accepted:
                raise ValueError("Plan changed after display; reload.")
            path = Path(str(accepted["plan_path"]))
            if action == "approve-plan":
                await approve_plan(path, accepted["text"])
            elif action == "edit-plan":
                await edit_plan(path, text(payload.get("text")), accepted["text"])
                return {"state": "AWAITING_PLAN_APPROVAL"}
            else:
                from dev_agent.workflow.plan_revision import request_changes

                attempt = await request_changes(
                    path, text(payload.get("text")), registry, accepted["text"]
                )
                return {"state": "AWAITING_PLAN_APPROVAL", "attempt": str(attempt)}
    if action == "revise":
        from dev_agent.workflow.revision import revise

        state = await revise(journal, ticket, text(payload.get("text")), config, registry)
    elif action in {"sync", "sync-continue"}:
        from dev_agent.workflow.synchronization import synchronize

        state = await synchronize(
            journal, ticket, config, registry, continue_sync=action == "sync-continue"
        )
    elif action == "merge":
        from dev_agent.workflow.merging import merge

        assert accepted is not None
        approved = MergePreview(
            **{**accepted["merge"], "target": Path(accepted["merge"]["target"])}
        )
        commit = await merge(journal, ticket, approved, f"Complete {ticket}")
        return {"state": "MERGED", "commit": commit}
    else:
        cycles = payload.get("additional_fix_cycles", 0)
        if type(cycles) is not int or not 0 <= cycles <= 10:
            raise ValueError("Additional fix cycles must be 0–10")
        state = await advance(journal, ticket, config, registry, additional_fix_cycles=cycles)
    return {
        "state": state,
        "error": journal.get(ticket).get("error"),
        "plan_path": journal.get(ticket).get("plan_path"),
    }


def main(path: Path) -> None:
    request = json.loads(path.read_text())
    repository = Path(request["repository"]).resolve(strict=True)
    receipt = path.with_name("result.json")
    from dev_agent.local import protect_local_files

    protect_local_files(repository)
    lock = repository / ".dev-agent/dashboard-action.lock"
    try:
        if not safe(lock):
            raise ValueError("Unsafe action lock")
        with lock.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another dashboard action is running for this project.") from None
            result = asyncio.run(execute(repository, request))
        save(receipt, {"status": "COMPLETE", "result": result, "finished_at": now()})
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    except BaseException as exc:
        save(receipt, {"status": "FAILED", "error": str(exc), "finished_at": now()})
        print(str(exc), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main(Path(sys.argv[1]))
