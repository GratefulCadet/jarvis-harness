from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.config import HarnessConfig
from harness.tools.discovery import Discovery
from harness.tools.file_store import FileStoreError
from harness.tools.workspace import WorkspaceManager, default_registry_file
from scripts.harness_bridge import BridgeSession, handle_message


class WorkspaceEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.memory = self.tmp / "memory"
        self.memory.mkdir()
        (self.memory / "projects.md").write_text("# Projects\n\n## Active\n\n- scratch: Scratch\n", encoding="utf-8")
        self.root = self.tmp / "workspace"
        self.root.mkdir()
        self.file = self.root / "README.md"
        self.file.write_text("hello\n", encoding="utf-8")
        (self.root / "image.bin").write_bytes(b"\x00\x01")
        self.undecodable = self.root / "legacy.md"
        self.undecodable.write_bytes(b"legacy:\xff\xfe\n")
        self.discovery = Discovery(self.memory, file_roots={"scratch": str(self.root)})
        self.workspace = WorkspaceManager(self.discovery.files, default_registry_file(self.memory))
        self.workspace.scan_root("scratch")
        self.ref = self.workspace.file_identity("scratch", "README.md")

    def test_read_revision_and_same_identity_after_save(self) -> None:
        opened = self.workspace.read_text("scratch", "README.md")
        self.assertEqual(opened["id"], self.ref["id"])
        saved = self.workspace.update_text(                "scratch", opened["id"], "README.md", "updated\n", opened["revision"]

        )
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["id"], opened["id"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), "updated\n")
        self.assertEqual(self.workspace.read_text("scratch", "README.md")["id"], opened["id"])

    def test_external_change_is_rejected_without_overwrite(self) -> None:
        opened = self.workspace.read_text("scratch", "README.md")
        self.file.write_text("external\n", encoding="utf-8")
        with self.assertRaises(FileStoreError):
            self.workspace.update_text(
                "scratch", opened["id"], "README.md", "local edit\n", opened["revision"]
            )
        self.assertEqual(self.file.read_text(encoding="utf-8"), "external\n")

    def test_disconnected_root_and_oversize_are_rejected(self) -> None:
        opened = self.workspace.read_text("scratch", "README.md")
        self.file.unlink()
        with self.assertRaises(FileStoreError):
            self.workspace.update_text(
                "scratch", opened["id"], "README.md", "x", opened["revision"]
            )
        self.file.write_text("restored\n", encoding="utf-8")
        self.workspace.scan_root("scratch")
        opened = self.workspace.read_text("scratch", "README.md")
        with self.assertRaises(FileStoreError):
            self.workspace.update_text(
                "scratch", opened["id"], "README.md", "x" * (1024 * 1024 + 1), opened["revision"]
            )

    def test_undecodable_utf8_is_read_only_and_never_rewritten(self) -> None:
        with self.assertRaises(FileStoreError):
            self.workspace.read_text("scratch", "legacy.md")

        ref = self.workspace.file_identity("scratch", "legacy.md")
        before = self.undecodable.read_bytes()
        revision = {
            "size": len(before),
            "fingerprint": self.workspace.registry.by_id(ref["id"]).fingerprint,
        }
        with self.assertRaises(FileStoreError):
            self.workspace.update_text(
                "scratch", ref["id"], "legacy.md", "safe utf8\n", revision
            )
        self.assertEqual(self.undecodable.read_bytes(), before)

    def test_identity_path_and_type_are_authoritative(self) -> None:
        opened = self.workspace.read_text("scratch", "README.md")
        with self.assertRaises(FileStoreError):
            self.workspace.update_text("scratch", opened["id"], "../README.md", "x", opened["revision"])
        with self.assertRaises(FileStoreError):
            self.workspace.update_text("scratch", opened["id"], "image.bin", "x", opened["revision"])
        with self.assertRaises(FileStoreError):
            self.workspace.update_text("scratch", "f-forged", "README.md", "x", opened["revision"])

    def test_bridge_file_read_and_write(self) -> None:
        cfg = HarnessConfig.load()
        cfg.memory_dir = self.memory
        cfg.task_file = self.memory / "tasks.md"
        cfg.file_roots = {"scratch": str(self.root)}
        cfg.trace_dir = self.tmp / "traces"
        cfg.trace_dir.mkdir()
        from harness.client import HarnessClient
        client = HarnessClient(cfg)
        session = BridgeSession()
        opened = handle_message(client, session, {"type": "file_read", "id": 1, "root": "scratch", "path": "README.md"})
        self.assertEqual(opened["status"], "ok")
        rejected = handle_message(client, session, {"type": "file_read", "id": 3, "root": "scratch", "path": "legacy.md"})
        self.assertEqual(rejected["status"], "error")
        saved = handle_message(client, session, {
            "type": "file_write", "id": 2, "root_id": "scratch", "file_id": opened["file_id"],
            "path": "README.md", "content": "bridge edit\n", "revision": opened["revision"],
        })
        self.assertEqual(saved["status"], "ok", saved)
        self.assertEqual(self.file.read_text(encoding="utf-8"), "bridge edit\n")


if __name__ == "__main__":
    unittest.main()
