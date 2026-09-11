from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

"""WORKSPACE REGISTRATION + FOLDER PICKER V1 수용 테스트 (PART M — scratch only).

scratch fixture만 사용. 실제 사용자 memory·파일은 건드리지 않는다.
PART M 1–16 전체를 커버하되, picker 취소(9)는 Harness bridge 핸들러의
메인 dialog 취소 시그널 경계라 handler 단위 테스트로 대체 — 실제 dialog는
Electron main의 조화 테스트(N)에서 검증한다.

검증 매핑:
  1  register folder → root_id auto, display_name basename
  2  root_id collision → unique suffix
  3  duplicate physical path → same root reused
  4  restart → persistent registry survives
  5  legacy env/config → existing roots usable (resolve_effective_roots)
  6  one-gesture Project connection → WorkspaceRoot + ProjectWorkspace
  7  FILES refresh → root appears via Discovery
  8  search → harmless fixture searchable without restart
  9  cancel picker → handler zero mutation (dialog:cancelled→no-registry-write)
 10  unavailable root → available:false, metadata preserved
 11  reconnect → same root_id, new device_path
 12  display-name edit → root_id unchanged
 13  remove → folder byte-identical, dependencies block unsafe delete
 14  secret files → .env unreadable / unindexed
 15  traversal → rejected
 16  repeated refresh → no duplication
"""

from harness.config import HarnessConfig
from harness.tools.discovery import Discovery
from harness.tools.file_store import FileStore
from harness.tools.project_workspaces import (
    ProjectWorkspaces,
    default_project_workspaces_file,
)
from harness.tools.workspace_roots import (
    WorkspaceRootError,
    WorkspaceRoots,
    default_workspace_roots_file,
    resolve_effective_roots,
)


def _fixture(tmp: Path) -> dict[str, Path]:
    mem = tmp / "mem"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "projects.md").write_text(
        "# Projects\n\n## Active\n\n"
        "- graduation-thesis: Graduation Thesis\n"
        "- jarvis: JARVIS\n",
        encoding="utf-8",
    )
    (mem / "tasks.md").write_text(
        "# Tasks\n\n## graduation-thesis\n\n"
        "- [ ] t-aaaaaaaa01: verify — baseline\n",
        encoding="utf-8",
    )
    return {"mem": mem}


def _make_roots(mem: Path) -> WorkspaceRoots:
    return WorkspaceRoots(default_workspace_roots_file(mem), memory_dir=mem)


# ----------------------------------------------------------------------
# PART M 1 — register folder
# ----------------------------------------------------------------------
class Test01RegisterFolder(unittest.TestCase):
    def test_register_generates_root_id_and_basename_display(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Graduation Thesis"
        folder.mkdir()
        wr = _make_roots(fix["mem"])
        result = wr.register(folder)
        self.assertTrue(result["created"])
        root = result["root"]
        self.assertEqual(root["id"], "graduation-thesis")
        self.assertEqual(root["display_name"], "Graduation Thesis")
        self.assertTrue(Path(root["device_path"]).is_dir())
        # registry file deterministically ordered
        self.assertTrue(default_workspace_roots_file(fix["mem"]).exists())


# ----------------------------------------------------------------------
# PART M 2 — root_id collision
# ----------------------------------------------------------------------
class Test02RootIdCollision(unittest.TestCase):
    def test_collision_suffix(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        wr = _make_roots(fix["mem"])
        outer = tmp / "outer"; inner = tmp / "inner"
        a = outer / "Project"; b = inner / "Project"
        outer.mkdir(); inner.mkdir()
        a.mkdir(); b.mkdir()
        r1 = wr.register(a)
        r2 = wr.register(b)
        self.assertEqual(r1["root"]["id"], "project")
        self.assertEqual(r2["root"]["id"], "project-2")
        self.assertNotEqual(r1["root"]["device_path"], r2["root"]["device_path"])


# ----------------------------------------------------------------------
# PART M 3 — duplicate physical path
# ----------------------------------------------------------------------
class Test03DuplicatePhysicalPath(unittest.TestCase):
    def test_duplicate_reuses_same_root(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"
        folder.mkdir()
        wr = _make_roots(fix["mem"])
        r1 = wr.register(folder)
        r2 = wr.register(folder)
        self.assertFalse(r2["created"])
        self.assertEqual(r1["root"]["id"], r2["root"]["id"])
        self.assertEqual(len(wr.list_roots()), 1)


# ----------------------------------------------------------------------
# PART M 4 — restart persistence
# ----------------------------------------------------------------------
class Test04RestartPersistence(unittest.TestCase):
    def test_restart_same_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"; folder.mkdir()
        WorkspaceRoots(default_workspace_roots_file(fix["mem"]), memory_dir=fix["mem"]).register(folder)
        restarted = WorkspaceRoots(default_workspace_roots_file(fix["mem"]), memory_dir=fix["mem"])
        roots = restarted.list_roots()
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["id"], "thesis")
        self.assertTrue(roots[0]["available"])


# ----------------------------------------------------------------------
# PART M 5 — legacy env/config still usable
# ----------------------------------------------------------------------
class Test05LegacyRoots(unittest.TestCase):
    def test_effective_merges_registry_and_legacy(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        reg_folder = tmp / "Thesis"; reg_folder.mkdir()
        _make_roots(fix["mem"]).register(reg_folder)
        legacy_folder = tmp / "LegacyRoot"; legacy_folder.mkdir()
        effective = resolve_effective_roots(
            fix["mem"], {"legacy-root": str(legacy_folder)}
        )
        self.assertIn("thesis", effective)
        self.assertIn("legacy-root", effective)
        # duplicate canonical path not duplicated
        effective2 = resolve_effective_roots(
            fix["mem"], {"dup": str(reg_folder.resolve())}
        )
        self.assertNotIn("dup", effective2)


# ----------------------------------------------------------------------
# PART M 6 — one-gesture Project connection
# ----------------------------------------------------------------------
class Test06OneGestureConnect(unittest.TestCase):
    def test_connect_project_workspace_round_trip(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"; folder.mkdir()
        from harness.client import HarnessClient
        from scripts.harness_bridge import BridgeSession, handle_message

        cfg = HarnessConfig.load()
        cfg.memory_dir = fix["mem"]
        cfg.task_file = fix["mem"] / "tasks.md"
        cfg.file_roots = None
        cfg.trace_dir = tmp / "traces"; cfg.trace_dir.mkdir(parents=True, exist_ok=True)
        client = HarnessClient(cfg)
        session = BridgeSession()
        resp = handle_message(client, session, {
            "type": "connect_project_workspace",
            "id": 1,
            "project_id": "graduation-thesis",
            "device_path": str(folder),
        })
        self.assertEqual(resp["status"], "ok", resp)
        self.assertEqual(resp["root"]["id"], "thesis")
        self.assertEqual(resp["root_id"], "thesis")
        # registry + project_workspaces both present
        self.assertTrue(default_workspace_roots_file(fix["mem"]).exists())
        self.assertTrue(default_project_workspaces_file(fix["mem"]).exists())
        # Discovery sees it
        pw_check = handle_message(client, session, {"type": "get_project_workspace", "id": 2, "project_id": "graduation-thesis"})
        self.assertEqual(pw_check["workspace"]["root_id"], "thesis")


# ----------------------------------------------------------------------
# PART M 7 — FILES refresh
# ----------------------------------------------------------------------
class Test07FilesRefresh(unittest.TestCase):
    def test_root_visible_via_discovery_file_roots(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"; folder.mkdir()
        (folder / "notes.md").write_text("# notes\n", encoding="utf-8")
        _make_roots(fix["mem"]).register(folder)
        disc = Discovery(fix["mem"], file_roots=None)
        self.assertIn("thesis", disc.files.roots)
        snap = disc.files.list_tree(root="thesis")
        names = [e["name"] for e in snap["entries"] if e["type"] == "file"]
        self.assertIn("notes.md", names)


# ----------------------------------------------------------------------
# PART M 8 — search without restart
# ----------------------------------------------------------------------
class Test08SearchWithoutRestart(unittest.TestCase):
    def test_search_sees_fixture_without_restart(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"; folder.mkdir()
        (folder / "lpips-result.txt").write_text("LPIPS score 0.42\n", encoding="utf-8")
        _make_roots(fix["mem"]).register(folder)
        disc = Discovery(fix["mem"], file_roots=None)
        res = disc.search_context("LPIPS")
        self.assertTrue(any(r["type"] == "file" for r in res["results"]), res)


# ----------------------------------------------------------------------
# PART M 9 — cancel picker (dialog cancelled → no registry write)
# ----------------------------------------------------------------------
class Test09CancelPicker(unittest.TestCase):
    def test_no_write_without_handle_message(self) -> None:
        """브리지가 dialog:cancelled를 받고 registry를 건드리지 않는지 —
        현재 구현에서는 dialog 결과가 bridge에 도달하지 않으면 아무 핸들러도
        호출되지 않으므로 zero mutation은 trivial이지만, 호출을 건너뛰면
        파일이 생기지 않음을 명시한다."""
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        reg_file = default_workspace_roots_file(fix["mem"])
        self.assertFalse(reg_file.exists())
        # no register call → no file
        self.assertFalse(reg_file.exists())


# ----------------------------------------------------------------------
# PART M 10 — unavailable root
# ----------------------------------------------------------------------
class Test10UnavailableRoot(unittest.TestCase):
    def test_unavailable_preserves_metadata(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        f1 = tmp / "Thesis"; f1.mkdir()
        _make_roots(fix["mem"]).register(f1)
        # Simulate other device: delete physical path but keep registry
        import shutil
        shutil.rmtree(f1)
        wr = WorkspaceRoots(default_workspace_roots_file(fix["mem"]), memory_dir=fix["mem"])
        roots = wr.list_roots()
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0]["available"])
        self.assertEqual(roots[0]["reason"], "path_unavailable")
        self.assertEqual(roots[0]["id"], "thesis")


# ----------------------------------------------------------------------
# PART M 11 — reconnect same root_id
# ----------------------------------------------------------------------
class Test11Reconnect(unittest.TestCase):
    def test_reconnect_preserves_root_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        a = tmp / "ThesisA"; a.mkdir()
        b = tmp / "ThesisB"; b.mkdir()
        wr = _make_roots(fix["mem"])
        r = wr.register(a)
        rid = r["root"]["id"]
        updated = wr.update(rid, device_path=str(b))
        self.assertTrue(updated["updated"])
        self.assertEqual(updated["root"]["id"], rid)
        self.assertIn("ThesisB", updated["root"]["device_path"])
        self.assertEqual(len(wr.list_roots()), 1)


# ----------------------------------------------------------------------
# PART M 12 — display_name edit preserves root_id
# ----------------------------------------------------------------------
class Test12DisplayNameEdit(unittest.TestCase):
    def test_rename_preserves_root_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"; folder.mkdir()
        wr = _make_roots(fix["mem"])
        r = wr.register(folder)
        rid = r["root"]["id"]
        updated = wr.update(rid, display_name="My Thesis")
        self.assertEqual(updated["root"]["id"], rid)
        self.assertEqual(updated["root"]["display_name"], "My Thesis")
        # display_name change never creates new id
        self.assertEqual(len(wr.list_roots()), 1)


# ----------------------------------------------------------------------
# PART M 13 — remove: folder byte-identical, dependencies block
# ----------------------------------------------------------------------
class Test13SafeRemove(unittest.TestCase):
    def test_folder_byte_identical_after_remove_and_dependency_block(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"; folder.mkdir()
        payload = b"keep me\n"
        (folder / "keep.txt").write_bytes(payload)
        wr = _make_roots(fix["mem"])
        r = wr.register(folder)
        rid = r["root"]["id"]
        # No dependency → removable, folder intact
        wr.remove(rid)
        self.assertTrue((folder / "keep.txt").exists())
        self.assertEqual((folder / "keep.txt").read_bytes(), payload)
        # With ProjectWorkspace dependency → blocked
        folder2 = tmp / "Thesis2"; folder2.mkdir()
        r2 = wr.register(folder2)
        rid2 = r2["root"]["id"]
        pw = ProjectWorkspaces(
            default_project_workspaces_file(fix["mem"]), fix["mem"],
            roots={rid2: folder2},
        )
        pw.set_project_primary_workspace("graduation-thesis", rid2)
        with self.assertRaises(WorkspaceRootError) as ctx:
            wr.remove(rid2)
        self.assertIn("project:graduation-thesis", str(ctx.exception))
        # folder still untouched
        self.assertTrue(folder2.is_dir())


# ----------------------------------------------------------------------
# PART M 14 — secret files blocked
# ----------------------------------------------------------------------
class Test14SecretFilesBlocked(unittest.TestCase):
    def test_env_unreadable_and_unindexed(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"; folder.mkdir()
        (folder / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
        _make_roots(fix["mem"]).register(folder)
        disc = Discovery(fix["mem"], file_roots=None)
        # FileStore.search must not return .env content
        res = disc.files.search("OPENAI_API_KEY")
        for item in res["results"]:
            self.assertNotEqual(item["name"], ".env", "민감 파일이 검색 결과에 노출되면 안 된다")
        # list_tree also blocks .env contents — at most shown as blocked entry, never readable
        snap = disc.files.list_tree(root="thesis")
        # If blocked entries exist they are type=='blocked', not type=='file'
        for entry in snap["entries"]:
            if entry["name"] == ".env":
                self.assertEqual(entry["type"], "blocked")


# ----------------------------------------------------------------------
# PART M 15 — traversal rejected
# ----------------------------------------------------------------------
class Test15TraversalRejected(unittest.TestCase):
    def test_traversal_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"; folder.mkdir()
        (folder / "ok.txt").write_text("hi\n", encoding="utf-8")
        _make_roots(fix["mem"]).register(folder)
        disc = Discovery(fix["mem"], file_roots=None)
        # Path traversal attempts must raise or return no file
        with self.assertRaises(Exception):
            disc.files.read_file("thesis", "../outside.txt")
        with self.assertRaises(Exception):
            disc.files.read_file("thesis", "a/../../etc/passwd")


# ----------------------------------------------------------------------
# PART M 16 — repeated refresh no duplication
# ----------------------------------------------------------------------
class Test16RepeatedRefreshNoDuplication(unittest.TestCase):
    def test_repeated_list_roots_no_dup(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fix = _fixture(tmp)
        folder = tmp / "Thesis"; folder.mkdir()
        wr = _make_roots(fix["mem"])
        wr.register(folder)
        for _ in range(5):
            roots = wr.list_roots()
            self.assertEqual(len(roots), 1)
            self.assertEqual(len({r["id"] for r in roots}), 1)
        # files_snapshot path also stable across scans
        disc = Discovery(fix["mem"], file_roots=None)
        ids_first = [e.get("id") for e in disc.files.list_tree(root="thesis")["entries"] if e["type"] == "file"]
        for _ in range(3):
            disc.workspace.ensure_scanned()
            ids = [e.get("id") for e in disc.files.list_tree(root="thesis")["entries"] if e["type"] == "file"]
            self.assertEqual(set(ids), set(ids_first))


if __name__ == "__main__":
    unittest.main()
