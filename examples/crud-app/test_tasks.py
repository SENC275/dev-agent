import tempfile
import unittest
from pathlib import Path

from tasks import TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)

    def test_crud_and_persistence(self) -> None:
        task = self.store.create("  First task  ")
        self.assertEqual(task["title"], "First task")
        self.assertEqual(TaskStore(self.path).get(task["id"]), task)
        self.assertEqual(self.store.list(), [task])
        changed = self.store.update(task["id"], "Updated", True)
        self.assertEqual(changed["title"], "Updated")
        self.assertEqual(changed["done"], 1)
        self.store.delete(task["id"])
        self.assertEqual(self.store.list(), [])

    def test_missing_records(self) -> None:
        for action in (
            lambda: self.store.get(99),
            lambda: self.store.delete(99),
            lambda: self.store.update(99, "New", None),
        ):
            with self.assertRaisesRegex(ValueError, "not found"):
                action()

    def test_blank_title(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            self.store.create(" ")
        task = self.store.create("Keep")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.store.update(task["id"], "  ", None)
        self.assertEqual(self.store.get(task["id"])["title"], "Keep")

    def test_partial_update(self) -> None:
        task = self.store.create("Keep")
        self.store.update(task["id"], None, True)
        task = self.store.update(task["id"], "Renamed", None)
        self.assertEqual(task["done"], 1)
        self.assertEqual(self.store.update(task["id"], None, False)["done"], 0)
        with self.assertRaises(ValueError):
            self.store.update(task["id"], None, None)

    def test_title_is_data(self) -> None:
        title = "'); DROP TABLE tasks; --"
        task = self.store.create(title)
        self.assertEqual(self.store.get(task["id"])["title"], title)
        self.assertEqual(len(self.store.list()), 1)


if __name__ == "__main__":
    unittest.main()
