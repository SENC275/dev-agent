"""Read-only planning and content-bound human approval records."""

import hashlib
import json
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from dev_agent.artifacts import parse_artifact
from dev_agent.git.worktree import clean_baseline
from dev_agent.models.agent import AgentTask
from dev_agent.models.investigation import Exploration, PatternAnalysis, TestAnalysis
from dev_agent.models.plan_review import PlanReview
from dev_agent.process import run_process
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.investigation import _write

HEADINGS = (
    "Summary",
    "Current Behavior",
    "Proposed Change",
    "Expected Files",
    "Existing Patterns",
    "Risks",
    "Test Plan",
    "Assumptions",
    "Unresolved Questions",
)
INPUTS = ("ticket.md", "exploration.json", "patterns.json", "tests.json", "investigation.json")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local_file(directory: Path, name: str) -> Path:
    path = directory / name
    if path.is_symlink():
        raise ValueError(f"Refusing symlink artifact: {name}")
    return path


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid object in {path.name}")
    return value


def _inputs(directory: Path) -> dict[str, str]:
    return {name: _hash(_local_file(directory, name).read_bytes()) for name in INPUTS}


def validate_plan(text: str) -> None:
    sections = list(re.finditer(r"^## ([^\n]+)\s*$", text, re.MULTILINE))
    found = [section.group(1).strip() for section in sections]
    if found != list(HEADINGS):
        raise ValueError("Plan must contain the nine required headings in order.")
    for index, section in enumerate(sections):
        end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
        if not text[section.end() : end].strip():
            raise ValueError(f"Plan section '{found[index]}' must not be empty.")


async def _head(repository: Path) -> str:
    result = await run_process(["git", "rev-parse", "--verify", "HEAD"], cwd=repository, timeout=10)
    if result.timed_out or result.exit_code != 0:
        raise ValueError("Repository must have an existing Git commit before planning.")
    return result.stdout.strip()


def investigation_repository(run: Path) -> Path:
    info = _json(_local_file(run, "investigation.json"))
    if info.get("status") != "COMPLETE":
        raise ValueError("Planning requires a COMPLETE investigation.")
    raw = info.get("repository")
    if not isinstance(raw, str):
        raise ValueError("Investigation repository is missing.")
    repository = Path(raw).resolve(strict=True)
    if not run.is_relative_to(repository / ".dev-agent" / "runs"):
        raise ValueError("Investigation directory does not belong to its recorded repository.")
    return repository


async def generate_plan(run: Path, registry: ProviderRegistry) -> Path:
    run = run.resolve(strict=True)
    repository = investigation_repository(run)
    hashes = _inputs(run)
    ticket = _local_file(run, "ticket.md").read_text(encoding="utf-8")
    if not ticket.strip():
        raise ValueError("Ticket must not be empty.")
    bundle: dict[str, object] = {"ticket": ticket}
    for name, model in (
        ("exploration.json", Exploration),
        ("patterns.json", PatternAnalysis),
        ("tests.json", TestAnalysis),
    ):
        bundle[name] = parse_artifact((run / name).read_text(encoding="utf-8"), model).model_dump()
    base = await _head(repository)
    clean = await clean_baseline(repository)
    prompt = files("dev_agent").joinpath("prompts/synthesize.md").read_text(encoding="utf-8")
    result = await registry.resolve("planner").execute(
        AgentTask(
            role="planner",
            prompt=prompt + "\nInput bundle:\n" + json.dumps(bundle, ensure_ascii=False),
            working_directory=repository,
            read_only=True,
        )
    )
    if not result.success or result.exit_code != 0 or result.timed_out:
        raise ValueError("Planner failed; check provider authentication, model and timeout.")
    validate_plan(result.output)
    if _inputs(run) != hashes or await _head(repository) != base:
        raise ValueError(
            "Investigation inputs or Git HEAD changed during planning; rerun planning."
        )
    clean = clean and await clean_baseline(repository)
    directory = run / "plans" / uuid4().hex
    if not directory.resolve().is_relative_to(run):
        raise ValueError("Plan directory must stay inside the investigation directory.")
    directory.mkdir(parents=True, exist_ok=False)
    _write(directory / "plan.md", result.output)
    _write(
        directory / "planning.json",
        json.dumps(
            {
                "status": "AWAITING_PLAN_APPROVAL",
                "base_commit": base,
                "clean_baseline": clean,
                "input_hashes": hashes,
                "created_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
        + "\n",
    )
    return directory


async def checked_plan(directory: Path) -> str:
    directory = directory.resolve(strict=True)
    if directory.parent.name != "plans":
        raise ValueError("Expected a plan directory under a run's plans directory.")
    run = directory.parent.parent
    repository = investigation_repository(run)
    metadata = _json(_local_file(directory, "planning.json"))
    if metadata.get("input_hashes") != _inputs(run):
        raise ValueError("Investigation inputs changed; generate a new plan.")
    if metadata.get("base_commit") != await _head(repository):
        raise ValueError("Git HEAD changed; generate a new plan.")
    text = _local_file(directory, "plan.md").read_text(encoding="utf-8")
    validate_plan(text)
    return text


async def approve_plan(
    directory: Path,
    displayed_text: str,
    *,
    actor: str = "human",
    review_sha256: str | None = None,
) -> None:
    if actor not in {"human", "agent"} or (actor == "agent" and not review_sha256):
        raise ValueError("Invalid approval provenance.")
    if actor == "human" and (directory / "plan-review.lock").exists():
        raise ValueError("Plan reviewer is running; wait before approving.")
    current = await checked_plan(directory)
    if current != displayed_text:
        raise ValueError("Plan changed after display; review it again before approving.")
    metadata = _json(_local_file(directory, "planning.json"))
    _write(
        directory / "approval.json",
        json.dumps(
            {
                "decision": "APPROVED",
                "actor": actor,
                "review_sha256": review_sha256,
                "approved_at": datetime.now(UTC).isoformat(),
                "plan_sha256": _hash(current.encode("utf-8")),
                "input_hashes": metadata["input_hashes"],
                "base_commit": metadata["base_commit"],
                "clean_baseline": metadata.get("clean_baseline", False),
            },
            indent=2,
        )
        + "\n",
    )


async def approval_is_current(directory: Path) -> bool:
    text = await checked_plan(directory)
    path = _local_file(directory, "approval.json")
    if not path.exists():
        return False
    approval = _json(path)
    metadata = _json(_local_file(directory, "planning.json"))
    actor = approval.get("actor", "human")
    if actor not in {"human", "agent"}:
        return False
    if actor == "agent":
        review_path = _local_file(directory, "plan-review.json")
        if not review_path.exists() or _hash(review_path.read_bytes()) != approval.get(
            "review_sha256"
        ):
            return False
        review = _json(review_path)
        decision = parse_artifact(json.dumps(review.get("review")), PlanReview)
        if (
            decision.decision != "approve"
            or review.get("plan_sha256") != _hash(text.encode("utf-8"))
            or review.get("input_hashes") != metadata["input_hashes"]
            or review.get("base_commit") != metadata["base_commit"]
        ):
            return False
    return (
        approval.get("decision") == "APPROVED"
        and approval.get("plan_sha256") == _hash(text.encode("utf-8"))
        and approval.get("input_hashes") == metadata["input_hashes"]
        and approval.get("base_commit") == metadata["base_commit"]
        and approval.get("clean_baseline", False) == metadata.get("clean_baseline", False)
    )


async def edit_plan(directory: Path, text: str, displayed_text: str) -> None:
    if await checked_plan(directory) != displayed_text:
        raise ValueError("Plan changed during editing; review the current version first.")
    validate_plan(text)
    _local_file(directory, "approval.json").unlink(missing_ok=True)
    _write(directory / "plan.md", text)
