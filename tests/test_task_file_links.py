from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.tools.discovery import Discovery
from harness.tools.resource_links import ResourceLinkError
from harness.tools.task_store import TaskStore, TaskValidationError
from scripts.harness_bridge import BridgeSession, handle_message


def build_fixture(tmp: Path) -> dict[str, Path]:
    memory = tmp / "memory"
    memory.mkdir()
    (memory / "projects.md").write_text(
        "# Projects\n\n## Active\n\n- graduation-thesis: Graduation Thesis\n",
        encoding="utf-8",
    )
    (memory / "tasks.md").write_text(
        "# Tasks\n\n## graduation-thesis\n\n"
        "- [ ] t-task11111111: Run LPIPS evaluation — compare outputs\n",
        encoding="utf-8",
    )
    workspace = tmp / "workspace"
    (workspace / "papers").mkdir(parents=True)
    (workspace / "results").mkdir()
    (workspace / "papers" / "qian2018.pdf").write_bytes(b"paper")
    (workspace / "results" / "lpips.csv").write_text("score\n0.31\n", encoding="utf-8")
    return {"memory": memory, "workspace": workspace}


def make_discovery(fixture: dict[str, Path]) -> Discovery:
    return Discovery(fixture["memory"], file_roots={"research": str(fixture["workspace"])})


def file_id(discovery: Discovery, path: str) -> str:
    assert discovery.workspace is not None
    discovery.workspace.ensure_scanned()
    ref = discovery.workspace.registry.by_path("research", path)
    assert ref is not None
    return ref.id


class TaskFileLinkTests(unittest.TestCase):
    def test_reference_source_result_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_fixture(Path(raw))
            discovery = make_discovery(fixture)
            task_id = "t-task11111111"
            ids = [
                file_id(discovery, "papers/qian2018.pdf"),
                file_id(discovery, "results/lpips.csv"),
            ]
            first = discovery.resources.link_task_file(task_id, ids[0], "reference")
            second = discovery.resources.link_task_file(task_id, ids[1], "result")
            self.assertTrue(first["created"])
            self.assertTrue(second["created"])
            fresh = make_discovery(fixture)
            resources = fresh.resources.list_task_resources(task_id)["resources"]
            self.assertEqual({entry["file"]["id"] for entry in resources}, set(ids))
            self.assertEqual({entry["relation"] for entry in resources}, {"reference", "result"})

    def test_duplicate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_fixture(Path(raw))
            discovery = make_discovery(fixture)
            fid = file_id(discovery, "results/lpips.csv")
            first = discovery.resources.link_task_file("t-task11111111", fid, "result")
            second = discovery.resources.link_task_file("t-task11111111", fid, "result")
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["link"]["id"], second["link"]["id"])

    def test_unknown_identity_and_path_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_fixture(Path(raw))
            discovery = make_discovery(fixture)
            for task_id, fid in (("t-missing", "f-known"), ("Run LPIPS", "f-known"), ("t-task11111111", "results/lpips.csv")):
                with self.assertRaises(ResourceLinkError):
                    discovery.resources.link_task_file(task_id, fid)
            self.assertEqual(discovery.resources.registry.all_links(), [])

    def test_task_edit_keeps_links(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_fixture(Path(raw))
            discovery = make_discovery(fixture)
            fid = file_id(discovery, "results/lpips.csv")
            discovery.resources.link_task_file("t-task11111111", fid, "result")
            store = TaskStore(fixture["memory"] / "tasks.md", fixture["memory"])
            updated = store.update_task("graduation-thesis", "t-task11111111", title="LPIPS baseline evaluation")
            self.assertEqual(updated["id"], "t-task11111111")
            self.assertEqual(discovery.resources.list_task_resources("t-task11111111")["resources"][0]["file"]["id"], fid)

    def test_file_move_keeps_link_file_id_and_new_locator(self) -> None:
        """Same-root move must reconcile the existing FileRef, not create a second one."""
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_fixture(Path(raw))
            discovery = make_discovery(fixture)
            task_id = "t-task11111111"
            old_path = fixture["workspace"] / "a" / "result.csv"
            new_path = fixture["workspace"] / "b" / "result.csv"
            old_path.parent.mkdir()
            old_path.write_text("lpips result\n", encoding="utf-8")
            fid = file_id(discovery, "a/result.csv")
            created = discovery.resources.link_task_file(task_id, fid, "result")
            link_id = created["link"]["id"]

            new_path.parent.mkdir()
            old_path.rename(new_path)
            report = discovery.workspace.scan_root("research")

            self.assertEqual(report["created"], 0)
            self.assertEqual(report["renamed"], [{"id": fid, "from": "a/result.csv", "to": "b/result.csv"}])
            self.assertEqual(
                discovery.workspace.registry.by_path("research", "b/result.csv").id,
                fid,
            )
            self.assertIsNone(discovery.workspace.registry.by_path("research", "a/result.csv"))

            resources = discovery.resources.list_task_resources(task_id)["resources"]
            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0]["link_id"], link_id)
            self.assertEqual(resources[0]["file"]["id"], fid)
            self.assertEqual(resources[0]["file"]["relative_path"], "b/result.csv")
            self.assertEqual(len(discovery.resources.registry.all_links()), 1)
            self.assertEqual(
                len([ref for ref in discovery.workspace.registry.refs.values() if ref.root_id == "research" and not ref.missing]),
                3,
            )

    def test_file_rename_keeps_link_and_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_fixture(Path(raw))
            discovery = make_discovery(fixture)
            fid = file_id(discovery, "results/lpips.csv")
            discovery.resources.link_task_file("t-task11111111", fid, "result")
            (fixture["workspace"] / "results" / "lpips.csv").rename(fixture["workspace"] / "results" / "lpips-final.csv")
            listed = discovery.resources.list_task_resources("t-task11111111")["resources"]
            self.assertEqual(listed[0]["file"]["id"], fid)
            self.assertEqual(listed[0]["file"]["relative_path"], "results/lpips-final.csv")

    def test_delete_refuses_linked_task_then_unlink_allows_delete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_fixture(Path(raw))
            discovery = make_discovery(fixture)
            fid = file_id(discovery, "results/lpips.csv")
            created = discovery.resources.link_task_file("t-task11111111", fid, "result")
            store = TaskStore(fixture["memory"] / "tasks.md", fixture["memory"])
            with self.assertRaises(TaskValidationError):
                store.delete_task("graduation-thesis", "t-task11111111")
            self.assertIsNotNone(store.find_task("t-task11111111"))
            discovery.resources.unlink_task_file(created["link"]["id"])
            self.assertTrue(store.delete_task("graduation-thesis", "t-task11111111")["deleted"])
            self.assertIsNone(store.find_task("t-task11111111"))
            self.assertTrue((fixture["workspace"] / "results" / "lpips.csv").exists())

    def test_tree_snapshot_and_bridge_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_fixture(Path(raw))
            discovery = make_discovery(fixture)
            fid = file_id(discovery, "results/lpips.csv")
            cfg = HarnessConfig.load()
            cfg.memory_dir = fixture["memory"]
            cfg.task_file = fixture["memory"] / "tasks.md"
            cfg.file_roots = {"research": str(fixture["workspace"])}
            cfg.trace_dir = Path(raw) / "traces"
            cfg.trace_dir.mkdir()
            client = HarnessClient(cfg)
            session = BridgeSession()
            created = handle_message(client, session, {
                "type": "link_task_file",
                "id": 1,
                "task_id": "t-task11111111",
                "file_id": fid,
                "relation": "result",
            })
            self.assertEqual(created["status"], "ok")
            link = created["link"]
            tree = handle_message(client, session, {"type": "tree_snapshot", "id": 2})
            task = next(
                task for project in tree["tree"] for task in project["children"]
                if task.get("id") == "t-task11111111"
            )
            resource_files = [
                child for group in task["children"]
                if group["type"] == "task_resource_group"
                for child in group["children"]
            ]
            self.assertEqual(resource_files[0]["file"]["id"], fid)
            linked = handle_message(client, session, {
                "type": "list_task_resources", "id": 3, "task_id": "t-task11111111",
            })
            self.assertEqual(linked["status"], "ok")
            self.assertEqual(linked["resources"][0]["link_id"], link["id"])

    def test_search_provenance_is_task_specific(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = build_fixture(Path(raw))
            discovery = make_discovery(fixture)
            fid = file_id(discovery, "results/lpips.csv")
            discovery.resources.link_task_file("t-task11111111", fid, "result")
            result = discovery.search_context("lpips")
            files = [entry for entry in result["results"] if entry["type"] == "file"]
            self.assertTrue(files)
            self.assertEqual(files[0]["linked_tasks"][0]["task_id"], "t-task11111111")
            self.assertEqual(files[0]["linked_tasks"][0]["relation"], "result")


if __name__ == "__main__":
    unittest.main()
