"""Interactive human gate, separate from provider execution."""

import asyncio
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import typer
from rich.console import Console
from rich.markdown import Markdown

from dev_agent.config import CONFIG_NAME, ConfigurationError, load_config
from dev_agent.progress import activity
from dev_agent.providers.registry import ProviderRegistry
from dev_agent.workflow.plan_revision import request_changes
from dev_agent.workflow.planning import (
    approve_plan,
    checked_plan,
    edit_plan,
    investigation_repository,
)


def review_plan(
    directory: Path, console: Console, registry: ProviderRegistry | None = None
) -> bool:
    text = asyncio.run(checked_plan(directory))
    console.print(Markdown(text))
    if not sys.stdin.isatty():
        console.print(
            "Plan saved; approval requires an interactive terminal. Run review-plan later."
        )
        return False
    while True:
        choice = (
            typer.prompt("[A] Approve  [E] Edit  [R] Request changes  [Q] Quit", default="Q")
            .strip()
            .lower()
        )
        if choice == "q":
            return False
        if choice == "a":
            asyncio.run(approve_plan(directory, text))
            console.print("Plan approved.")
            return True
        if choice == "r":
            feedback = typer.prompt(
                "Changes for the planner (blank cancels)", default="", show_default=False
            )
            if not feedback.strip():
                continue
            try:
                repository = investigation_repository(directory.resolve().parent.parent)
                current_registry = registry or ProviderRegistry(
                    load_config(repository / CONFIG_NAME)
                )
                with activity(console, "Planner revising the plan…"):
                    attempt = asyncio.run(
                        request_changes(directory, feedback, current_registry, text)
                    )
                console.print(f"Revision saved: {attempt}", markup=False)
            except (ValueError, OSError, ConfigurationError, sqlite3.Error) as exc:
                console.print(f"Plan revision stopped: {exc}", markup=False)
                console.print("Inspect human-plan-revisions/ for details. No approval granted.")
            text = asyncio.run(checked_plan(directory))
            console.print(Markdown(text))
            console.print("Review the plan again. Only A approves implementation.")
            continue
        if choice == "e":
            editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
            with TemporaryDirectory(prefix="dev-agent-plan-edit-") as tmp:
                edited = Path(tmp) / "plan.md"
                edited.write_text(text, encoding="utf-8")
                result = subprocess.run([*editor, str(edited)], check=False)
                if result.returncode != 0:
                    console.print("Editor failed; saved plan unchanged.")
                    continue
                replacement = edited.read_text(encoding="utf-8")
                try:
                    asyncio.run(edit_plan(directory, replacement, text))
                except ValueError as exc:
                    console.print(str(exc), markup=False)
                    continue
            text = asyncio.run(checked_plan(directory))
            console.print(Markdown(text))
        elif choice != "a":
            console.print("Choose A, E, R or Q.")
