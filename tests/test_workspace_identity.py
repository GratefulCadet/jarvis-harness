from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

"""Workspace Identity V1 수용 테스트 (PART I — scratch only).

검증 매핑:
- Test 1  initial identity  : 스캔 → 모든 파일 f-* 1개씩
- Test 2  restart           : 재생성해도 같은 f-*, 중복 없음
- Test 3  rename            : 같은 f-* + 새 relative_path
- Test 4  move              : 같은 f-* (확신 가능할 때)
- Test 5  ambiguous move    : 조용한 바인딩 금지 + unresolved 보고
- Test 6  modification      : 같은 f-* + 검색/읽기에 새 canonical 내용
- Test 7  deletion          : files_snapshot에 부활 없음, metadata 보존
- Test 8  sensitive         : .env/credentials 미인덱싱·미노출
- Test 9  containment       : .. / 루트 밖 하드 거부
- Test 10 SYSTEM MAP        : files_snapshot identity + 이동 후 노드 이동
- Test 11 search identity   : 이동 전후 같은 f-* + 현재 locator
- Test 12 bridge            : 실제 bridge handle_message 경로

파일시스템은 canonical — 레지스트리(<memory_dir>/file_refs.json)는 파생 상태.
"""

from harness.config import HarnessConfig
from harness.tools.discovery import Discovery
from harness.tools.file_store import FileStore, FileStoreError
from harness.tools.workspace import (
    FileRefRegistry,
    WorkspaceManager,
    default_registry_file,
    new_file_ref_id,
)


def _build_workspace(tmp: Path) -> Path:
    """PART I fixture: papers/experiments/music 구조."""
    ws = tmp / "workspace"
    (ws / "papers").mkdir(parents=True)
    (ws / "experiments").mkdir()
    (ws / "music").mkdir()
    (ws / "papers" / "qian2018.pdf").write_bytes(b"%PDF-1.4 qian perceptual loss paper")
    (ws / "experiments" / "lpips-notes.md").write_text(
        "LPIPS notes v1: perceptual metric\n", encoding="utf-8"
    )
    (ws / "music" / "berg.md").write_text("berg post-tonal notes\n", encoding="utf-8")
    return ws


def _make_manager(ws: Path, mem: Path) -> WorkspaceManager:
    return WorkspaceManager(
        FileStore({"workspace": str(ws)}), default_registry_file(mem)
    )


class WorkspaceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.mem = tmp / "mem"
        self.mem.mkdir()
        self.ws = _build_workspace(tmp)
        self.manager = _make_manager(self.ws, self.mem)


class Test1InitialIdentity(WorkspaceTestBase):
    def test_every_file_gets_one_stable_id(self) -> None:
        report = self.manager.scan_root("workspace")
        self.assertEqual(report["scanned_files"], 3)
        self.assertEqual(report["created"], 3)
        ids = {
            ref.id for ref in self.manager.registry.refs.values()
            if ref.root_id == "workspace"
        }
        self.assertEqual(len(ids), 3)
        self.assertTrue(all(ref_id.startswith("f-") for ref_id in ids))
        qian = self.manager.registry.by_path("workspace", "papers/qian2018.pdf")
        self.assertTrue(qian.id.startswith("f-"))
        # identity는 경로/이름/내용에서 파생되지 않는다 — 랜덤
        self.assertNotIn("qian2018", qian.id)

    def test_ref_id_not_reproducible_from_path(self) -> None:
        self.manager.scan_root("workspace")
        ref = self.manager.registry.by_path("workspace", "music/berg.md")
        # 같은 경로를 다른 레지스트리에서 다시 스캔해도 같은 파일은 같은 id를
        # 유지하지만, id 자체는 경로 해시가 아니다 (f- + random uuid)
        import hashlib

        self.assertNotEqual(
            ref.id, f"f-{hashlib.sha1(b'music/berg.md').hexdigest()[:12]}"
        )


class Test2Restart(WorkspaceTestBase):
    def test_identity_survives_registry_recreation(self) -> None:
        first = self.manager.scan_root("workspace")
        qian = self.manager.registry.by_path("workspace", "papers/qian2018.pdf")
        ref_id = qian.id

        # 완전히 새 인스턴스(=앱 재시작 시뮬레이션) — 같은 레지스트리 파일
        second = _make_manager(self.ws, self.mem)
        self.assertEqual(
            second.registry.registry_file, self.manager.registry.registry_file
        )
        report = second.scan_root("workspace")
        self.assertEqual(report["created"], 0)  # 중복 생성 없음
        again = second.registry.by_path("workspace", "papers/qian2018.pdf")
        self.assertEqual(again.id, ref_id)
        self.assertEqual(first["total_refs"], report["total_refs"])

    def test_registry_is_valid_json_with_version(self) -> None:
        self.manager.scan_root("workspace")
        raw = json.loads(default_registry_file(self.mem).read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        self.assertEqual(len(raw["refs"]), 3)


class Test3Rename(WorkspaceTestBase):
    def test_rename_preserves_identity(self) -> None:
        self.manager.scan_root("workspace")
        ref_id = self.manager.registry.by_path(
            "workspace", "papers/qian2018.pdf"
        ).id

        os.rename(self.ws / "papers" / "qian2018.pdf", self.ws / "papers" / "qian-2018-cvpr.pdf")
        report = self.manager.scan_root("workspace")

        self.assertEqual(len(report["renamed"]), 1)
        self.assertEqual(report["renamed"][0]["id"], ref_id)
        self.assertEqual(report["renamed"][0]["from"], "papers/qian2018.pdf")
        self.assertEqual(report["renamed"][0]["to"], "papers/qian-2018-cvpr.pdf")
        moved = self.manager.registry.by_id(ref_id)
        self.assertEqual(moved.relative_path, "papers/qian-2018-cvpr.pdf")
        # 이전 경로는 어떤 ref도 점유하지 않는다 (중복 엔티티 없음)
        self.assertIsNone(
            self.manager.registry.by_path("workspace", "papers/qian2018.pdf")
        )
        self.assertEqual(report["created"], 0)


class Test4Move(WorkspaceTestBase):
    def test_move_preserves_identity(self) -> None:
        self.manager.scan_root("workspace")
        ref_id = self.manager.registry.by_path(
            "workspace", "papers/qian2018.pdf"
        ).id

        os.rename(
            self.ws / "papers" / "qian2018.pdf",
            self.ws / "references" / "qian-2018-cvpr.pdf",
        ) if False else None
        (self.ws / "references").mkdir()
        os.rename(
            self.ws / "papers" / "qian2018.pdf",
            self.ws / "references" / "qian-2018-cvpr.pdf",
        )
        report = self.manager.scan_root("workspace")

        moved = self.manager.registry.by_id(ref_id)
        self.assertIsNotNone(moved)
        self.assertEqual(moved.relative_path, "references/qian-2018-cvpr.pdf")
        self.assertEqual(report["created"], 0)


class Test5AmbiguousMove(WorkspaceTestBase):
    def test_ambiguous_move_not_silently_bound(self) -> None:
        self.manager.scan_root("workspace")
        (self.ws / "tmp").mkdir()
        (self.ws / "tmp" / "orig.txt").write_bytes(b"A" * 100)
        self.manager.scan_root("workspace")
        orig = self.manager.registry.by_path("workspace", "tmp/orig.txt")
        orig_id = orig.id

        # 원본 삭제 + 동일 size·mtime 후보 2개 생성
        os.remove(self.ws / "tmp" / "orig.txt")
        (self.ws / "tmp" / "cand1.txt").write_bytes(b"B" * 100)
        (self.ws / "tmp" / "cand2.txt").write_bytes(b"C" * 100)
        now = time.time()
        os.utime(self.ws / "tmp" / "cand1.txt", (now, now))
        os.utime(self.ws / "tmp" / "cand2.txt", (now, now))
        report = self.manager.scan_root("workspace")

        self.assertEqual(len(report["ambiguous"]), 1)
        ambiguity = report["ambiguous"][0]
        self.assertEqual(ambiguity["id"], orig_id)
        self.assertEqual(
            sorted(ambiguity["candidates"]),
            ["tmp/cand1.txt", "tmp/cand2.txt"],
        )
        # old ref는 어느 쪽에도 조용히 묶이지 않는다
        stale = self.manager.registry.by_id(orig_id)
        self.assertTrue(stale.unresolved)
        self.assertTrue(stale.missing)
        self.assertEqual(stale.relative_path, "tmp/orig.txt")
        # 후보 파일들은 각자 새 identity를 받는다 (잘못 묶은 f-* 없음)
        for candidate in ("tmp/cand1.txt", "tmp/cand2.txt"):
            ref = self.manager.registry.by_path("workspace", candidate)
            self.assertIsNotNone(ref)
            self.assertNotEqual(ref.id, orig_id)


class Test6Modification(WorkspaceTestBase):
    def test_modify_preserves_identity_and_content_refreshes(self) -> None:
        self.manager.scan_root("workspace")
        ref = self.manager.registry.by_path("workspace", "experiments/lpips-notes.md")
        ref_id = ref.id

        time.sleep(0.05)
        (self.ws / "experiments" / "lpips-notes.md").write_text(
            "LPIPS notes v2: CHANGED metric results\n", encoding="utf-8"
        )
        report = self.manager.scan_root("workspace")
        self.assertEqual(report["renamed"], [])
        self.assertEqual(report["created"], 0)

        # identity 보존 + 새 canonical 내용이 읽기/검색에 반영
        content = self.manager.store.read_text(
            "experiments/lpips-notes.md", root="workspace"
        )
        self.assertIn("CHANGED", content["content"])
        result = self.manager.store.search("CHANGED metric", root="workspace")
        paths = [item["path"] for item in result["results"]]
        self.assertIn("experiments/lpips-notes.md", paths)
        again = self.manager.registry.by_path("workspace", "experiments/lpips-notes.md")
        self.assertEqual(again.id, ref_id)


class Test7Deletion(WorkspaceTestBase):
    def test_deleted_file_not_fabricated_metadata_retained(self) -> None:
        self.manager.scan_root("workspace")
        berg_id = self.manager.registry.by_path("workspace", "music/berg.md").id

        os.remove(self.ws / "music" / "berg.md")
        report = self.manager.scan_root("workspace")
        self.assertIn("music/berg.md", report["missing"])

        # files_snapshot(=실제 파일시스템 뷰)에 부활하지 않는다
        tree = self.manager.store.list_tree("workspace")
        snapshot_paths = [
            entry["path"] for entry in tree["entries"] if entry["type"] == "file"
        ]
        self.assertNotIn("music/berg.md", snapshot_paths)
        # 향후 복귀/이동 감지를 위해 metadata는 유지 (파일이 없어도)
        retained = self.manager.registry.by_id(berg_id)
        self.assertIsNotNone(retained)
        self.assertTrue(retained.missing)

        # 복귀하면 같은 identity로 돌아온다
        (self.ws / "music" / "berg.md").write_text("berg post-tonal notes\n", encoding="utf-8")
        self.manager.scan_root("workspace")
        restored = self.manager.registry.by_path("workspace", "music/berg.md")
        self.assertEqual(restored.id, berg_id)


class Test8SensitiveFiles(WorkspaceTestBase):
    def test_sensitive_files_never_indexed_or_exposed(self) -> None:
        (self.ws / ".env").write_text("SECRET_TOKEN=leak-me-not\n", encoding="utf-8")
        (self.ws / "credentials.txt").write_text("user: pass\n", encoding="utf-8")
        report = self.manager.scan_root("workspace")

        # 레지스트리에도 없다
        for bad in (".env", "credentials.txt"):
            self.assertIsNone(self.manager.registry.by_path("workspace", bad))
        # 검색으로 내용이 노출되지 않는다
        for query in ("SECRET_TOKEN", "leak-me-not", "user: pass"):
            result = self.manager.store.search(query, root="workspace")
            self.assertEqual(result["count"], 0, f"민감 내용 노출: {query}")
        # 읽기도 차단
        with self.assertRaises(FileStoreError):
            self.manager.store.read_text(".env", root="workspace")


class Test9PathContainment(WorkspaceTestBase):
    def test_traversal_and_outside_root_hard_rejected(self) -> None:
        self.manager.scan_root("workspace")
        for bad in ("../outside.txt", "papers/../../x.md", ".."):
            with self.assertRaises(FileStoreError):
                self.manager.store.read_text(bad, root="workspace")
            with self.assertRaises(FileStoreError):
                self.manager.store.list_tree("workspace", bad)
        for bad in ("C:/Windows/win.ini", "/etc/passwd"):
            with self.assertRaises(FileStoreError):
                self.manager.store.read_text(bad, root="workspace")


class Test10SystemMapIdentity(WorkspaceTestBase):
    def test_files_snapshot_identity_and_move(self) -> None:
        from harness.client import HarnessClient
        from scripts.harness_bridge import BridgeSession, handle_message

        cfg = HarnessConfig.load()
        cfg.memory_dir = self.mem
        cfg.task_file = self.mem / "tasks.md"
        cfg.file_roots = {"workspace": str(self.ws)}
        cfg.trace_dir = self.mem.parent / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)
        client = HarnessClient(cfg)
        session = BridgeSession()

        first = handle_message(client, session, {"type": "files_snapshot", "id": 1})
        self.assertEqual(first["status"], "ok")
        entries = {
            entry["path"]: entry
            for section in first["sections"]
            for entry in section["entries"]
            if entry["type"] == "file"
        }
        qian = entries["papers/qian2018.pdf"]
        self.assertTrue(qian["id"].startswith("f-"))
        self.assertEqual(qian["root_id"], "workspace")
        ref_id = qian["id"]

        # move → refresh → 같은 identity, 새 위치, 옛 노드 없음
        (self.ws / "references").mkdir()
        os.rename(
            self.ws / "papers" / "qian2018.pdf",
            self.ws / "references" / "qian-2018-cvpr.pdf",
        )
        second = handle_message(client, session, {"type": "files_snapshot", "id": 2})
        entries2 = {
            entry["path"]: entry
            for section in second["sections"]
            for entry in section["entries"]
            if entry["type"] == "file"
        }
        self.assertIn("references/qian-2018-cvpr.pdf", entries2)
        self.assertNotIn("papers/qian2018.pdf", entries2)
        self.assertEqual(entries2["references/qian-2018-cvpr.pdf"]["id"], ref_id)

        # refresh no-duplicate
        third = handle_message(client, session, {"type": "files_snapshot", "id": 3})
        paths3 = [
            entry["path"]
            for section in third["sections"]
            for entry in section["entries"]
            if entry["type"] == "file"
        ]
        self.assertEqual(len(paths3), len(set(paths3)))


class Test11SearchIdentity(WorkspaceTestBase):
    def test_search_preserves_identity_across_move(self) -> None:
        # search_context는 Project/Task 원천도 읽으므로 최소 memory fixture 필요
        (self.mem / "projects.md").write_text(
            "# Projects\n\n## Active\n\n- local-jarvis: test\n", encoding="utf-8"
        )
        discovery = Discovery(
            self.mem,
            task_file=self.mem / "tasks.md",
            file_roots={"workspace": str(self.ws)},
        )
        result = discovery.search_context("LPIPS")
        file_hits = [item for item in result["results"] if item["type"] == "file"]
        self.assertTrue(file_hits)
        before = file_hits[0]
        self.assertEqual(before["relative_path"] if "relative_path" in before else before["path"], "experiments/lpips-notes.md")
        self.assertTrue(str(before.get("id", "")).startswith("f-"))
        ref_id = before["id"]

        os.rename(
            self.ws / "experiments" / "lpips-notes.md",
            self.ws / "experiments" / "lpips-v2.md",
        )
        after_result = discovery.search_context("LPIPS")
        after = next(
            item for item in after_result["results"] if item["type"] == "file"
        )
        self.assertEqual(after["path"], "experiments/lpips-v2.md")  # 현재 locator
        self.assertEqual(after["id"], ref_id)  # 불변 identity


class Test12BridgeEndToEnd(WorkspaceTestBase):
    def test_full_bridge_path_list_read_search(self) -> None:
        from harness.client import HarnessClient
        from scripts.harness_bridge import BridgeSession, handle_message

        cfg = HarnessConfig.load()
        cfg.memory_dir = self.mem
        cfg.task_file = self.mem / "tasks.md"
        cfg.file_roots = {"workspace": str(self.ws)}
        cfg.trace_dir = self.mem.parent / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)
        client = HarnessClient(cfg)
        session = BridgeSession()

        # identity는 bridge를 통해 일관된다
        listing = handle_message(client, session, {"type": "files_snapshot", "id": 1})
        ids_via_snapshot = {
            entry["path"]: entry.get("id")
            for section in listing["sections"]
            for entry in section["entries"]
            if entry["type"] == "file"
        }
        read = client.execute_tool(
            "read_file", {"path": "experiments/lpips-notes.md", "root": "workspace"}
        )
        self.assertEqual(read.data["id"], ids_via_snapshot["experiments/lpips-notes.md"])
        search = client.execute_tool("search_files", {"query": "berg"})
        self.assertEqual(
            search.data["results"][0]["id"], ids_via_snapshot["music/berg.md"]
        )
        # registry file lives in memory_dir (scratch-safe)
        self.assertEqual(
            default_registry_file(self.mem), self.mem / "file_refs.json"
        )


class RegistryUnitTests(unittest.TestCase):
    def test_new_id_random_not_path_derived(self) -> None:
        ids = {new_file_ref_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_registry_roundtrip_and_corruption_recovery(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        reg_file = tmp / "file_refs.json"
        registry = FileRefRegistry(reg_file)
        registry.add(
            __import__(
                "harness.tools.workspace", fromlist=["FileRef"]
            ).FileRef(
                id="f-test12345678",
                root_id="r",
                relative_path="a/b.txt",
                name="b.txt",
            )
        )
        registry.save()
        reloaded = FileRefRegistry(reg_file)
        self.assertIsNotNone(reloaded.by_path("r", "a/b.txt"))
        # 손상된 JSON → 빈 레지스트리 (crash 금지)
        reg_file.write_text("{not json", encoding="utf-8")
        broken = FileRefRegistry(reg_file)
        self.assertEqual(broken.refs, {})


if __name__ == "__main__":
    unittest.main()
