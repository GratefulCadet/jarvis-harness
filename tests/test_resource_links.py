from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

"""RESOURCE LINK V1 수용 테스트 (PART K — milestone 검수 기준).

scratch fixture만 사용한다. 실제 사용자 memory·파일은 건드리지 않는다.

검증 매핑:
- Test 1/2   create reference/result   → link_project_file
- Test 3     restart                   → registry 재생성, 동일 rl-*/f-*
- Test 4     duplicate                 → created=False, 링크 1개 유지
- Test 5/6   rename/move linked file   → 동일 f-*, 새 locator, 링크 무손상
- Test 7-10  validation reject         → unknown project/file, path-as-id, relation
- Test 11/12 SYSTEM MAP projection    → tree_snapshot relation 그룹, refresh 중복 없음
- Test 13    unlink                    → JARVIS 메타데이터만 제거, 파일 무손상
- PART L     search provenance         → persisted 링크가 있을 때만 linked_projects

파일시스템은 canonical — resource_links.json은 JARVIS 메타데이터.
"""

from harness.tools.discovery import Discovery
from harness.tools.resource_links import (
    ProjectResources,
    default_links_file,
    new_link_id,
)


def _build_fixture(tmp: Path) -> dict[str, Path]:
    """scratch JARVIS memory + research workspace (PART K fixture)."""
    mem = tmp / "mem"
    mem.mkdir()
    (mem / "projects.md").write_text(
        "# Projects\n\n## Active\n\n"
        "- graduation-thesis: 졸업논문 프로젝트\n",
        encoding="utf-8",
    )
    (mem / "tasks.md").write_text(
        "# Tasks\n\n## graduation-thesis\n\n- [ ] t-fix11111111: setup — baseline\n",
        encoding="utf-8",
    )

    ws = tmp / "research"
    (ws / "papers").mkdir(parents=True)
    (ws / "results").mkdir()
    (ws / "papers" / "qian2018.pdf").write_bytes(b"%PDF-1.4 qian perceptual loss paper")
    (ws / "results" / "lpips.csv").write_text(
        "step,lpips\n1,0.42\n2,0.31\n", encoding="utf-8"
    )
    return {"mem": mem, "ws": ws}


def _make_discovery(fixture: dict[str, Path]) -> Discovery:
    return Discovery(
        fixture["mem"],
        file_roots={"research": str(fixture["ws"])},
    )


def _file_id(discovery: Discovery, relative_path: str) -> str:
    """현재 스캔 상태에서 relative_path → f-* identity."""
    assert discovery.workspace is not None
    discovery.workspace.ensure_scanned()
    ref = discovery.workspace.registry.by_path("research", relative_path)
    assert ref is not None, f"FileRef 없음: {relative_path}"
    return ref.id


class Test1CreateReference(unittest.TestCase):
    """Test 1 — Project --reference--> FileRef."""

    def test_create_reference(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "papers/qian2018.pdf")

        out = discovery.resources.link_project_file(
            "graduation-thesis", fid, relation="reference"
        )
        self.assertTrue(out["created"])
        link = out["link"]
        self.assertTrue(link["id"].startswith("rl-"))
        self.assertEqual(link["from_type"], "project")
        self.assertEqual(link["from_id"], "graduation-thesis")
        self.assertEqual(link["to_type"], "file")
        self.assertEqual(link["to_id"], fid)
        self.assertEqual(link["relation"], "reference")


class Test2CreateResult(unittest.TestCase):
    """Test 2 — Project --result--> FileRef."""

    def test_create_result(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")

        out = discovery.resources.link_project_file(
            "graduation-thesis", fid, relation="result"
        )
        self.assertTrue(out["created"])
        self.assertEqual(out["link"]["relation"], "result")

        listed = discovery.resources.list_project_resources("graduation-thesis")
        self.assertEqual(len(listed["resources"]), 1)
        entry = listed["resources"][0]
        self.assertEqual(entry["relation"], "result")
        self.assertEqual(entry["file"]["id"], fid)
        self.assertEqual(entry["file"]["relative_path"], "results/lpips.csv")
        self.assertEqual(entry["file"]["status"], "ok")


class Test3RestartPersistence(unittest.TestCase):
    """Test 3 — 재시작(관리자 재생성) 후 동일 링크/identity."""

    def test_restart_preserves_links(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid_a = _file_id(discovery, "papers/qian2018.pdf")
        fid_b = _file_id(discovery, "results/lpips.csv")
        r1 = discovery.resources.link_project_file("graduation-thesis", fid_a)
        r2 = discovery.resources.link_project_file(
            "graduation-thesis", fid_b, relation="result"
        )
        link_a, link_b = r1["link"]["id"], r2["link"]["id"]

        # '재시작': Discovery/ProjectResources를 완전히 새로 만든다.
        rediscovery = Discovery(
            fixture["mem"],
            file_roots={"research": str(fixture["ws"])},
        )
        listed = rediscovery.resources.list_project_resources("graduation-thesis")
        ids = {e["link_id"] for e in listed["resources"]}
        self.assertEqual(ids, {link_a, link_b})
        files = {e["file"]["id"] for e in listed["resources"]}
        self.assertEqual(files, {fid_a, fid_b})


class Test4DuplicateIdempotent(unittest.TestCase):
    """Test 4 — 동일 삼중항 재생성 → created=False, 링크 1개."""

    def test_duplicate_is_idempotent(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")

        first = discovery.resources.link_project_file(
            "graduation-thesis", fid, relation="result"
        )
        second = discovery.resources.link_project_file(
            "graduation-thesis", fid, relation="result"
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["link"]["id"], first["link"]["id"])
        listed = discovery.resources.list_project_resources("graduation-thesis")
        self.assertEqual(len(listed["resources"]), 1)


class Test5RenameLinkedFile(unittest.TestCase):
    """Test 5 — 링크된 파일 rename → 동일 f-*, 링크 무손상, 새 locator."""

    def test_rename_preserves_identity(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")
        discovery.resources.link_project_file("graduation-thesis", fid, relation="result")

        (fixture["ws"] / "results" / "lpips.csv").rename(
            fixture["ws"] / "results" / "lpips-final.csv"
        )

        # 재스캔 → reconcile이 동일 identity 유지해야 한다
        discovery.workspace.scan_root("research")
        ref = discovery.workspace.registry.by_id(fid)
        self.assertIsNotNone(ref)
        self.assertFalse(ref.missing)
        self.assertEqual(ref.relative_path, "results/lpips-final.csv")

        # ResourceLink는 재작성 없이 최신 locator를 리포트한다(PART G)
        listed = discovery.resources.list_project_resources("graduation-thesis")
        entry = listed["resources"][0]
        self.assertEqual(entry["file"]["id"], fid)
        self.assertEqual(entry["file"]["relative_path"], "results/lpips-final.csv")
        self.assertEqual(entry["file"]["name"], "lpips-final.csv")


class Test6MoveLinkedFile(unittest.TestCase):
    """Test 6 — 링크된 파일 move(동일 root) → 동일 f-* + 새 locator."""

    def test_move_preserves_identity(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")
        discovery.resources.link_project_file("graduation-thesis", fid, relation="result")

        # fixture에 experiments/ 디렉터리는 없으므로 먼저 만들고 이동한다
        (fixture["ws"] / "experiments").mkdir()
        (fixture["ws"] / "results" / "lpips.csv").rename(
            fixture["ws"] / "experiments" / "lpips.csv"
        )

        discovery.workspace.scan_root("research")
        ref = discovery.workspace.registry.by_id(fid)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.relative_path, "experiments/lpips.csv")

        listed = discovery.resources.list_project_resources("graduation-thesis")
        self.assertEqual(listed["resources"][0]["file"]["relative_path"], "experiments/lpips.csv")


class Test7UnknownProjectRejected(unittest.TestCase):
    """Test 7 — 존재하지 않는 Project → reject, zero mutation."""

    def test_unknown_project(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")

        with self.assertRaises(Exception) as ctx:
            discovery.resources.link_project_file("no-such-project", fid)
        self.assertIn("Project", str(ctx.exception))
        registry = discovery.resources.registry
        registry.reload()
        self.assertEqual(len(registry.all_links()), 0)


class Test8UnknownFileRefRejected(unittest.TestCase):
    """Test 8 — 존재하지 않는 FileRef → reject, zero mutation."""

    def test_unknown_file_ref(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)

        with self.assertRaises(Exception) as ctx:
            discovery.resources.link_project_file(
                "graduation-thesis", "f-doesnotexist00"
            )
        self.assertIn("FileRef", str(ctx.exception))
        registry = discovery.resources.registry
        registry.reload()
        self.assertEqual(len(registry.all_links()), 0)


class Test9PathInsteadOfFileRefRejected(unittest.TestCase):
    """Test 9 — 경로를 identity 필드로 전달 → 하드 거부."""

    def test_path_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)

        for bad in ("results/lpips.csv", "C:/research/results/lpips.csv", ".hidden"):
            with self.assertRaises(Exception) as ctx:
                discovery.resources.link_project_file("graduation-thesis", bad)
            self.assertIn("f-*", str(ctx.exception))
        registry = discovery.resources.registry
        registry.reload()
        self.assertEqual(len(registry.all_links()), 0)


class Test10UnsupportedRelationRejected(unittest.TestCase):
    """Test 10 — 지원하지 않는 relation → reject."""

    def test_unsupported_relation(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")

        with self.assertRaises(Exception) as ctx:
            discovery.resources.link_project_file(
                "graduation-thesis", fid, relation="owns"
            )
        self.assertIn("relation", str(ctx.exception))
        registry = discovery.resources.registry
        registry.reload()
        self.assertEqual(len(registry.all_links()), 0)


class Test11SystemMapProjection(unittest.TestCase):
    """Test 11 — PROJECTS의 Resources relation 그룹이 동일 f-* identity를 실는다.

    tree_snapshot은 bridge 레벨 기능이므로 handle_message로 검증한다(§PART H).
    """

    def test_tree_snapshot_carries_identity(self) -> None:
        from harness.client import HarnessClient
        from harness.config import HarnessConfig
        from scripts.harness_bridge import BridgeSession, handle_message

        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")
        discovery.resources.link_project_file("graduation-thesis", fid, relation="result")
        fid_a = _file_id(discovery, "papers/qian2018.pdf")
        discovery.resources.link_project_file(
            "graduation-thesis", fid_a, relation="reference"
        )

        cfg = HarnessConfig.load()
        cfg.memory_dir = fixture["mem"]
        cfg.task_file = fixture["mem"] / "tasks.md"
        cfg.file_roots = {"research": str(fixture["ws"])}
        cfg.trace_dir = tmp / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)
        client = HarnessClient(cfg)
        response = handle_message(client, BridgeSession(), {"type": "tree_snapshot", "id": 1})
        self.assertEqual(response["status"], "ok")

        proj = next(
            n for n in response["tree"]
            if n.get("type") == "project" and n.get("id") == "graduation-thesis"
        )
        # relation 그룹(Reference/Result 등) 아래 파일 노드가 실제 f-* identity를 실는다
        groups = proj["children"]
        self.assertTrue(
            any(g.get("type") == "resource_group" for g in groups),
            "resource_group 노드가 있어야 한다",
        )
        file_nodes = [
            n for g in groups if g.get("type") == "resource_group"
            for n in g.get("children", [])
        ]
        self.assertEqual(
            {n["file"]["id"] for n in file_nodes}, {fid, fid_a},
            "relation 그룹 아래 파일 노드가 실제 f-* identity를 실어야 한다",
        )
        relations = {g["title"] for g in groups if g.get("type") == "resource_group"}
        self.assertEqual(len(file_nodes), 2)
        self.assertTrue(relations)


class Test12RefreshNoDuplicates(unittest.TestCase):
    """Test 12 — refresh(재구성) 후 중복 리소스/노드 없음."""

    def test_refresh_no_duplicates(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")
        discovery.resources.link_project_file("graduation-thesis", fid, relation="result")

        listed1 = discovery.resources.list_project_resources("graduation-thesis")
        listed2 = discovery.resources.list_project_resources("graduation-thesis")
        link_ids_1 = [e["link_id"] for e in listed1["resources"]]
        link_ids_2 = [e["link_id"] for e in listed2["resources"]]
        self.assertEqual(link_ids_1, link_ids_2)
        self.assertEqual(len(link_ids_1), len(set(link_ids_1)), "중복 링크 없어야 함")

        listed = discovery.resources.list_project_resources("graduation-thesis")
        link_ids = [e["link_id"] for e in listed["resources"]]
        self.assertEqual(len(link_ids), len(set(link_ids)))
        self.assertEqual(len(link_ids), 1)


class Test13UnlinkMetadataOnly(unittest.TestCase):
    """Test 13 — unlink → JARVIS 메타데이터만 제거, 사용자 파일 무손상."""

    def test_unlink_preserves_file(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")
        out = discovery.resources.link_project_file(
            "graduation-thesis", fid, relation="result"
        )
        link_id = out["link"]["id"]
        csv_before = (fixture["ws"] / "results" / "lpips.csv").read_text(encoding="utf-8")

        removed = discovery.resources.unlink(link_id)
        self.assertTrue(removed["removed"])

        listed = discovery.resources.list_project_resources("graduation-thesis")
        self.assertEqual(listed["resources"], [])
        # 사용자 파일은 그대로
        self.assertEqual(
            (fixture["ws"] / "results" / "lpips.csv").read_text(encoding="utf-8"),
            csv_before,
        )
        # 다시 링크 가능(새 rl-*)
        re_out = discovery.resources.link_project_file(
            "graduation-thesis", fid, relation="result"
        )
        self.assertTrue(re_out["created"])
        self.assertNotEqual(re_out["link"]["id"], link_id)


class TestSearchProvenance(unittest.TestCase):
    """PART L — 검색 일치 ≠ persisted 관계. 링크가 있을 때만 provenance."""

    def test_provenance_only_with_persisted_link(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        fid = _file_id(discovery, "results/lpips.csv")

        # 링크 전: 파일 매치에 linked_projects 없음
        before = discovery.search_context("lpips")
        file_before = next(r for r in before["results"] if r["type"] == "file")
        self.assertNotIn("linked_projects", file_before)

        discovery.resources.link_project_file(
            "graduation-thesis", fid, relation="result"
        )

        # 링크 후: persisted provenance만 단다
        after = discovery.search_context("lpips")
        file_after = next(r for r in after["results"] if r["type"] == "file")
        self.assertEqual(len(file_after["linked_projects"]), 1)
        self.assertEqual(file_after["linked_projects"][0]["project_id"], "graduation-thesis")
        self.assertEqual(file_after["linked_projects"][0]["relation"], "result")

        # unlink → provenance 사라짐
        link_id = file_after["linked_projects"][0]["link_id"]
        discovery.resources.unlink(link_id)
        cleared = discovery.search_context("lpips")
        file_cleared = next(r for r in cleared["results"] if r["type"] == "file")
        self.assertNotIn("linked_projects", file_cleared)


class TestIdAndRegistryHelpers(unittest.TestCase):
    """모듈 수준 불변식 — ID 형식, default 경로, relation vocabulary."""

    def test_new_link_id_format(self) -> None:
        self.assertTrue(new_link_id().startswith("rl-"))
        self.assertNotEqual(new_link_id(), new_link_id())

    def test_default_links_file_under_memory(self) -> None:
        mem = Path(tempfile.mkdtemp())
        p = default_links_file(mem)
        self.assertEqual(p.parent, mem)
        self.assertEqual(p.name, "resource_links.json")

    def test_direct_api_rejects_empty_inputs(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        resources = ProjectResources(
            default_links_file(fixture["mem"]), fixture["mem"], workspace=None
        )
        with self.assertRaises(Exception):
            resources.link_project_file("", "f-x")
        with self.assertRaises(Exception):
            resources.unlink("")


if __name__ == "__main__":
    unittest.main()
