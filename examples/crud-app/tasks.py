"""Small SQLite task CRUD application used as a dev-agent target repository."""

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


class TaskStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tasks "
                "(id INTEGER PRIMARY KEY, title TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)"
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def validate_title(title: str) -> str:
        title = title.strip()
        if not title:
            raise ValueError("Title must not be empty")
        return title

    def create(self, title: str) -> dict[str, Any]:
        title = self.validate_title(title)
        with self.connect() as connection:
            cursor = connection.execute("INSERT INTO tasks (title) VALUES (?)", (title,))
            task_id = cursor.lastrowid
        assert task_id is not None
        return self.get(task_id)

    def get(self, task_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"Task {task_id} not found")
        return dict(row)

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM tasks ORDER BY id")]

    def update(self, task_id: int, title: str | None, done: bool | None) -> dict[str, Any]:
        if title is None and done is None:
            raise ValueError("Provide --title or --done/--no-done")
        title = self.validate_title(title) if title is not None else None
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET title = COALESCE(?, title), "
                "done = COALESCE(?, done) WHERE id = ?",
                (title, done, task_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Task {task_id} not found")
        return self.get(task_id)

    def delete(self, task_id: int) -> None:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            if cursor.rowcount == 0:
                raise ValueError(f"Task {task_id} not found")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local task CRUD sandbox")
    parser.add_argument("--db", type=Path, default=Path("tasks.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("title")
    commands.add_parser("list")
    for name in ("get", "update", "delete"):
        command = commands.add_parser(name)
        command.add_argument("id", type=int)
        if name == "update":
            command.add_argument("--title")
            command.add_argument("--done", action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    try:
        store = TaskStore(args.db)
        result: dict[str, Any] | list[dict[str, Any]]
        match args.command:
            case "create":
                result = store.create(args.title)
            case "list":
                result = store.list()
            case "get":
                result = store.get(args.id)
            case "update":
                result = store.update(args.id, args.title, args.done)
            case "delete":
                store.delete(args.id)
                result = {"deleted": args.id}
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
