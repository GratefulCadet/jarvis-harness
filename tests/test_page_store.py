from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.config import HarnessConfig
from harness.tools.page_store import PageStore, derived_page_id, split_frontmatter
from scripts.harness_bridge import BridgeSession, handle_message

"""Read-only Page identity acceptance tests using scratch Markdown only."""


def write_page(root: Path, relative: str, meta: dict[str, str], body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    front = ""
    if meta:
        front = "---\n" + "".join(
            f"{key}: {value}\n" for key, value in meta.items()
        ) + "---\n"
    path.write_text(front + body, encoding="utf-8")
    return path


class SplitFrontmatterTests(unittest.TestCase):
    def test_basic_split(self) -> None:
        meta, body = split_frontmatter("---\nid: a\ntitle: T\n---\n# T\n")
        self.assertEqual(meta, {"id": "a", "title": "T"})
        self.assertEqual(body, "# T")

    def test_no_frontmatter(self) -> None:
        meta, body = split_frontmatter("# 그냥 문서\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "# 그냥 문서\n")

    def test_unclosed_fence_is_body(self) -> None:
        meta, body = split_frontmatter("---\nid: a\n# 아님\n")
        self.assertEqual(meta, {})
        self.assertIn("id: a", body)


class PageStoreSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.pages = Path(self.tmp.name) / "pages"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_three_levels(self) -> None:
        write_page(self.pages, "knowledge.md", {"id": "knowledge", "title": "Knowledge"}, "# Knowledge\n")
        write_page(self.pages, "graduation.md", {"id": "graduation", "parent": "knowledge", "title": "Graduation"}, "# Graduation\n")
        write_page(self.pages, "experiments.md", {"id": "experiments", "parent": "graduation", "title": "Experiments"}, "# Experiments\n")
        write_page(self.pages, "lpips-results.md", {"id": "lpips-results", "parent": "experiments", "title": "LPIPS Results"}, "# LPIPS Results\n")

    def test_recursive_hierarchy(self) -> None:
        self._seed_three_levels()
        snapshot = PageStore(self.pages).snapshot()
        self.assertEqual(snapshot["count"], 4)
        (root,) = snapshot["pages"]
        self.assertEqual(root["id"], "knowledge")
        (graduation,) = root["children"]
        (experiments,) = graduation["children"]
        self.assertEqual(experiments["children"][0]["id"], "lpips-results")

    def test_parent_by_relative_path(self) -> None:
        write_page(self.pages, "a.md", {"id": "a"}, "# A\n")
        write_page(self.pages, "sub/b.md", {"parent": "a"}, "# B\n")
        snapshot = PageStore(self.pages).snapshot()
        (root,) = snapshot["pages"]
        self.assertEqual(root["children"][0]["parent_id"], "a")

    def test_legacy_path_fallback_is_initial_only(self) -> None:
        write_page(self.pages, "notes/plain.md", {}, "# Plain\n")
        store = PageStore(self.pages)
        first = store.snapshot()
        (node,) = first["pages"]
        self.assertEqual(node["id"], derived_page_id("notes/plain"))

        (self.pages / "notes" / "plain.md").rename(self.pages / "renamed.md")
        moved = store.snapshot()["pages"][0]
        self.assertEqual(moved["id"], node["id"])
        self.assertEqual(moved["path"], "renamed")
        self.assertEqual(PageStore(self.pages).snapshot()["pages"][0]["id"], node["id"])

    def test_title_and_parent_changes_preserve_identity(self) -> None:
        write_page(self.pages, "knowledge.md", {"id": "knowledge"}, "# Knowledge\n")
        write_page(self.pages, "child.md", {"parent": "knowledge"}, "# Child\n")
        store = PageStore(self.pages)
        child_id = store.snapshot()["pages"][0]["children"][0]["id"]

        write_page(self.pages, "child.md", {"title": "Renamed"}, "# Renamed\n")
        snapshot = store.snapshot()
        child = next(node for node in snapshot["pages"] if node["path"] == "child")
        self.assertEqual(child["id"], child_id)
        self.assertEqual(child["title"], "Renamed")
        self.assertIsNone(child["parent_id"])

    def test_explicit_id_survives_move_title_and_parent_change(self) -> None:
        write_page(self.pages, "knowledge.md", {"id": "knowledge"}, "# Knowledge\n")
        write_page(self.pages, "child.md", {"id": "page-child", "parent": "knowledge"}, "# Child\n")
        store = PageStore(self.pages)
        self.assertEqual(store.snapshot()["pages"][0]["children"][0]["id"], "page-child")

        (self.pages / "child.md").rename(self.pages / "renamed.md")
        write_page(self.pages, "renamed.md", {"id": "page-child", "title": "Renamed"}, "# Renamed\n")
        node = next(node for node in store.snapshot()["pages"] if node["path"] == "renamed")
        self.assertEqual(node["id"], "page-child")
        self.assertEqual(node["title"], "Renamed")
        self.assertIsNone(node["parent_id"])

    def test_physical_move_does_not_change_semantic_parent(self) -> None:
        write_page(self.pages, "knowledge.md", {"id": "knowledge"}, "# Knowledge\n")
        write_page(self.pages, "child.md", {"id": "child", "parent": "knowledge"}, "# Child\n")
        store = PageStore(self.pages)
        store.snapshot()

        (self.pages / "renamed").mkdir()
        (self.pages / "child.md").rename(self.pages / "renamed" / "child.md")
        moved = next(node for node in store.snapshot()["pages"][0]["children"] if node["id"] == "child")
        self.assertEqual(moved["path"], "renamed/child")
        self.assertEqual(moved["parent_id"], "knowledge")

    def test_duplicate_explicit_id_is_flagged_without_duplicate_identity(self) -> None:
        write_page(self.pages, "one.md", {"id": "same"}, "# One\n")
        write_page(self.pages, "two.md", {"id": "same"}, "# Two\n")
        snapshot = PageStore(self.pages).snapshot()
        self.assertTrue(any("중복 id" in item for item in snapshot["conflicts"]))
        ids = [node["id"] for node in snapshot["pages"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], "same")
        self.assertNotEqual(ids[1], "same")

    def test_deleted_page_is_not_silently_rebound(self) -> None:
        write_page(self.pages, "old.md", {}, "# Old\n")
        store = PageStore(self.pages)
        old_id = store.snapshot()["pages"][0]["id"]
        (self.pages / "old.md").unlink()
        self.assertEqual(store.snapshot()["pages"], [])
        write_page(self.pages, "new.md", {}, "# Old\n")
        self.assertNotEqual(store.snapshot()["pages"][0]["id"], old_id)
        registry = json.loads((self.pages.parent / "page_identity.json").read_text(encoding="utf-8"))
        self.assertTrue(any(item["id"] == old_id and item["missing"] for item in registry["pages"]))

    def test_ambiguous_reconciliation_does_not_bind_old_identity(self) -> None:
        write_page(self.pages, "first.md", {}, "# Same\n")
        write_page(self.pages, "second.md", {}, "# Same\n")
        store = PageStore(self.pages)
        old_ids = {node["id"] for node in store.snapshot()["pages"]}
        (self.pages / "first.md").unlink()
        (self.pages / "second.md").unlink()
        write_page(self.pages, "a.md", {}, "# Same\n")
        write_page(self.pages, "b.md", {}, "# Same\n")
        snapshot = store.snapshot()
        self.assertTrue(any("애매" in item for item in snapshot["conflicts"]))
        self.assertTrue({node["id"] for node in snapshot["pages"]}.isdisjoint(old_ids))

    def test_search_context_keeps_page_identity_after_move(self) -> None:
        memory = self.pages.parent / "memory"
        memory.mkdir()
        (memory / "projects.md").write_text(
            "# Projects\n\n## Active\n\n- demo: Demo\n", encoding="utf-8"
        )
        pages = memory / "pages"
        write_page(pages, "notes/lpips.md", {}, "# LPIPS Results\n")
        from harness.tools.discovery import Discovery

        discovery = Discovery(memory)
        before = next(
            item for item in discovery.search_context("LPIPS")["results"]
            if item["type"] == "page"
        )
        (pages / "notes" / "lpips.md").rename(pages / "renamed.md")
        after = next(
            item for item in discovery.search_context("LPIPS")["results"]
            if item["type"] == "page"
        )
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["path"], "renamed")

    def test_title_from_frontmatter_then_h1_then_stem(self) -> None:
        write_page(self.pages, "x.md", {"title": "메타 제목"}, "# 본문 제목\n")
        write_page(self.pages, "y.md", {}, "# 본문 제목\n")
        write_page(self.pages, "z.md", {}, "본문만\n")
        snapshot = PageStore(self.pages).snapshot()
        titles = {node["path"]: node["title"] for node in snapshot["pages"]}
        self.assertEqual(titles["x"], "메타 제목")
        self.assertEqual(titles["y"], "본문 제목")
        self.assertEqual(titles["z"], "z")

    def test_dangling_parent_becomes_standalone(self) -> None:
        write_page(self.pages, "orphan.md", {"parent": "없는페이지"}, "# Orphan\n")
        node = PageStore(self.pages).snapshot()["pages"][0]
        self.assertIsNone(node["parent_id"])
        self.assertEqual(node["path"], "orphan")

    def test_cycle_pages_exposed_detached_with_conflict(self) -> None:
        write_page(self.pages, "a.md", {"id": "a", "parent": "b"}, "# A\n")
        write_page(self.pages, "b.md", {"id": "b", "parent": "a"}, "# B\n")
        snapshot = PageStore(self.pages).snapshot()
        self.assertEqual(sorted(node["path"] for node in snapshot["pages"]), ["a", "b"])
        self.assertTrue(all(node.get("detached") for node in snapshot["pages"]))
        self.assertTrue(any("순환" in item for item in snapshot["conflicts"]))

    def test_missing_dir_is_empty_snapshot(self) -> None:
        self.assertEqual(PageStore(self.pages / "없음").snapshot(), {"pages": [], "count": 0, "conflicts": []})

    def test_reload_preserves_hierarchy_and_no_duplicates(self) -> None:
        self._seed_three_levels()
        store = PageStore(self.pages)
        first = store.snapshot()
        self.assertEqual(first, store.snapshot())
        flat: list[str] = []

        def walk(node: dict) -> None:
            flat.append(node["id"])
            for child in node["children"]:
                walk(child)

        for root in first["pages"]:
            walk(root)
        self.assertEqual(len(flat), len(set(flat)))


class BridgePagesSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = self.root / "memory"
        self.memory.mkdir()
        self.trace = self.root / "traces"
        self.trace.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _client(self) -> object:
        class NoModelClient:
            def __init__(self, memory_dir: Path, trace_dir: Path) -> None:
                self.config = HarnessConfig(runtime="mock", memory_dir=memory_dir, trace_dir=trace_dir)

            def chat_with_tools(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("pages_snapshot must not call the model")

        return NoModelClient(self.memory, self.trace)

    def test_pages_snapshot_reads_recursive_pages(self) -> None:
        pages = self.memory / "pages"
        write_page(pages, "knowledge.md", {"id": "knowledge"}, "# Knowledge\n")
        write_page(pages, "graduation.md", {"id": "graduation", "parent": "knowledge"}, "# Graduation\n")
        response = handle_message(self._client(), BridgeSession(), {"type": "pages_snapshot", "id": 9})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["count"], 2)
        self.assertFalse(response["scratch"])
        self.assertEqual(response["pages"][0]["children"][0]["id"], "graduation")

    def test_pages_snapshot_missing_dir_is_empty_and_serializable(self) -> None:
        response = handle_message(self._client(), BridgeSession(), {"type": "pages_snapshot", "id": 10})
        self.assertEqual(response["pages"], [])
        self.assertEqual(response["count"], 0)
        self.assertIn('"pages"', json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
