from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.tools.task_store import TaskStore, TaskValidationError, _new_task_id

"""TASK IDENTITY V1 acceptance tests (PART J).

scratch only. No real user data mutation.

Test 1  — New Task gets content-independent immutable ID
Test 2  — Restart preserves same ID
Test 3  — Duplicate create returns existing ID (created:false)
Test 4  — Same title, different reason → different ID
Test 5  — Complete/Reopen same ID
Test 6  — Legacy deterministic ID preserved
Test 7  — Mixed legacy + new store identically
Test 8  — Bridge create_task round-trip
Test 9  — list_current_tasks same ID
Test 10 — Repeated refresh no duplicates
Test 11 — Collision handling (generator retries safely)
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


class Test01NewTaskImmutableID(unittest.TestCase):
    def test_new_id_is_not_content_hash(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        result = store.create("graduation-thesis", "LPIPS evaluation", "compare outputs")
        self.assertTrue(result["created"])
        tid = result["id"]
        self.assertTrue(tid.startswith("t-"))
        self.assertEqual(len(tid), 14)  # t- + 12 hex chars
        # Verify it is NOT the old deterministic hash
        import hashlib
        old_hash = "t-" + hashlib.sha1(
            "graduation-thesis|LPIPS evaluation|compare outputs".encode()
        ).hexdigest()[:12]
        self.assertNotEqual(tid, old_hash, "New ID must not be content-derived hash")

    def test_two_creates_get_different_ids(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r1 = store.create("graduation-thesis", "Task A", "reason A")
        r2 = store.create("graduation-thesis", "Task B", "reason B")
        self.assertNotEqual(r1["id"], r2["id"])


class Test02RestartPreservesID(unittest.TestCase):
    def test_same_id_after_reload(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "LPIPS eval", "compare")
        tid = r["id"]
        store2 = TaskStore(tmp / "mem" / "tasks.md", memory_dir=tmp / "mem")
        tasks = store2.list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], tid)


class Test03DuplicateCreate(unittest.TestCase):
    def test_same_content_returns_existing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r1 = store.create("graduation-thesis", "LPIPS eval", "compare")
        r2 = store.create("graduation-thesis", "LPIPS eval", "compare")
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        self.assertEqual(r1["id"], r2["id"])
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 1)


class Test04SameTitleDifferentReason(unittest.TestCase):
    def test_different_reason_different_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r1 = store.create("graduation-thesis", "LPIPS eval", "reason A")
        r2 = store.create("graduation-thesis", "LPIPS eval", "reason B")
        self.assertNotEqual(r1["id"], r2["id"])
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 2)


class Test05CompleteReopenSameID(unittest.TestCase):
    def test_done_toggle_preserves_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "LPIPS eval", "compare")
        tid = r["id"]
        # Complete
        store.set_done("graduation-thesis", tid, True)
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(tasks[0]["id"], tid)
        self.assertTrue(tasks[0]["done"])
        # Reopen
        store.set_done("graduation-thesis", tid, False)
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(tasks[0]["id"], tid)
        self.assertFalse(tasks[0]["done"])


class Test06LegacyDeterministicID(unittest.TestCase):
    def test_legacy_id_preserved(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        mem = tmp / "mem"; mem.mkdir()
        (mem / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
        # Write legacy content-derived task
        (mem / "tasks.md").write_text(
            "# Tasks\n\n## graduation-thesis\n\n"
            "- [ ] t-9419617c712e: legacy task — old reason\n",
            encoding="utf-8",
        )
        store = TaskStore(mem / "tasks.md", memory_dir=mem)
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], "t-9419617c712e")
        # Toggle preserves ID
        store.set_done("graduation-thesis", "t-9419617c712e", True)
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(tasks[0]["id"], "t-9419617c712e")
        self.assertTrue(tasks[0]["done"])


class Test07MixedLegacyAndNew(unittest.TestCase):
    def test_parser_treats_both_identically(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        mem = tmp / "mem"; mem.mkdir()
        (mem / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
        # Legacy + new task
        (mem / "tasks.md").write_text(
            "# Tasks\n\n## graduation-thesis\n\n"
            "- [ ] t-9419617c712e: legacy task — old reason\n"
            "- [ ] t-abc123def456: new task — new reason\n",
            encoding="utf-8",
        )
        store = TaskStore(mem / "tasks.md", memory_dir=mem)
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 2)
        ids = {t["id"] for t in tasks}
        self.assertIn("t-9419617c712e", ids)
        self.assertIn("t-abc123def456", ids)
        # Both can be toggled
        store.set_done("graduation-thesis", "t-9419617c712e", True)
        store.set_done("graduation-thesis", "t-abc123def456", True)
        tasks = store.list_tasks("graduation-thesis")
        self.assertTrue(all(t["done"] for t in tasks))


class Test08BridgeRoundTrip(unittest.TestCase):
    def test_create_through_harness_client(self) -> None:
        from harness.client import HarnessClient
        from harness.config import HarnessConfig
        from harness.tools import build_default_registry

        tmp = Path(tempfile.mkdtemp())
        mem = tmp / "mem"; mem.mkdir()
        (mem / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
        (mem / "tasks.md").write_text("", encoding="utf-8")

        cfg = HarnessConfig.load()
        cfg.memory_dir = mem
        cfg.task_file = mem / "tasks.md"
        cfg.file_roots = None
        cfg.trace_dir = tmp / "traces"; cfg.trace_dir.mkdir(parents=True, exist_ok=True)

        registry = build_default_registry(
            memory_dir=cfg.memory_dir,
            task_file=cfg.task_file,
            file_roots=cfg.file_roots,
        )
        client = HarnessClient(cfg, tools=registry)

        result = client.execute_tool("create_task", {
            "project_id": "graduation-thesis",
            "title": "Bridge test task",
            "reason": "verify round-trip",
        }, approve_write=True)
        self.assertTrue(result.ok, f"error: {result.error}")
        tid = result.data["id"]
        self.assertTrue(tid.startswith("t-"))
        self.assertTrue(result.data["created"])

        # Verify in tasks.md
        tasks = TaskStore(mem / "tasks.md", memory_dir=mem).list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], tid)

        # Verify list_current_tasks returns same ID
        result2 = client.execute_tool("list_current_tasks", {"project_id": "graduation-thesis"})
        self.assertTrue(result2.ok)
        self.assertEqual(len(result2.data["tasks"]), 1)
        self.assertEqual(result2.data["tasks"][0]["id"], tid)


class Test09ListCurrentTasksSameID(unittest.TestCase):
    def test_list_returns_same_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        r = store.create("graduation-thesis", "Task X", "reason X")
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(tasks[0]["id"], r["id"])


class Test10RepeatedRefreshNoDuplicates(unittest.TestCase):
    def test_no_duplicates_after_repeated_create(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        for _ in range(5):
            store.create("graduation-thesis", "Same task", "Same reason")
        tasks = store.list_tasks("graduation-thesis")
        self.assertEqual(len(tasks), 1)


class Test11CollisionHandling(unittest.TestCase):
    def test_generator_retries_on_collision(self) -> None:
        """If generated ID collides with existing, create should not overwrite."""
        tmp = Path(tempfile.mkdtemp())
        store = _make_store(tmp)
        # Manually insert a task with a known ID
        (tmp / "mem" / "tasks.md").write_text(
            "# Tasks\n\n## graduation-thesis\n\n"
            "- [ ] t-aaaaaaaaaaaa: collision test — existing\n",
            encoding="utf-8",
        )
        # Mock _new_task_id to return collision first, then unique
        import harness.tools.task_store as ts
        call_count = [0]
        original_new_id = ts._new_task_id
        def mock_new_id():
            call_count[0] += 1
            if call_count[0] == 1:
                return "t-aaaaaaaaaaaa"  # collision
            return "t-bbbbbbbbbbbb"  # unique
        ts._new_task_id = mock_new_id
        try:
            result = store.create("graduation-thesis", "New task", "new reason")
            self.assertTrue(result["created"])
            self.assertEqual(result["id"], "t-bbbbbbbbbbbb")
            tasks = store.list_tasks("graduation-thesis")
            self.assertEqual(len(tasks), 2)
            ids = {t["id"] for t in tasks}
            self.assertIn("t-aaaaaaaaaaaa", ids)
            self.assertIn("t-bbbbbbbbbbbb", ids)
        finally:
            ts._new_task_id = original_new_id


if __name__ == "__main__":
    unittest.main()
