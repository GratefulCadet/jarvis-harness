from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.config import HarnessConfig
from harness.tools.page_store import (
    PageStore,
    derived_page_id,
    split_frontmatter,
)
from scripts.harness_bridge import BridgeSession, handle_message

"""Knowledge Markdown page store 검증 (Knowledge slice, read-only).

요구사항 매핑:
- frontmatter parent(id·상대경로)로 재귀 계층 구성          (snapshot)
- 안정적 id: explicit id 우선, 없으면 경로 해시             (stable ids)
- reload해도 같은 id·같은 구조 (결정적)                     (reload stability)
- dangling parent → 최상위 standalone                       (standalone pages)
- 순환 참조 → detached로 최상위 노출 + conflicts 보고       (cycle safety)
- 중복 explicit id → 파생 id로 충돌 해소 + conflicts 보고   (id conflicts)
- bridge pages_snapshot → 모델 호출 없이 status=ok          (bridge message)
- chat 모델 호출 없음 유지 (pages는 chat에 영향 없음)       (no model call)
"""


def write_page(root: Path, relative: str, meta: dict[str, str], body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if meta:
        front = "---\n" + "".join(
            f"{key}: {value}\n" for key, value in meta.items()
        ) + "---\n"
    else:
        front = ""
    path.write_text(front + body, encoding="utf-8")
    return path


class SplitFrontmatterTests(unittest.TestCase):
    def test_basic_split(self) -> None:
        meta, body = split_frontmatter("---\nid: a\ntitle: T\n---\n# T\n")
        self.assertEqual(meta, {"id": "a", "title": "T"})
        self.assertEqual(body, "# T")  # 마지막 개행은 splitlines 정규화로 떨어진다

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
        """Knowledge → Graduation → Experiments → LPIPS Results (4레벨)."""
        write_page(self.pages, "knowledge.md",
                   {"id": "knowledge", "title": "Knowledge"}, "# Knowledge\n")
        write_page(self.pages, "graduation.md",
                   {"id": "graduation", "parent": "knowledge",
                    "title": "Graduation"}, "# Graduation\n")
        write_page(self.pages, "experiments.md",
                   {"id": "experiments", "parent": "graduation",
                    "title": "Experiments"}, "# Experiments\n")
        write_page(self.pages, "lpips-results.md",
                   {"id": "lpips-results", "parent": "experiments",
                    "title": "LPIPS Results"}, "# LPIPS Results\n")

    def test_recursive_hierarchy(self) -> None:
        self._seed_three_levels()
        snapshot = PageStore(self.pages).snapshot()
        self.assertEqual(snapshot["count"], 4)
        self.assertEqual(snapshot["conflicts"], [])
        (root,) = snapshot["pages"]
        self.assertEqual(root["id"], "knowledge")
        self.assertEqual(root["type"], "page")
        self.assertIsNone(root["parent_id"])
        (grad,) = root["children"]
        self.assertEqual(grad["id"], "graduation")
        (exps,) = grad["children"]
        self.assertEqual(exps["id"], "experiments")
        (lpips,) = exps["children"]
        self.assertEqual(lpips["id"], "lpips-results")
        self.assertEqual(lpips["children"], [])

    def test_parent_by_relative_path(self) -> None:
        write_page(self.pages, "a.md", {"id": "a"}, "# A\n")
        write_page(self.pages, "sub/b.md", {"parent": "a"}, "# B\n")
        snapshot = PageStore(self.pages).snapshot()
        (root,) = snapshot["pages"]
        self.assertEqual(root["id"], "a")
        (child,) = root["children"]
        self.assertEqual(child["path"], "sub/b")
        self.assertEqual(child["parent_id"], "a")

    def test_derived_id_is_stable_hash_of_path(self) -> None:
        write_page(self.pages, "notes/plain.md", {}, "# Plain\n")
        snapshot = PageStore(self.pages).snapshot()
        (node,) = snapshot["pages"]
        self.assertEqual(node["id"], derived_page_id("notes/plain"))
        # reload → 동일 id
        again = PageStore(self.pages).snapshot()
        self.assertEqual(again["pages"][0]["id"], node["id"])

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
        write_page(self.pages, "orphan.md",
                   {"parent": "없는페이지"}, "# Orphan\n")
        snapshot = PageStore(self.pages).snapshot()
        (node,) = snapshot["pages"]
        self.assertEqual(node["path"], "orphan")
        self.assertIsNone(node["parent_id"])
        self.assertEqual(snapshot["conflicts"], [])

    def test_cycle_pages_exposed_detached_with_conflict(self) -> None:
        write_page(self.pages, "a.md", {"id": "a", "parent": "b"}, "# A\n")
        write_page(self.pages, "b.md", {"id": "b", "parent": "a"}, "# B\n")
        snapshot = PageStore(self.pages).snapshot()
        self.assertEqual(snapshot["count"], 2)
        paths = sorted(node["path"] for node in snapshot["pages"])
        self.assertEqual(paths, ["a", "b"])  # 최상위로 노출 — 데이터가 사라지지 않음
        for node in snapshot["pages"]:
            self.assertTrue(node.get("detached"))
        self.assertTrue(any("순환" in line for line in snapshot["conflicts"]))

    def test_duplicate_explicit_id_resolved_with_conflict(self) -> None:
        write_page(self.pages, "one.md", {"id": "dup"}, "# One\n")
        write_page(self.pages, "two.md", {"id": "dup"}, "# Two\n")
        snapshot = PageStore(self.pages).snapshot()
        ids = sorted(node["id"] for node in snapshot["pages"])
        self.assertNotEqual(ids[0], ids[1])  # 충돌 해소 — 두 노드 모두 존재
        self.assertTrue(any("중복 id" in line for line in snapshot["conflicts"]))

    def test_missing_dir_is_empty_snapshot(self) -> None:
        snapshot = PageStore(self.pages / "없음").snapshot()
        self.assertEqual(snapshot, {"pages": [], "count": 0, "conflicts": []})

    def test_reload_preserves_hierarchy_and_no_duplicates(self) -> None:
        self._seed_three_levels()
        store = PageStore(self.pages)
        first = store.snapshot()
        second = store.snapshot()
        self.assertEqual(first, second)  # 결정적 — reload 동일
        flat = []

        def walk(node: dict) -> None:
            flat.append(node["id"])
            for child in node["children"]:
                walk(child)

        for root in first["pages"]:
            walk(root)
        self.assertEqual(len(flat), len(set(flat)))  # 중복 노드 없음


class BridgePagesSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = self.root / "memory"
        self.memory.mkdir(parents=True)
        self.trace = self.root / "traces"
        self.trace.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _client(self) -> object:
        class NoModelClient:
            """chat_with_tools를 호출하면 실패 — 모델 호출이 없음을 강제 검증."""

            def __init__(self, memory_dir: Path, trace_dir: Path) -> None:
                self.config = HarnessConfig(
                    runtime="mock",
                    memory_dir=memory_dir,
                    trace_dir=trace_dir,
                )

            def chat_with_tools(self, *args, **kwargs):  # pragma: no cover
                raise AssertionError("pages_snapshot은 모델을 호출하면 안 된다")

        return NoModelClient(self.memory, self.trace)

    def test_pages_snapshot_ok_and_not_scratch_for_temp_dir(self) -> None:
        pages = self.memory / "pages"
        write_page(pages, "knowledge.md", {"id": "knowledge"}, "# Knowledge\n")
        write_page(pages, "graduation.md",
                   {"id": "graduation", "parent": "knowledge"}, "# Graduation\n")
        response = handle_message(
            self._client(), BridgeSession(), {"type": "pages_snapshot", "id": 9},
        )
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["id"], 9)
        self.assertEqual(response["count"], 2)
        # 임시 디렉터리는 프로젝트 data/ scratch가 아니므로 scratch=False
        self.assertFalse(response["scratch"])
        self.assertTrue(response["pages_dir"].endswith("pages"))
        (root,) = response["pages"]
        self.assertEqual(root["id"], "knowledge")
        self.assertEqual(root["children"][0]["id"], "graduation")

    def test_pages_snapshot_missing_dir_is_ok_empty(self) -> None:
        response = handle_message(
            self._client(), BridgeSession(), {"type": "pages_snapshot", "id": 10},
        )
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["pages"], [])
        self.assertEqual(response["count"], 0)

    def test_pages_snapshot_json_serializable(self) -> None:
        write_page(self.memory / "pages", "a.md", {"id": "a"}, "# A\n")
        response = handle_message(
            self._client(), BridgeSession(), {"type": "pages_snapshot", "id": 11},
        )
        encoded = json.dumps(response, ensure_ascii=False)  # 직렬화 가능해야 한다
        self.assertIn('"pages"', encoded)


if __name__ == "__main__":
    unittest.main()
