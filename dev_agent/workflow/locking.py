"""Prevent standalone stages from overlapping an active fix cycle."""

from contextvars import ContextVar
from pathlib import Path

revision_owner: ContextVar[str | None] = ContextVar("revision_owner", default=None)


def check_revision_lock(directory: Path) -> None:
    lock = directory / "revision.lock"
    if lock.is_symlink():
        raise ValueError("Refusing symlink revision lock.")
    if lock.exists() and (revision_owner.get() is None or lock.read_text() != revision_owner.get()):
        raise ValueError("A revision is running; wait before starting another stage.")


def check_fix_lock(directory: Path, token: str | None = None) -> None:
    check_revision_lock(directory)
    lock = directory / "fix-cycle.lock"
    if lock.is_symlink():
        raise ValueError("Refusing symlink fix-cycle lock.")
    if lock.exists() and (token is None or lock.read_text(encoding="utf-8") != token):
        raise ValueError("A fix cycle is running; wait before starting another stage.")
