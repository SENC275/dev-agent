"""SQLite run journal; artifacts remain on disk and locks cover whole advances."""

import fcntl
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dev_agent.local import protect_local_files


def now() -> str:
    return datetime.now(UTC).isoformat()


class Journal:
    def __init__(self, repository: Path):
        self.repository = repository.resolve(strict=True)
        protect_local_files(self.repository)
        directory = self.repository / ".dev-agent"
        if directory.is_symlink():
            raise ValueError("Refusing symlink state directory.")
        directory.mkdir(exist_ok=True)
        self.path = directory / "state.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Refusing symlink database.")
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported journal schema version.")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE,
                    repository TEXT NOT NULL, state TEXT NOT NULL,
                    current_step TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    ticket TEXT NOT NULL, config TEXT NOT NULL,
                    investigation_path TEXT, plan_path TEXT, worktree_path TEXT, branch TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL,
                    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                    artifact_path TEXT, error TEXT
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, ticket_id: str, ticket: str, config: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", ticket_id) or not ticket.strip():
            raise ValueError("Provide a nonempty ticket and a valid ticket ID.")
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO runs(run_id,ticket_id,repository,state,created_at,updated_at,"
                    "ticket,config) VALUES(?,?,?,'NEW',?,?,?,?)",
                    (uuid4().hex, ticket_id, str(self.repository), now(), now(), ticket, config),
                )
        except sqlite3.IntegrityError:
            raise ValueError("Ticket already exists; use resume or a new ticket ID.") from None

    def get(self, ticket_id: str) -> dict[str, str | None]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE ticket_id=?", (ticket_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown managed ticket: {ticket_id}")
        return dict(row)

    def update(self, ticket_id: str, **values: str | None) -> None:
        allowed = {
            "state",
            "current_step",
            "investigation_path",
            "plan_path",
            "worktree_path",
            "branch",
            "error",
        }
        if not values.keys() <= allowed:
            raise ValueError("Invalid journal fields.")
        values["updated_at"] = now()
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET " + ",".join(f"{key}=?" for key in values) + " WHERE ticket_id=?",
                (*values.values(), ticket_id),
            )

    def steps(self, ticket_id: str) -> list[dict[str, str | None]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT steps.* FROM steps JOIN runs USING(run_id) "
                    "WHERE ticket_id=? ORDER BY id",
                    (ticket_id,),
                )
            ]

    def begin(self, ticket_id: str, name: str) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO steps(run_id,name,status,started_at) "
                "SELECT run_id,?,'RUNNING',? FROM runs WHERE ticket_id=?",
                (name, now(), ticket_id),
            )
            db.execute(
                "UPDATE runs SET state='RUNNING',current_step=?,updated_at=?,error=NULL "
                "WHERE ticket_id=?",
                (name, now(), ticket_id),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def finish(
        self, step: int, status: str, artifact: Path | None = None, error: str | None = None
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE steps SET status=?,finished_at=?,artifact_path=?,error=? WHERE id=?",
                (status, now(), str(artifact) if artifact else None, error, step),
            )

    @contextmanager
    def lock(self, ticket_id: str) -> Iterator[None]:
        run = self.get(ticket_id)
        path = self.path.parent / f"run-{run['run_id']}.lock"
        if path.is_symlink():
            raise ValueError("Refusing symlink run lock.")
        with path.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("This ticket is already running in another process.") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def formatted(self, ticket_id: str) -> str:
        run = self.get(ticket_id)
        # Ticket body and provider configuration are not status output.
        return json.dumps(
            {key: value for key, value in run.items() if key not in {"ticket", "config"}}, indent=2
        )
