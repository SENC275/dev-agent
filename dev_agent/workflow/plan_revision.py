"""Human-requested read-only planning revisions; never grant approval."""

import json
import sqlite3
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from dev_agent.config import CONFIG_NAME, load_config
from dev_agent.git.snapshot import capture_snapshot
from dev_agent.models.agent import AgentTask
from dev_agent.models.config import ProjectConfig
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.investigation import _write
from dev_agent.workflow.persistence import now
from dev_agent.workflow.planning import (
    _json,
    _local_file,
    checked_plan,
    investigation_repository,
    validate_plan,
)


def check_configuration(repository: Path, directory: Path, config: ProjectConfig) -> None:
    path = repository / CONFIG_NAME
    if path.exists() and load_config(path) != config:
        raise ValueError("Configuration changed during plan review; restore it before retrying.")
    database = repository / ".dev-agent/state.sqlite3"
    if database.is_symlink():
        raise ValueError("Refusing symlink journal.")
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute("SELECT config FROM runs WHERE plan_path=?", (str(directory),))
            for (saved,) in rows:
                if ProjectConfig.model_validate_json(saved) != config:
                    raise ValueError(
                        "Configuration changed since start; restore it before revising."
                    )


async def request_changes(
    directory: Path, feedback: str, registry: ProviderRegistry, displayed_text: str
) -> Path:
    if not feedback.strip() or len(feedback.encode("utf-8")) > 32_000:
        raise ValueError("Provide nonempty feedback of at most 32 KB.")
    directory = directory.resolve(strict=True)
    if await checked_plan(directory) != displayed_text:
        raise ValueError("Plan changed after display; review the current plan first.")
    repository = investigation_repository(directory.parent.parent)
    check_configuration(repository, directory, registry.config)
    lock = _local_file(directory, "plan-review.lock")
    try:
        with lock.open("x") as stream:
            stream.write("Human-requested planner revision running.\n")
    except FileExistsError:
        raise ValueError(
            "Plan review is already running; inspect its lock before retrying."
        ) from None
    attempt = None
    try:

        def no_worktree() -> None:
            if any((directory / name).exists() for name in ("worktree.json", "worktree.lock")):
                raise ValueError(
                    "Plan changes require a plan without a worktree; use revise later."
                )

        no_worktree()
        metadata = _json(_local_file(directory, "planning.json"))
        before = await capture_snapshot(repository, str(metadata["base_commit"]))
        history = _local_file(directory, "human-plan-revisions")
        history.mkdir(exist_ok=True)
        previous = []
        for path in sorted(history.iterdir()):
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Invalid plan revision history.")
            status = _json(_local_file(path, "status.json"))
            if status.get("status") == "COMPLETE":
                previous.append(_local_file(path, "feedback.md").read_text())
        attempt = history / (now().replace(":", "-") + "-" + uuid4().hex[:8])
        attempt.mkdir()
        _write(attempt / "before.md", displayed_text)
        _write(attempt / "feedback.md", feedback)
        _write(attempt / "status.json", json.dumps({"status": "RUNNING", "started_at": now()}))
        # An explicit objection invalidates even an earlier approval of identical text.
        _local_file(directory, "approval.json").unlink(missing_ok=True)
        _write(directory / "human-plan-review.json", '{"required":true}\n')
        run = directory.parent.parent
        bundle = {
            "ticket": _local_file(run, "ticket.md").read_text(),
            "current_plan": displayed_text,
            "previous_human_feedback": previous,
            "human_feedback": feedback,
            "evidence": {
                name: _json(_local_file(run, name))
                for name in ("exploration.json", "patterns.json", "tests.json")
            },
        }
        prompt = files("dev_agent").joinpath("prompts/synthesize.md").read_text()
        result = await registry.execute(
            "human_plan_revision",
            directory,
            AgentTask(
                role="planner",
                working_directory=repository,
                read_only=True,
                prompt=prompt + "\nRevise the current plan to address the human feedback. "
                "Preserve prior accepted requirements unless explicitly superseded. "
                "Use the existing investigation; inspect additional code only as needed. "
                "Return the complete nine-section replacement plan, not a patch. "
                "Do not implement, edit repository files, approve the plan or run deployments. "
                "Identify unresolved questions rather than inventing evidence.\nInput bundle:\n"
                + json.dumps(bundle, ensure_ascii=False),
            ),
        )
        _write(attempt / "response.md", result.output)
        _write(attempt / "execution.json", result.model_dump_json(indent=2))
        no_worktree()
        check_configuration(repository, directory, registry.config)
        if (
            await checked_plan(directory) != displayed_text
            or await capture_snapshot(repository, str(metadata["base_commit"])) != before
        ):
            raise ValueError("Plan or repository changed during revision; inspect before retrying.")
        if not result.success or result.exit_code != 0 or result.timed_out:
            raise ValueError("Planner failed; check provider authentication/model/timeout.")
        validate_plan(result.output)
        _local_file(directory, "approval.json").unlink(missing_ok=True)
        _write(directory / "plan.md", result.output)
        _write(attempt / "status.json", json.dumps({"status": "COMPLETE", "finished_at": now()}))
        return attempt
    except BaseException as exc:
        if attempt is not None:
            _write(
                attempt / "status.json",
                json.dumps(
                    {
                        "status": "FAILED" if isinstance(exc, Exception) else "INTERRUPTED",
                        "error": str(exc),
                        "finished_at": now(),
                    }
                ),
            )
        raise
    finally:
        lock.unlink(missing_ok=True)
