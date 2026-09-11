from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.tools.task_store import TaskStore, TaskValidationError

"""CANONICAL EDIT TASK acceptance tests (PART J).

scratch only. No real user data mutation.

Test 1   — title edit
Test 2   — reason edit
Test 3   — title + reason
Test 4   — done Task edit
Test 5   — legacy Task edit
Test 6   — duplicate target rejection
Test 7   — idempotent edit
Test 8   — unknown Task
Test 9   — wrong Project
Test 10  — validation
Test 11  — bridge round-trip
Test 12  — list_current_tasks
Test 13  — repeated refresh
"""

PROJECTS_MD = """# Projects

## Active

- graduation-thesis: 졸업논문 프로젝트
- jarvis-app: JARVIS Electron
"""


def _make_store(tmp: Path) -> TaskStore:
    mem = tmp / "mem"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
    return TaskStore(mem / "tasks.md", memory_dir=mem)


class Test01TitleEdit(unittest.TestCase):
    def test_edit_title_same_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "LPIPS experiment", "compare outputs")
        tid = r["id"]
        result = store.update_task("graduation-thesis", tid, title="LPIPS baseline evaluation")
        self.assertTrue(result["updated"])
        self.assertEqual(result["id"], tid)
        self.assertEqual(result["title"], "LPIPS baseline evaluation")
        self.assertEqual(result["reason"], "compare outputs")
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(tasks[0]["id"], tid)
        self.assertEqual(tasks[0]["title"], "LPIPS baseline evaluation")


class Test02ReasonEdit(unittest.TestCase):
    def test_edit_reason_same_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Task", "old reason")
        tid = r["id"]
        result = store.update_task("graduation-thesis", tid, reason="new reason")
        self.assertTrue(result["updated"])
        self.assertEqual(result["id"], tid)
        self.assertEqual(result["title"], "Task")
        self.assertEqual(result["reason"], "new reason")


class Test03TitleAndReasonEdit(unittest.TestCase):
    def test_edit_both_same_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Old title", "old reason")
        tid = r["id"]
        result = store.update_task("graduation-thesis", tid, title="New title", reason="new reason")
        self.assertTrue(result["updated"])
        self.assertEqual(result["id"], tid)
        self.assertEqual(result["title"], "New title")
        self.assertEqual(result["reason"], "new reason")


class Test04DoneTaskEdit(unittest.TestCase):
    def test_edit_done_task_preserves_done(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Task", "reason")
        tid = r["id"]
        store.set_done("graduation-thesis", tid, True)
        result = store.update_task("graduation-thesis", tid, title="Updated title")
        self.assertTrue(result["updated"])
        self.assertEqual(result["id"], tid)
        self.assertTrue(result["done"])


class Test05LegacyTaskEdit(unittest.TestCase):
    def test_edit_legacy_id_preserved(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        mem = tmp / "mem"; mem.mkdir()
        (mem / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
        (mem / "tasks.md").write_text(
            "# Tasks\n\n## graduation-thesis\n\n"
            "- [ ] t-9419617c712e: legacy task — old reason\n",
            encoding="utf-8",
        )
        store = TaskStore(mem / "tasks.md", memory_dir=mem)
        result = store.update_task("graduation-thesis", "t-9419617c712e", title="updated legacy")
        self.assertTrue(result["updated"])
        self.assertEqual(result["id"], "t-9419617c712e")
        self.assertEqual(result["title"], "updated legacy")
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(tasks[0]["id"], "t-9419617c712e")


class Test06DuplicateRejection(unittest.TestCase):
    def test_edit_to_existing_title_reason_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r1 = store.create("graduation-thesis", "Task A", "reason A")
        r2 = store.create("graduation-thesis", "Task B", "reason B")
        with self.assertRaises(TaskValidationError) as ctx:
            store.update_task("graduation-thesis", r2["id"], title="Task A", reason="reason A")
        self.assertIn("이미 존재", str(ctx.exception))
        # Verify zero mutation
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["title"], "Task A")
        self.assertEqual(tasks[1]["title"], "Task B")


class Test07IdempotentEdit(unittest.TestCase):
    def test_edit_same_values_no_write(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Task", "reason")
        result = store.update_task("graduation-thesis", r["id"], title="Task", reason="reason")
        self.assertFalse(result["updated"])
        self.assertEqual(result["id"], r["id"])


class Test08UnknownTask(unittest.TestCase):
    def test_edit_unknown_task_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        store.create("graduation-thesis", "Task", "reason")
        with self.assertRaises(TaskValidationError) as ctx:
            store.update_task("graduation-thesis", "t-nonexistent", title="New")
        self.assertIn("t-nonexistent", str(ctx.exception))


class Test09WrongProject(unittest.TestCase):
    def test_edit_through_wrong_project_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Task", "reason")
        with self.assertRaises(TaskValidationError):
            store.update_task("jarvis-app", r["id"], title="Hijacked")
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(tasks[0]["title"], "Task")


class Test10Validation(unittest.TestCase):
    def test_empty_title_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Task", "reason")
        with self.assertRaises(TaskValidationError):
            store.update_task("graduation-thesis", r["id"], title="   ")

    def test_overlong_title_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Task", "reason")
        with self.assertRaises(TaskValidationError):
            store.update_task("graduation-thesis", r["id"], title="x" * 300)

    def test_newline_in_title_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Task", "reason")
        with self.assertRaises(TaskValidationError):
            store.update_task("graduation-thesis", r["id"], title="line1\nline2")


class Test11BridgeRoundTrip(unittest.TestCase):
    def test_edit_through_bridge(self) -> None:
        from scripts.harness_bridge import handle_message, BridgeSession
        from harness.client import HarnessClient
        from harness.config import HarnessConfig

        tmp = Path(tempfile.mkdtemp())
        mem = tmp / "mem"; mem.mkdir()
        (mem / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
        (mem / "tasks.md").write_text("", encoding="utf-8")

        cfg = HarnessConfig.load()
        cfg.memory_dir = mem
        cfg.task_file = mem / "tasks.md"
        cfg.file_roots = None
        cfg.trace_dir = tmp / "traces"; cfg.trace_dir.mkdir(parents=True, exist_ok=True)

        client = HarnessClient(cfg)
        session = BridgeSession()

        # Create through bridge
        r1 = handle_message(client, session, {
            "type": "create_task", "id": 1,
            "project_id": "graduation-thesis",
            "title": "Original",
            "reason": "original reason",
        })
        self.assertEqual(r1["status"], "ok")
        tid = r1["task"]["id"]

        # Edit title through bridge
        r2 = handle_message(client, session, {
            "type": "update_task", "id": 2,
            "project_id": "graduation-thesis",
            "task_id": tid,
            "title": "Edited title",
        })
        self.assertEqual(r2["status"], "ok", r2)
        self.assertEqual(r2["task"]["id"], tid)
        self.assertEqual(r2["task"]["title"], "Edited title")
        self.assertEqual(r2["task"]["reason"], "original reason")

        # Verify in file
        tasks = TaskStore(mem / "tasks.md", memory_dir=mem).list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], tid)
        self.assertEqual(tasks[0]["title"], "Edited title")


class Test12ListCurrentTasks(unittest.TestCase):
    def test_list_shows_updated_title(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Old title", "reason")
        store.update_task("graduation-thesis", r["id"], title="New title")
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], r["id"])
        self.assertEqual(tasks[0]["title"], "New title")


class Test13RepeatedRefresh(unittest.TestCase):
    def test_same_id_updated_content_no_dup(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Task", "reason")
        tid = r["id"]
        for i in range(5):
            store.update_task("graduation-thesis", tid, title=f"Updated {i}")
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], tid)
        self.assertEqual(tasks[0]["title"], "Updated 4")


if __name__ == "__main__":
    unittest.main()
