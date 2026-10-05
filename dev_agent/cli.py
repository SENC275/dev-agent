"""User-facing command line interface."""

import asyncio
import json
import shlex
import sqlite3
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dev_agent.artifacts import parse_artifact
from dev_agent.config import CONFIG_NAME, ConfigurationError, initialize_config, load_config
from dev_agent.doctor import check_environment, is_git_repository
from dev_agent.git.worktree import clean_baseline, create_approved_worktree
from dev_agent.models.finding import Review
from dev_agent.process import run_process
from dev_agent.progress import activity
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.validation.runner import ValidationRun, failure_summary
from dev_agent.validation.runner import validate as run_validation
from dev_agent.workflow.agent_plan_gate import review_plan_by_agent
from dev_agent.workflow.fixing import fix as run_fix
from dev_agent.workflow.implementation import implement as run_implementation
from dev_agent.workflow.investigation import investigate as run_investigation
from dev_agent.workflow.persistence import Journal
from dev_agent.workflow.plan_gate import review_plan as run_plan_gate
from dev_agent.workflow.planning import generate_plan, investigation_repository
from dev_agent.workflow.review import review as run_review
from dev_agent.workflow.revision import revise as run_revision
from dev_agent.workflow.runner import advance
from dev_agent.workflow.ticket import generate_ticket

app = typer.Typer(
    no_args_is_help=True,
    help="Local engineering workflow CLI. Plan, implement, validate, review, and resume.",
)
console = Console(highlight=False)


@app.command()
def init() -> None:
    """Create .dev-agent.yaml with Codex defaults; never overwrite existing files."""
    try:
        path = initialize_config(Path.cwd())
    except FileExistsError:
        console.print(
            f"{CONFIG_NAME} already exists; kept unchanged. Local Git exclusions configured.",
            markup=False,
        )
        raise typer.Exit(1) from None
    except (OSError, ValueError):
        console.print("Cannot create configuration; check directory permissions.", markup=False)
        raise typer.Exit(1) from None
    console.print(f"Created {path}\nNext: dev-agent doctor", markup=False)


@app.command()
def doctor() -> None:
    """Check Python, Git, configuration, and required providers without calling AI."""
    checks = check_environment(Path.cwd())
    section = ""
    for check in checks:
        if check.section != section:
            section = check.section
            console.print(f"\n{section}", style="bold", markup=False)
        symbol = "✓" if check.passed else ("✗" if check.required else "○")
        console.print(f"{symbol} {check.name}  {check.detail}", markup=False)
    failed = any(not check.passed and check.required for check in checks)
    console.print("\nFix the checks above, then rerun dev-agent doctor." if failed else "\nReady.")
    if failed:
        raise typer.Exit(1)


@app.command("test")
def test_command(
    config_file: Annotated[
        Path,
        typer.Option("--config", help="Config file; tests still run in the current directory."),
    ] = Path(CONFIG_NAME),
) -> None:
    """Run only commands.test in the current directory, without AI or a ticket."""
    try:
        config = load_config(config_file)
        arguments = shlex.split(config.commands.test)
        if not arguments:
            raise ValueError("commands.test must not be empty.")
        console.print(f"Test directory: {Path.cwd()}", markup=False)
        console.print(f"Command: {config.commands.test}", markup=False)
        with activity(console, "Running configured test command…"):
            result = asyncio.run(
                run_process(arguments, cwd=Path.cwd(), timeout=config.validation.timeout_seconds)
            )
    except (ConfigurationError, ValueError, OSError) as exc:
        console.print(f"Test could not run: {exc}", markup=False)
        raise typer.Exit(1) from None
    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.stderr:
        typer.echo(result.stderr, err=True, nl=False)
    if result.timed_out:
        console.print(f"Test timed out after {config.validation.timeout_seconds:g}s.", markup=False)
        raise typer.Exit(124)
    if result.exit_code:
        console.print(f"Test failed (exit {result.exit_code}).", markup=False)
        raise typer.Exit(result.exit_code if result.exit_code > 0 else 128 - result.exit_code)
    console.print(f"Test passed ({result.duration_seconds:.1f}s).", markup=False)


@app.command()
def ticket(
    ticket_id: str,
    description: Annotated[str | None, typer.Option("--description", "-d")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", exists=True, dir_okay=False, readable=True)
    ] = None,
) -> None:
    """Draft .dev-agent/tickets/ID.md from a description using the read-only planner."""
    try:
        if description is not None and file is not None:
            raise ValueError("Use either --description or --file, not both.")
        if file is not None:
            if file.stat().st_size > 32_000:
                raise ValueError("Description file exceeds 32 KB.")
            description = file.read_text(encoding="utf-8")
        if description is None:
            if not sys.stdin.isatty():
                raise ValueError("Provide --description or --file in a noninteractive terminal.")
            description = typer.prompt("需求描述")
        repository = Path.cwd()
        config = load_config(repository / CONFIG_NAME)
        with activity(console, "Reading repository and drafting ticket…"):
            output = asyncio.run(
                generate_ticket(repository, ticket_id, description, ProviderRegistry(config))
            )
        console.print(f"Draft: {output}", markup=False)
        console.print(
            "Review the local draft and resolve open questions. No ticket commit is required.",
            markup=False,
        )
        console.print(
            f"Next: dev-agent start {ticket_id} --file .dev-agent/tickets/{ticket_id}.md",
            markup=False,
        )
    except (ValueError, OSError, ConfigurationError) as exc:
        console.print(f"Ticket generation stopped: {exc}", markup=False)
        raise typer.Exit(1) from None


@app.command()
def investigate(
    ticket_id: str,
    file: Annotated[Path, typer.Option("--file", exists=True, dir_okay=False, readable=True)],
) -> None:
    """Run three parallel read-only investigations; stop before planning."""
    try:
        repository = Path.cwd()
        config = load_config(repository / CONFIG_NAME)
        if not is_git_repository(repository):
            raise ValueError("Run inside a Git working tree with .dev-agent.yaml.")
        ticket = file.read_text(encoding="utf-8")
        console.print("Investigating code paths, existing patterns, and tests…", markup=False)
        with activity(console, "Three read-only investigation roles running…"):
            result = asyncio.run(
                run_investigation(
                    ticket_id=ticket_id,
                    ticket=ticket,
                    repository=repository,
                    registry=ProviderRegistry(config),
                )
            )
    except (ConfigurationError, ValueError) as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from None
    except (OSError, UnicodeError):
        console.print(
            "Cannot read ticket, run Git, or write artifacts; check paths and permissions."
        )
        raise typer.Exit(1) from None
    console.print(f"Artifacts: {result.directory}", markup=False)
    if not result.success:
        for role, error in result.errors.items():
            console.print(f"✗ {role}: {error}", markup=False)
        raise typer.Exit(1)
    console.print("✓ Investigation complete: exploration.json, patterns.json, tests.json")


@app.command()
def plan(run: Path) -> None:
    """Generate a plan and apply the configured human or agent review gate."""
    try:
        repository = investigation_repository(run.resolve(strict=True))
        config = load_config(repository / CONFIG_NAME)
        with activity(console, "Generating plan in a fresh read-only session…"):
            directory = asyncio.run(generate_plan(run, ProviderRegistry(config)))
        console.print(f"Plan directory: {directory}", markup=False)
        approved = False
        if config.gates.plan_review == "agent":
            with activity(console, "Independent agent reviewing the plan…"):
                approved = asyncio.run(review_plan_by_agent(directory, ProviderRegistry(config)))
            if not approved:
                console.print(
                    f"Plan reviewer requested changes. See {directory / 'plan-review.json'}"
                )
        if not approved:
            approved = run_plan_gate(directory, console)
    except (ValueError, ConfigurationError, OSError) as exc:
        console.print(f"Planning failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    if not approved:
        raise typer.Exit(2)


@app.command("review-plan")
def review_plan_command(directory: Path) -> None:
    """Review a saved plan: approve, edit with VISUAL/EDITOR, or quit."""
    try:
        approved = run_plan_gate(directory, console)
    except (ValueError, OSError) as exc:
        console.print(f"Plan review failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    if not approved:
        raise typer.Exit(2)


@app.command()
def worktree(directory: Path) -> None:
    """Create an isolated worktree from a currently approved plan."""
    try:
        directory = directory.resolve(strict=True)
        repository = investigation_repository(directory.parent.parent)
        config = load_config(repository / CONFIG_NAME)
        result = asyncio.run(create_approved_worktree(directory, config.git))
    except (ValueError, OSError, ConfigurationError) as exc:
        console.print(f"Worktree creation failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    console.print(f"Worktree: {result.path}\nBranch: {result.branch}", markup=False)
    console.print("Worktree ready. Implementation has not started.")


@app.command()
def implement(directory: Path) -> None:
    """Implement one approved plan in its worktree; validation is a separate stage."""
    try:
        directory = directory.resolve(strict=True)
        repository = investigation_repository(directory.parent.parent)
        config = load_config(repository / CONFIG_NAME)
        with activity(console, "Implementer running in the isolated worktree…"):
            result = asyncio.run(run_implementation(directory, ProviderRegistry(config)))
    except (ValueError, OSError, ConfigurationError) as exc:
        console.print(f"Implementation failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    console.print(f"Execution record: {result.directory / 'implementation.json'}", markup=False)
    console.print(f"Changed files: {len(result.changed_files)}; validation has not run.")
    if not result.success:
        console.print(
            "Provider execution failed. Inspect the record and worktree before continuing."
        )
        raise typer.Exit(1)
    console.print("Implementation attempt finished. Changes require validation and review.")


@app.command()
def validate(directory: Path) -> None:
    """Run configured test, lint and typecheck commands inside the implementation worktree."""
    try:
        directory = directory.resolve(strict=True)
        repository = investigation_repository(directory.parent.parent)
        config = load_config(repository / CONFIG_NAME)
        with activity(console, "Running deterministic validation in the worktree…"):
            result = asyncio.run(run_validation(directory, config.commands, config.validation))
    except (ValueError, OSError, ConfigurationError) as exc:
        console.print(f"Validation failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    for item in result.commands:
        console.print(f"{item.name}: {item.status} (exit={item.exit_code})", markup=False)
        if item.error:
            console.print(item.error, markup=False)
    console.print(f"Results: {result.directory / 'validation.json'}", markup=False)
    if not result.success:
        raise typer.Exit(1)
    console.print("Validation passed. Independent review has not run.")


@app.command()
def review(directory: Path) -> None:
    """Independently review the validated change with a fresh read-only agent."""
    try:
        directory = directory.resolve(strict=True)
        repository = investigation_repository(directory.parent.parent)
        config = load_config(repository / CONFIG_NAME)
        with activity(console, "Independent reviewer inspecting the validated change…"):
            output = asyncio.run(run_review(directory, ProviderRegistry(config)))
        findings = parse_artifact((output / "review.json").read_text(encoding="utf-8"), Review)
    except (ValueError, OSError, ConfigurationError) as exc:
        console.print(f"Review failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    console.print(f"Review: {output / 'review.json'}", markup=False)
    for level in ("critical", "high", "medium", "low"):
        count = sum(finding.severity == level for finding in findings.findings)
        console.print(f"{level}: {count}")
    console.print("Findings are advisory. No fixes, approval or merge performed.")


@app.command()
def fix(directory: Path) -> None:
    """Verify findings, fix within the configured budget, validate, and independently review."""
    try:
        directory = directory.resolve(strict=True)
        repository = investigation_repository(directory.parent.parent)
        config = load_config(repository / CONFIG_NAME)
        with activity(console, "Running bounded fix, validation and review cycles…"):
            status = asyncio.run(run_fix(directory, config, ProviderRegistry(config)))
    except (ValueError, OSError, ConfigurationError) as exc:
        console.print(f"Fix cycle failed: {exc}", markup=False)
        raise typer.Exit(1) from None
    console.print(f"{status}\nRecord: {directory / 'fix-cycle.json'}", markup=False)
    if status != "READY_FOR_HUMAN_REVIEW":
        raise typer.Exit(2)
    console.print("Inspect the final diff. No commit, merge or deployment performed.")


@app.command()
def start(
    ticket_id: str,
    file: Annotated[Path, typer.Option("--file", exists=True, dir_okay=False, readable=True)],
) -> None:
    """Start a durable workflow using the configured plan approval gate."""
    _managed(ticket_id, file)


@app.command()
def resume(
    ticket_id: str,
    additional_fix_cycles: Annotated[int, typer.Option(min=0, max=10)] = 0,
) -> None:
    """Continue a saved workflow without replaying uncertain writes."""
    _managed(ticket_id, additional_fix_cycles=additional_fix_cycles)


def _managed(ticket_id: str, file: Path | None = None, *, additional_fix_cycles: int = 0) -> None:
    try:
        repository = Path.cwd().resolve()
        config = load_config(repository / CONFIG_NAME)
        if not is_git_repository(repository):
            raise ValueError("Run from the repository root with .dev-agent.yaml.")
        journal = Journal(repository)
        if file is not None:
            if not asyncio.run(clean_baseline(repository)):
                raise ValueError(
                    "Start requires a clean, committed source baseline. "
                    "Inspect git status --short; commit changes and ignore generated files."
                )
            journal.create(ticket_id, file.read_text(encoding="utf-8"), config.model_dump_json())
        with activity(console, "Workflow running…", lambda: _stage(journal, ticket_id)):
            state = asyncio.run(
                advance(
                    journal,
                    ticket_id,
                    config,
                    ProviderRegistry(config),
                    additional_fix_cycles=additional_fix_cycles,
                )
            )
        if state == "AWAITING_PLAN_APPROVAL":
            plan_path = journal.get(ticket_id)["plan_path"]
            assert plan_path is not None
            console.print(f"Plan: {plan_path}", markup=False)
            if config.gates.plan_review == "agent":
                console.print(
                    f"[plan_rejected] Automatic plan revisions exhausted. "
                    f"Review: {Path(plan_path) / 'plan-review.json'}",
                    markup=False,
                )
            if run_plan_gate(Path(plan_path), console):
                with activity(
                    console, "Approved workflow running…", lambda: _stage(journal, ticket_id)
                ):
                    state = asyncio.run(
                        advance(journal, ticket_id, config, ProviderRegistry(config))
                    )
        console.print(journal.formatted(ticket_id), markup=False)
        if state != "READY_FOR_HUMAN_REVIEW":
            paused = journal.get(ticket_id)
            if paused["current_step"] in {"validation_fix", "fix"} and paused["plan_path"]:
                report = ValidationRun.model_validate_json(
                    (Path(paused["plan_path"]) / "validation.json").read_bytes()
                )
                if not report.success:
                    console.print(
                        f"[validation_failed] {failure_summary(report)}. "
                        f"Inspect {Path(paused['plan_path']) / 'validation.json'}; "
                        f"after fixing the cause: dev-agent resume {ticket_id}",
                        markup=False,
                    )
            raise typer.Exit(2)
        console.print("Ready for human review. Inspect the final diff in the worktree.")
        if config.knowledge.enabled:
            console.print(
                f"Also review docs/knowledge/{ticket_id}.md in that worktree.", markup=False
            )
    except (ValueError, OSError, ConfigurationError, sqlite3.Error) as exc:
        console.print(f"Workflow stopped: {exc}", markup=False)
        console.print(f"Inspect: dev-agent status {ticket_id}", markup=False)
        raise typer.Exit(1) from None


def _stage(journal: Journal, ticket_id: str) -> str:
    try:
        run = journal.get(ticket_id)
        label = f"{ticket_id}: {run['current_step'] or run['state']}"
        if run["current_step"] == "plan_review" and run["plan_path"]:
            progress = Path(run["plan_path"]) / "plan-review-status.json"
            if progress.exists():
                label += ":" + str(json.loads(progress.read_text()).get("stage", "review"))
        if run["current_step"] == "validation_fix" and run["plan_path"]:
            report = json.loads((Path(run["plan_path"]) / "validation.json").read_text())
            if report.get("status") == "FAILED":
                label += f" ({failure_summary(ValidationRun.model_validate(report))} → fixer)"
        return label
    except (ValueError, OSError, sqlite3.Error):
        return f"{ticket_id}: waiting for workflow status"


@app.command()
def revise(
    ticket_id: str,
    file: Annotated[
        Path | None, typer.Option("--file", exists=True, dir_okay=False, readable=True)
    ] = None,
) -> None:
    """Apply human feedback in the existing worktree, then validate and review again."""
    try:
        journal = Journal(Path.cwd())
        run = journal.get(ticket_id)
        if run["state"] != "READY_FOR_HUMAN_REVIEW":
            raise ValueError("Revise requires a completed run ready for human review.")
        config = load_config(journal.repository / CONFIG_NAME)
        if file is not None:
            feedback = file.read_text(encoding="utf-8")
        else:
            if not sys.stdin.isatty():
                raise ValueError("Use --file feedback.md in a noninteractive terminal.")
            console.print(
                "Describe your changes; press Enter to submit. Use --file for multiline feedback."
            )
            feedback = typer.prompt("Revision feedback", default="", show_default=False)
        with activity(console, "Applying your feedback…", lambda: _stage(journal, ticket_id)):
            state = asyncio.run(
                run_revision(journal, ticket_id, feedback, config, ProviderRegistry(config))
            )
        console.print(journal.formatted(ticket_id), markup=False)
        if state != "READY_FOR_HUMAN_REVIEW":
            raise typer.Exit(2)
        console.print("Revision ready for human review. Inspect the final diff in the worktree.")
        if config.knowledge.enabled:
            console.print(
                f"Also review docs/knowledge/{ticket_id}.md in that worktree.", markup=False
            )
    except (ValueError, OSError, ConfigurationError, sqlite3.Error) as exc:
        console.print(f"Revision stopped: {exc}", markup=False)
        console.print(f"Inspect: dev-agent status {ticket_id}", markup=False)
        raise typer.Exit(1) from None


@app.command()
def merge(
    ticket_id: str,
    yes: Annotated[
        bool, typer.Option("--yes", help="Explicitly accept and merge without prompting.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Check and show changes without committing.")
    ] = False,
    message: Annotated[str | None, typer.Option("--message", "-m")] = None,
) -> None:
    """Accept reviewed changes, commit if needed, and fast-forward into the source branch."""
    from dev_agent.workflow.merging import merge as merge_workflow
    from dev_agent.workflow.merging import preview_merge

    try:
        journal = Journal(Path.cwd())
        preview = asyncio.run(preview_merge(journal, ticket_id))
        console.print(
            f"{preview.branch} → {preview.destination}\nWorktree: {preview.target}", markup=False
        )
        console.print(preview.summary or "No file changes", markup=False)
        if (preview.target / "docs" / "knowledge" / f"{ticket_id}.md").is_file():
            console.print(
                f"Review knowledge candidates: docs/knowledge/{ticket_id}.md\n"
                "Merge accepts this document together with the code. "
                "Use revise to remove or correct candidates before accepting.",
                markup=False,
            )
        console.print(
            "This commits reviewed changes and fast-forwards the source branch. "
            "No push, migration, deployment, or worktree deletion."
        )
        if dry_run:
            return
        if not yes:
            if not sys.stdin.isatty():
                raise ValueError("Use an interactive terminal or explicitly pass --yes.")
            if not typer.confirm("Accept and merge these changes?", default=False):
                raise typer.Exit(2)
        commit = asyncio.run(
            merge_workflow(journal, ticket_id, preview, message or f"Complete {ticket_id}")
        )
        console.print(f"Merged {commit} into {preview.destination}.", markup=False)
    except (ValueError, OSError, sqlite3.Error) as exc:
        console.print(f"Merge stopped: {exc}", markup=False)
        raise typer.Exit(1) from None


@app.command()
def status(ticket_id: str) -> None:
    """Show persisted state and step history without calling a provider."""
    try:
        journal = Journal(Path.cwd())
        console.print(journal.formatted(ticket_id), markup=False)
        for step in journal.steps(ticket_id):
            console.print(
                f"{step['name']}: {step['status']}  {step['artifact_path'] or ''}", markup=False
            )
        console.print("State is the last recorded result; resume checks before continuing.")
    except (ValueError, OSError, sqlite3.Error) as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
