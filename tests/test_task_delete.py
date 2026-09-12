from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.tools.task_store import TaskStore, TaskValidationError

"""CANONICAL DELETE TASK acceptance tests (PART I).

scratch only. No real user data mutation.

Test 1  — open Task delete
Test 2  — done Task delete
Test 3  — legacy ID delete
Test 4  — generated immutable ID delete
Test 5  — wrong Project
Test 6  — unknown Task
Test 7  — sibling preservation
Test 8  — restart (delete persists)
Test 9  — tree_snapshot (delete via bridge + snapshot)
Test 10 — list_current_tasks
Test 11 — bridge round-trip
Test 12 — already-deleted → unknown error
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


class Test01OpenTaskDelete(unittest.TestCase):
    def test_delete_open_task(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        a = store.create("graduation-thesis", "Task A", "a")
        store.create("graduation-thesis", "Task B", "b")
        result = store.delete_task("graduation-thesis", a["id"])
        self.assertTrue(result["deleted"])
        self.assertEqual(result["id"], a["id"])
        self.assertEqual(result["title"], "Task A")
        self.assertEqual(result["reason"], "a")
        self.assertFalse(result["done"])
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual([t["id"] for t in tasks], [t["id"] for t in tasks if t["id"] != a["id"]])
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Task B")


class Test02DoneTaskDelete(unittest.TestCase):
    def test_delete_done_task(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        t = store.create("graduation-thesis", "Done task", "d")
        store.set_done("graduation-thesis", t["id"], True)
        result = store.delete_task("graduation-thesis", t["id"])
        self.assertTrue(result["deleted"])
        self.assertTrue(result["done"])
        self.assertEqual(store.list_tasks("graduation-thesis"), [])


class Test03LegacyIdDelete(unittest.TestCase):
    def test_delete_legacy_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        # Seed a legacy content-derived ID directly — identity must be respected
        (store.task_file).write_text(
            "# Tasks\n\n## graduation-thesis\n\n"
            "- [ ] t-9419617c712e: Legacy task — seeded\n",
            encoding="utf-8",
        )
        result = store.delete_task("graduation-thesis", "t-9419617c712e")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["id"], "t-9419617c712e")
        self.assertEqual(result["title"], "Legacy task")
        self.assertEqual(store.list_tasks("graduation-thesis"), [])


class Test04GeneratedIdDelete(unittest.TestCase):
    def test_delete_generated_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        t = store.create("jarvis-app", "Generated", "g")
        self.assertTrue(t["id"].startswith("t-"))
        result = store.delete_task("jarvis-app", t["id"])
        self.assertTrue(result["deleted"])
        self.assertEqual(result["id"], t["id"])
        self.assertEqual(store.list_tasks("jarvis-app"), [])


class Test05WrongProject(unittest.TestCase):
    def test_delete_through_wrong_project_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        a = store.create("graduation-thesis", "In A", "a")
        with self.assertRaises(TaskValidationError):
            store.delete_task("jarvis-app", a["id"])
        # zero mutation — task still exists in original project
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual([t["id"] for t in tasks], [a["id"]])


class Test06UnknownTask(unittest.TestCase):
    def test_delete_unknown_task_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        store.create("graduation-thesis", "Exists", "e")
        with self.assertRaises(TaskValidationError):
            store.delete_task("graduation-thesis", "t-doesnotexist0")
        self.assertEqual(len(store.list_tasks("graduation-thesis")), 1)


class Test07SiblingPreservation(unittest.TestCase):
    def test_delete_middle_sibling(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        a = store.create("graduation-thesis", "A", "ra")
        b = store.create("graduation-thesis", "B", "rb")
        c = store.create("graduation-thesis", "C", "rc")
        store.set_done("graduation-thesis", b["id"], True)
        store.delete_task("graduation-thesis", b["id"])
        remaining = store.list_tasks("graduation-thesis")
        self.assertEqual([t["id"] for t in remaining], [a["id"], c["id"]])
        self.assertEqual(remaining[0]["title"], "A")
        self.assertEqual(remaining[0]["reason"], "ra")
        self.assertFalse(remaining[0]["done"])
        self.assertEqual(remaining[1]["title"], "C")
        self.assertEqual(remaining[1]["reason"], "rc")
        self.assertFalse(remaining[1]["done"])


class Test08RestartPersistence(unittest.TestCase):
    def test_delete_survives_reload(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        t = store.create("graduation-thesis", "Vanishing", "v")
        store.delete_task("graduation-thesis", t["id"])
        # fresh store instance = restart
        fresh = TaskStore(tmp / "mem" / "tasks.md", memory_dir=tmp / "mem")
        self.assertEqual(fresh.list_tasks("graduation-thesis"), [])
        with self.assertRaises(TaskValidationError):
            fresh.delete_task("graduation-thesis", t["id"])


class Test09TreeSnapshotAgreement(unittest.TestCase):
    def test_bridge_delete_then_snapshot(self) -> None:
        from scripts.harness_bridge import handle_message, BridgeSession
        from harness.client import HarnessClient
        from harness.config import HarnessConfig

        tmp = Path(tempfile.mkdtemp())
        mem = tmp / "mem"
        mem.mkdir()
        (mem / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
        (mem / "tasks.md").write_text("", encoding="utf-8")

        cfg = HarnessConfig.load()
        cfg.memory_dir = mem
        cfg.task_file = mem / "tasks.md"
        cfg.file_roots = None
        cfg.trace_dir = tmp / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)

        client = HarnessClient(cfg)
        session = BridgeSession()

        r1 = handle_message(client, session, {
            "type": "create_task", "id": 1,
            "project_id": "graduation-thesis",
            "title": "Snapshot victim",
            "reason": "snap",
        })
        self.assertEqual(r1["status"], "ok", r1)
        tid = r1["task"]["id"]

        snap_before = handle_message(client, session, {"type": "tree_snapshot", "id": 2})
        task_ids_before = [
            c["id"]
            for p in snap_before["tree"]
            for c in p.get("children", [])
        ]
        self.assertIn(tid, task_ids_before)

        r2 = handle_message(client, session, {
            "type": "delete_task", "id": 3,
            "project_id": "graduation-thesis",
            "task_id": tid,
        })
        self.assertEqual(r2["status"], "ok", r2)
        self.assertEqual(r2["task"]["id"], tid)
        self.assertTrue(r2["task"]["deleted"])

        snap_after = handle_message(client, session, {"type": "tree_snapshot", "id": 4})
        task_ids_after = [
            c["id"]
            for p in snap_after["tree"]
            for c in p.get("children", [])
        ]
        self.assertNotIn(tid, task_ids_after)


class Test10ListCurrentTasks(unittest.TestCase):
    def test_list_reflects_delete(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        a = store.create("graduation-thesis", "Keep", "k")
        b = store.create("graduation-thesis", "Drop", "d")
        store.delete_task("graduation-thesis", b["id"])
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual([t["id"] for t in tasks], [a["id"]])
        self.assertNotIn(b["id"], [t["id"] for t in tasks])


class Test11BridgeRoundTrip(unittest.TestCase):
    def test_delete_through_bridge(self) -> None:
        from scripts.harness_bridge import handle_message, BridgeSession
        from harness.client import HarnessClient
        from harness.config import HarnessConfig

        tmp = Path(tempfile.mkdtemp())
        mem = tmp / "mem"
        mem.mkdir()
        (mem / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
        (mem / "tasks.md").write_text("", encoding="utf-8")

        cfg = HarnessConfig.load()
        cfg.memory_dir = mem
        cfg.task_file = mem / "tasks.md"
        cfg.file_roots = None
        cfg.trace_dir = tmp / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)

        client = HarnessClient(cfg)
        session = BridgeSession()

        r1 = handle_message(client, session, {
            "type": "create_task", "id": 1,
            "project_id": "graduation-thesis",
            "title": "Bridge delete me",
            "reason": "bridge",
        })
        tid = r1["task"]["id"]

        r2 = handle_message(client, session, {
            "type": "delete_task", "id": 2,
            "project_id": "graduation-thesis",
            "task_id": tid,
        })
        self.assertEqual(r2["status"], "ok", r2)
        self.assertEqual(r2["task"]["id"], tid)
        self.assertTrue(r2["task"]["deleted"])
        self.assertEqual(r2["task"]["title"], "Bridge delete me")

        # deleted task not in file anymore
        tasks = TaskStore(mem / "tasks.md", memory_dir=mem).list_tasks("graduation-thesis")
        self.assertEqual(tasks, [])


class Test12AlreadyDeleted(unittest.TestCase):
    def test_second_delete_is_unknown_error(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        t = store.create("graduation-thesis", "Once", "o")
        store.delete_task("graduation-thesis", t["id"])
        with self.assertRaises(TaskValidationError) as ctx:
            store.delete_task("graduation-thesis", t["id"])
        self.assertIn("알 수 없는 task", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
