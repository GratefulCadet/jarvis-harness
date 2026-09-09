from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

"""PROJECT PRIMARY WORKSPACE V1 수용 테스트 (PART J — scratch only).

scratch fixture만 사용한다. 실제 사용자 memory·파일은 건드리지 않는다.

검증 매핑:
- Test 1  set + persist        → set_project_primary_workspace
- Test 2  restart              → 관리자 재생성 후 동일 root_id
- Test 3  read context         → get returns root_id 'thesis'
- Test 4  invalid project      → reject, zero mutation
- Test 5  unknown root         → reject, zero mutation
- Test 6  path-as-root_id      → reject
- Test 7  change workspace     → thesis → jarvis-root, identity/link 유지
- Test 8  clear                → 관계 제거, project/file 무손상
- Test 9  unavailable device   → available:false, 메타데이터 보존
- Test 10 SYSTEM MAP           → tree_snapshot Workspace 노드
- Test 11 refresh no-dup       → snapshot 반복 조회 시 노드 중복 없음
- Test 12 regressions          → discovery/resource_links 회귀 (별도 파일 존재)

파일시스템도 projects.md도 수정하지 않는다 — project_workspaces.json만.
"""

from harness.config import HarnessConfig
from harness.tools.discovery import Discovery
from harness.tools.project_workspaces import (
    ProjectWorkspaceError,
    ProjectWorkspaces,
    default_project_workspaces_file,
)
from harness.tools.resource_links import ResourceLinkRegistry, default_links_file


def _build_fixture(tmp: Path) -> dict[str, Path]:
    """scratch memory + 2 workspace roots (PART J fixture)."""
    mem = tmp / "mem"
    mem.mkdir()
    (mem / "projects.md").write_text(
        "# Projects\n\n## Active\n\n"
        "- graduation-thesis: 졸업논문\n"
        "- jarvis: JARVIS\n",
        encoding="utf-8",
    )
    (mem / "tasks.md").write_text(
        "# Tasks\n\n## graduation-thesis\n\n- [ ] t-fix11111111: setup — baseline\n",
        encoding="utf-8",
    )

    thesis = tmp / "thesis"
    thesis.mkdir()
    (thesis / "papers").mkdir()
    (thesis / "papers" / "lpips.csv").write_text("step,lpips\n1,0.42\n", encoding="utf-8")

    jarvis_ws = tmp / "jarvis-ws"
    jarvis_ws.mkdir()

    return {"mem": mem, "thesis": thesis, "jarvis_ws": jarvis_ws}


def _roots(fixture: dict[str, Path]) -> dict[str, str]:
    return {
        "thesis": str(fixture["thesis"]),
        "jarvis-root": str(fixture["jarvis_ws"]),
    }


def _make_discovery(fixture: dict[str, Path]) -> Discovery:
    return Discovery(fixture["mem"], file_roots=_roots(fixture))


class Test1SetAndPersist(unittest.TestCase):
    """Test 1 — graduation-thesis → thesis 설정 + 영속화."""

    def test_set_persists(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        pw = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots=_roots(fixture),
        )

        out = pw.set_project_primary_workspace("graduation-thesis", "thesis")
        self.assertTrue(out["project_id"], "graduation-thesis")
        self.assertEqual(out["root_id"], "thesis")
        self.assertFalse(out["updated"])  # 신규 관계

        registry_file = default_project_workspaces_file(fixture["mem"])
        self.assertTrue(registry_file.exists())
        self.assertIn("graduation-thesis", registry_file.read_text(encoding="utf-8"))

        # projects.md는 절대 수정되지 않는다
        md = (fixture["mem"] / "projects.md").read_text(encoding="utf-8")
        self.assertNotIn("primary", md.lower())
        self.assertNotIn("thesis→", md)


class Test2RestartPersistence(unittest.TestCase):
    """Test 2 — 관리자 재생성(재시작) 후 관계 유지."""

    def test_restart_preserves_relationship(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        pw = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots=_roots(fixture),
        )
        pw.set_project_primary_workspace("graduation-thesis", "thesis")

        restarted = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots=_roots(fixture),
        )
        got = restarted.get_project_primary_workspace("graduation-thesis")
        self.assertIsNotNone(got)
        self.assertEqual(got["root_id"], "thesis")
        self.assertTrue(got["available"])


class Test3ReadContext(unittest.TestCase):
    """Test 3 — project context 읽기가 root_id를 반환."""

    def test_read_returns_root_id(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        discovery.project_workspaces.set_project_primary_workspace(
            "graduation-thesis", "thesis"
        )

        detailed = discovery.list_projects_detailed()
        entry = next(p for p in detailed if p["id"] == "graduation-thesis")
        self.assertEqual(entry["primary_workspace"]["root_id"], "thesis")
        self.assertTrue(entry["primary_workspace"]["available"])

        # 관계 없는 프로젝트에는 필드 자체가 없다 (fabricate 금지)
        jarvis_entry = next(p for p in detailed if p["id"] == "jarvis")
        self.assertNotIn("primary_workspace", jarvis_entry)


class Test4InvalidProject(unittest.TestCase):
    """Test 4 — 존재하지 않는 Project → reject, zero mutation."""

    def test_unknown_project_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        pw = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots=_roots(fixture),
        )

        with self.assertRaises(ProjectWorkspaceError) as ctx:
            pw.set_project_primary_workspace("no-such-project", "thesis")
        self.assertIn("Project", str(ctx.exception))
        self.assertEqual(pw._load(), {})  # zero mutation


class Test5UnknownRoot(unittest.TestCase):
    """Test 5 — 이 디바이스에 없는 root → reject, zero mutation."""

    def test_unknown_root_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        pw = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots=_roots(fixture),
        )

        with self.assertRaises(ProjectWorkspaceError) as ctx:
            pw.set_project_primary_workspace("graduation-thesis", "unknown-root")
        self.assertIn("root", str(ctx.exception))
        self.assertEqual(pw._load(), {})


class Test6PathAsRootIdRejected(unittest.TestCase):
    """Test 6 — 절대/상대 경로를 root_id로 → 하드 거부 (§7)."""

    def test_path_forms_rejected(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        pw = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots=_roots(fixture),
        )

        for bad in (
            "C:/Research/graduation-thesis",
            "C:\\Research\\graduation-thesis",
            "scratch/thesis",
            "./thesis",
            "D:/",
        ):
            with self.assertRaises(ProjectWorkspaceError) as ctx:
                pw.set_project_primary_workspace("graduation-thesis", bad)
            self.assertIn("root_id", str(ctx.exception))
        self.assertEqual(pw._load(), {})


class Test7ChangeWorkspace(unittest.TestCase):
    """Test 7 — thesis → jarvis-root 변경. Project identity·ResourceLink 유지."""

    def test_change_preserves_identity_and_links(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        discovery = _make_discovery(fixture)
        pw = discovery.project_workspaces

        pw.set_project_primary_workspace("graduation-thesis", "thesis")

        # 기존 ResourceLink 하나 생성 (rename/move와 무관하게 유지돼야 한다)
        discovery.workspace.ensure_scanned()
        ref = discovery.workspace.registry.by_path("thesis", "papers/lpips.csv")
        self.assertIsNotNone(ref)
        link = discovery.resources.link_project_file(
            "graduation-thesis", ref.id, relation="result"
        )
        link_id = link["link"]["id"]

        # 변경
        out = pw.set_project_primary_workspace("graduation-thesis", "jarvis-root")
        self.assertTrue(out["updated"])
        self.assertEqual(out["root_id"], "jarvis-root")

        # Project identity 불변, ResourceLink 불변
        detailed = discovery.list_projects_detailed()
        entry = next(p for p in detailed if p["id"] == "graduation-thesis")
        self.assertEqual(entry["id"], "graduation-thesis")
        self.assertEqual(entry["primary_workspace"]["root_id"], "jarvis-root")

        registry = ResourceLinkRegistry(default_links_file(fixture["mem"]))
        registry.reload()
        links = registry.links_for_project("graduation-thesis")
        self.assertEqual([l.id for l in links], [link_id])
        self.assertEqual([l.relation for l in links], ["result"])


class Test8Clear(unittest.TestCase):
    """Test 8 — clear: 관계만 제거. Project·파일 무손상."""

    def test_clear_removes_relationship_only(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        pw = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots=_roots(fixture),
        )
        pw.set_project_primary_workspace("graduation-thesis", "thesis")

        out = pw.clear_project_primary_workspace("graduation-thesis")
        self.assertTrue(out["removed"])
        self.assertIsNone(pw.get_project_primary_workspace("graduation-thesis"))

        # Project는 그대로, 파일도 그대로
        reader_projects = [
            p["id"]
            for p in __import__(
                "harness.tools.memory_context", fromlist=["MemoryContextReader"]
            )
            .MemoryContextReader(fixture["mem"])
            .list_projects()
        ]
        self.assertIn("graduation-thesis", reader_projects)
        self.assertTrue((fixture["thesis"] / "papers" / "lpips.csv").exists())

        # 재-clear는 no-op (removed: False)
        again = pw.clear_project_primary_workspace("graduation-thesis")
        self.assertFalse(again["removed"])


class Test9UnavailableDevice(unittest.TestCase):
    """Test 9 — 이 디바이스에 루트 미설정 → available:false, 메타데이터 보존."""

    def test_unavailable_root_preserved(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        pw = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots=_roots(fixture),
        )
        pw.set_project_primary_workspace("graduation-thesis", "thesis")

        # '다른 디바이스': thesis 루트가 config에 없음
        other_device = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots={"jarvis-root": str(fixture["jarvis_ws"])},
        )
        got = other_device.get_project_primary_workspace("graduation-thesis")
        self.assertIsNotNone(got, "관계 메타데이터는 삭제되지 않는다")
        self.assertEqual(got["root_id"], "thesis")
        self.assertFalse(got["available"])
        self.assertEqual(got["reason"], "root_not_configured")

        # device_path가 사라진 경우도 available:false
        import shutil

        missing_root = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots={"thesis": str(tmp / "deleted-path")},
        )
        got2 = missing_root.get_project_primary_workspace("graduation-thesis")
        self.assertFalse(got2["available"])
        self.assertEqual(got2["reason"], "path_unavailable")

        # 원래 디바이스는 여전히 available
        home = ProjectWorkspaces(
            default_project_workspaces_file(fixture["mem"]),
            fixture["mem"],
            roots=_roots(fixture),
        )
        self.assertTrue(
            home.get_project_primary_workspace("graduation-thesis")["available"]
        )


class Test10SystemMapWorkspaceNode(unittest.TestCase):
    """Test 10 — tree_snapshot의 Project 아래 Workspace semantic 노드 1개."""

    def test_workspace_node_present(self) -> None:
        from harness.client import HarnessClient
        from scripts.harness_bridge import BridgeSession, handle_message

        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)

        cfg = HarnessConfig.load()
        cfg.memory_dir = fixture["mem"]
        cfg.task_file = fixture["mem"] / "tasks.md"
        cfg.file_roots = _roots(fixture)
        cfg.trace_dir = tmp / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)
        client = HarnessClient(cfg)
        session = BridgeSession()

        set_resp = handle_message(
            client,
            session,
            {
                "type": "set_project_workspace",
                "id": 1,
                "project_id": "graduation-thesis",
                "root_id": "thesis",
            },
        )
        self.assertEqual(set_resp["status"], "ok", set_resp)

        tree = handle_message(client, session, {"type": "tree_snapshot", "id": 2})
        self.assertEqual(tree["status"], "ok")
        proj = next(
            n for n in tree["tree"]
            if n["type"] == "project" and n["id"] == "graduation-thesis"
        )

        ws_groups = [c for c in proj["children"] if c.get("type") == "workspace_group"]
        self.assertEqual(len(ws_groups), 1)
        leaves = ws_groups[0]["children"]
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0]["type"], "project_workspace")
        self.assertEqual(leaves[0]["root_id"], "thesis")
        self.assertTrue(leaves[0]["available"])
        self.assertEqual(leaves[0]["status"], "available")

        # 관계 없는 프로젝트에는 workspace_group이 없다
        jarvis_proj = next(
            n for n in tree["tree"] if n["type"] == "project" and n["id"] == "jarvis"
        )
        self.assertNotIn(
            "workspace_group", [c.get("type") for c in jarvis_proj["children"]]
        )


class Test11RefreshNoDuplicates(unittest.TestCase):
    """Test 11 — refresh(재조회) 시 Workspace 노드 중복 없음."""

    def test_repeated_snapshot_no_duplicates(self) -> None:
        from harness.client import HarnessClient
        from scripts.harness_bridge import BridgeSession, handle_message

        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)

        cfg = HarnessConfig.load()
        cfg.memory_dir = fixture["mem"]
        cfg.task_file = fixture["mem"] / "tasks.md"
        cfg.file_roots = _roots(fixture)
        cfg.trace_dir = tmp / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)
        client = HarnessClient(cfg)
        session = BridgeSession()

        handle_message(
            client,
            session,
            {
                "type": "set_project_workspace",
                "id": 1,
                "project_id": "graduation-thesis",
                "root_id": "thesis",
            },
        )

        def ws_node_ids() -> list[str]:
            tree = handle_message(client, session, {"type": "tree_snapshot", "id": 99})
            proj = next(
                n for n in tree["tree"]
                if n["type"] == "project" and n["id"] == "graduation-thesis"
            )
            ids = []
            for child in proj["children"]:
                if child.get("type") == "workspace_group":
                    ids.append(child["id"])
                    ids.extend(leaf["id"] for leaf in child.get("children", []))
            return ids

        first = ws_node_ids()
        second = ws_node_ids()
        third = ws_node_ids()
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(len(first), len(set(first)), "workspace 노드 중복 없어야 함")


class TestSearchEvidenceLabeled(unittest.TestCase):
    """PART E — workspace 경로/표시명은 명시적 라벨의 2차 근거일 뿐이다."""

    def test_workspace_match_labeled_and_identity_unchanged(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)
        # 'graduation'은 어느 project id/title/task에도 없고
        # workspace device_path에만 존재하도록 루트를 만든다
        ws_path = tmp / "Research" / "graduation-thesis"
        ws_path.mkdir(parents=True)
        discovery = Discovery(
            fixture["mem"],
            file_roots={"thesis": str(ws_path), "jarvis-root": str(fixture["jarvis_ws"])},
        )
        discovery.project_workspaces.set_project_primary_workspace(
            "jarvis", "thesis"
        )

        result = discovery.search_projects("graduation")
        matches = [r for r in result["results"] if r["id"] == "jarvis"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["matched_on"], "primary_workspace")

        # id/title 일치와 동일하게 Project identity는 그대로 반환된다
        self.assertEqual(matches[0]["type"], "project")


class TestBridgeSetGetClear(unittest.TestCase):
    """bridge handle_message 수준의 set/get/clear 전 경로."""

    def test_bridge_full_round_trip(self) -> None:
        from harness.client import HarnessClient
        from scripts.harness_bridge import BridgeSession, handle_message

        tmp = Path(tempfile.mkdtemp())
        fixture = _build_fixture(tmp)

        cfg = HarnessConfig.load()
        cfg.memory_dir = fixture["mem"]
        cfg.task_file = fixture["mem"] / "tasks.md"
        cfg.file_roots = _roots(fixture)
        cfg.trace_dir = tmp / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)
        client = HarnessClient(cfg)
        session = BridgeSession()

        def send(payload: dict) -> dict:
            return handle_message(client, session, payload)

        set_resp = send({
            "type": "set_project_workspace",
            "id": 1,
            "project_id": "graduation-thesis",
            "root_id": "thesis",
        })
        self.assertEqual(set_resp["status"], "ok")
        self.assertEqual(set_resp["root_id"], "thesis")

        get_resp = send({
            "type": "get_project_workspace",
            "id": 2,
            "project_id": "graduation-thesis",
        })
        self.assertEqual(get_resp["status"], "ok")
        self.assertEqual(get_resp["workspace"]["root_id"], "thesis")

        # path-as-id via bridge
        bad = send({
            "type": "set_project_workspace",
            "id": 3,
            "project_id": "graduation-thesis",
            "root_id": "C:/thesis",
        })
        self.assertEqual(bad["status"], "error")
        self.assertIn("root_id", bad["error"])

        # unknown root via bridge
        bad2 = send({
            "type": "set_project_workspace",
            "id": 4,
            "project_id": "graduation-thesis",
            "root_id": "nope",
        })
        self.assertEqual(bad2["status"], "error")

        clear_resp = send({
            "type": "clear_project_workspace",
            "id": 5,
            "project_id": "graduation-thesis",
        })
        self.assertEqual(clear_resp["status"], "ok")
        self.assertTrue(clear_resp["removed"])

        get_after = send({
            "type": "get_project_workspace",
            "id": 6,
            "project_id": "graduation-thesis",
        })
        self.assertIsNone(get_after["workspace"])


if __name__ == "__main__":
    unittest.main()
