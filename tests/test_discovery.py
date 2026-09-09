from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

"""Context Discovery 수용 테스트 (PART F — milestone 검수 기준).

scratch fixture만 사용한다. 실제 사용자 memory·파일은 건드리지 않는다.

검증 매핑:
- Test 1 enumerate        : list_projects_detailed / discover_projects
- Test 2 name lookup      : search_projects('JARVIS') → exact id resolution
- Test 3 task-content     : search_projects('보컬 앱 아이디어') → matched_on: task
- Test 4 page search      : search_context('LPIPS') → Page (project 소속 추론 없음)
- Test 5 files            : list/search/read + SYSTEM MAP snapshot + reload 안정성
- Test 6 invalid path     : traversal/루트 밖/민감 경로 하드 실패 + 내용 0 노출

bridge 메시지(discover_projects/search_context/files_snapshot)도
handle_message 수준에서 같은 fixture로 검증한다.
"""

from harness.config import HarnessConfig
from harness.tools.discovery import Discovery
from harness.tools.file_store import FileStoreError


def _build_fixture(tmp: Path) -> dict[str, Path]:
    """projects/tasks/pages/workspace scratch fixture (Test 1–6 공용)."""
    mem = tmp / "mem"
    mem.mkdir()
    (mem / "projects.md").write_text(
        "# Projects\n\n## Active\n\n"
        "- jarvis-app: Electron JARVIS (PiP + Command Center)\n"
        "- local-jarvis: 로컬 우선 개인 AI 비서\n",
        encoding="utf-8",
    )
    (mem / "tasks.md").write_text(
        "# Tasks\n\n"
        "## jarvis-app\n\n"
        "- [ ] t-33adec5fdf00: PiP 통합 확인 — electron 브리지 스모크\n\n"
        "## local-jarvis\n\n"
        "- [ ] t-704ace97d0ab: 보컬 앱 아이디어 탐색 — 사용자 요청\n"
        "- [x] t-aaaa11111111: LPIPS evaluation — lpips 지표 실험\n",
        encoding="utf-8",
    )
    pages = mem / "pages"
    (pages / "graduation" / "experiments").mkdir(parents=True)
    (pages / "graduation.md").write_text(
        "---\nid: graduation\ntitle: Graduation\n---\n# Graduation\n",
        encoding="utf-8",
    )
    (pages / "graduation" / "experiments" / "lpips-results.md").write_text(
        "---\nid: lpips-results\ntitle: LPIPS Results\nparent: graduation\n---\n"
        "# LPIPS Results\nlpips 0.28 지표 기록\n",
        encoding="utf-8",
    )
    (pages / "standalone-note.md").write_text(
        "---\nid: standalone-note\ntitle: 독립 노트\n---\n"
        "# 독립 노트\nLPIPS와 무관한 내용\n",
        encoding="utf-8",
    )
    workspace = tmp / "workspace"
    (workspace / "graduation" / "experiments").mkdir(parents=True)
    (workspace / "music").mkdir()
    (workspace / "graduation" / "experiments" / "lpips-notes.md").write_text(
        "LPIPS notes: perceptual metric\n",
        encoding="utf-8",
    )
    (workspace / "graduation" / "paper.txt").write_text(
        "thesis draft text\n",
        encoding="utf-8",
    )
    (workspace / "music" / "post-tonal.md").write_text(
        "post tonal theory notes\n",
        encoding="utf-8",
    )
    (workspace / ".env").write_text("SECRET_TOKEN=should-never-leak\n", encoding="utf-8")
    (workspace / "api-key.txt").write_text("sk-should-never-leak\n", encoding="utf-8")
    return {"mem": mem, "pages": pages, "workspace": workspace}


def _make_discovery(fixture: dict[str, Path]) -> Discovery:
    return Discovery(
        fixture["mem"],
        task_file=fixture["mem"] / "tasks.md",
        pages_dir=fixture["pages"],
        file_roots={"workspace": str(fixture["workspace"])},
    )


class Test1EnumerateProjects(unittest.TestCase):
    """Test 1 — '불러올 수 있는 모든 프로젝트 알려줘'."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.fixture = _build_fixture(tmp)
        self.discovery = _make_discovery(self.fixture)

    def test_lists_all_projects_with_task_counts(self) -> None:
        projects = self.discovery.list_projects_detailed()
        ids = [project["id"] for project in projects]
        self.assertEqual(ids, ["jarvis-app", "local-jarvis"])
        by_id = {project["id"]: project for project in projects}
        self.assertEqual(by_id["jarvis-app"]["title"], "Electron JARVIS (PiP + Command Center)")
        self.assertEqual(by_id["local-jarvis"]["task_count"], 2)
        self.assertEqual(by_id["local-jarvis"]["done_count"], 1)
        # fabricate 금지 — projects.md에 없는 필드는 없다
        self.assertNotIn("summary", by_id["jarvis-app"])
        self.assertNotIn("status", by_id["jarvis-app"])

    def test_bridge_discover_projects(self) -> None:
        from harness.client import HarnessClient
        from scripts.harness_bridge import BridgeSession, handle_message

        cfg = HarnessConfig.load()
        cfg.memory_dir = self.fixture["mem"]
        cfg.task_file = self.fixture["mem"] / "tasks.md"
        cfg.pages_dir = self.fixture["pages"]
        cfg.file_roots = {"workspace": str(self.fixture["workspace"])}
        cfg.trace_dir = self.fixture["mem"].parent / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)
        client = HarnessClient(cfg)
        response = handle_message(client, BridgeSession(), {"type": "discover_projects", "id": 1})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(
            [project["id"] for project in response["projects"]],
            ["jarvis-app", "local-jarvis"],
        )


class Test2NameLookup(unittest.TestCase):
    """Test 2 — 'JARVIS 프로젝트 찾아줘' → candidate → exact project_id."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.fixture = _build_fixture(tmp)
        self.discovery = _make_discovery(self.fixture)

    def test_search_by_name_returns_canonical_id(self) -> None:
        result = self.discovery.search_projects("JARVIS")
        # 'jarvis'는 jarvis-app(제목)과 local-jarvis(id substring) 둘 다 매치 —
        # 임의 선택 금지: 두 후보를 rank 순으로 모두 반환하는 게 맞다.
        self.assertEqual(result["count"], 2)
        top = result["results"][0]
        self.assertEqual(top["id"], "jarvis-app")  # 제목 매치(rank 3)가 id 부분매치(rank 4)보다 앞
        self.assertEqual(top["matched_on"], "title")
        # canonical identity는 id — matched text와 분리
        self.assertTrue(top["matched_text"])
        self.assertIn(top["id"], {"jarvis-app", "local-jarvis"})

    def test_search_by_id_exact_rank_first(self) -> None:
        result = self.discovery.search_projects("local-jarvis")
        self.assertEqual(result["results"][0]["id"], "local-jarvis")
        self.assertEqual(result["results"][0]["matched_on"], "id")
        self.assertEqual(result["results"][0]["rank"], 0)

    def test_multiple_candidates_preserved_for_clarification(self) -> None:
        # 'app'은 jarvis-app 제목 substring — 후보 목록은 임의 선택하지 않는다
        result = self.discovery.search_projects("app")
        self.assertGreaterEqual(result["count"], 1)
        # 후보들이 score 없이 rank 순 정렬돼 반환된다
        ranks = [item["rank"] for item in result["results"]]
        self.assertEqual(ranks, sorted(ranks))


class Test3TaskContentDiscovery(unittest.TestCase):
    """Test 3 — '보컬 앱 아이디어 작업하던 프로젝트 찾아줘'."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.fixture = _build_fixture(tmp)
        self.discovery = _make_discovery(self.fixture)

    def test_project_found_via_task_content(self) -> None:
        result = self.discovery.search_projects("보컬 앱 아이디어")
        self.assertEqual(result["count"], 1)
        match = result["results"][0]
        self.assertEqual(match["id"], "local-jarvis")
        self.assertEqual(match["matched_on"], "task")
        self.assertEqual(match["matched_text"], "보컬 앱 아이디어 탐색")
        # matched identity와 content 분리 (PART G)
        self.assertEqual(match["type"], "project")

    def test_task_content_via_search_context(self) -> None:
        result = self.discovery.search_context("보컬 앱 아이디어")
        task_matches = [
            item for item in result["results"] if item["type"] == "task"
        ]
        self.assertTrue(task_matches)
        self.assertEqual(task_matches[0]["project_id"], "local-jarvis")
        self.assertEqual(task_matches[0]["id"], "t-704ace97d0ab")


class Test4PageSearch(unittest.TestCase):
    """Test 4 — 'LPIPS' → Page 매치, Project 소속 추론 금지."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.fixture = _build_fixture(tmp)
        self.discovery = _make_discovery(self.fixture)

    def test_page_result_has_no_inferred_project(self) -> None:
        result = self.discovery.search_context("LPIPS")
        page_matches = [
            item for item in result["results"] if item["type"] == "page"
        ]
        self.assertTrue(page_matches)
        page = next(
            item for item in page_matches if item["path"].endswith("lpips-results")
        )
        self.assertEqual(page["id"], "lpips-results")
        self.assertEqual(page["title"], "LPIPS Results")
        self.assertEqual(page["matched_on"], "title")
        # Page↔Project 관계는 canonical metadata에 없다 — 절대 주입 금지
        self.assertNotIn("project_id", page)
        self.assertNotIn("project_title", page)

    def test_task_and_file_match_same_query(self) -> None:
        result = self.discovery.search_context("LPIPS")
        types = {item["type"] for item in result["results"]}
        self.assertIn("task", types)
        self.assertIn("page", types)
        self.assertIn("file", types)


class Test5Files(unittest.TestCase):
    """Test 5 — scratch workspace 파일 구조 + SYSTEM MAP snapshot."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.fixture = _build_fixture(tmp)
        self.discovery = _make_discovery(self.fixture)

    def test_list_files_recursive_structure(self) -> None:
        tree = self.discovery.files.list_tree("workspace")
        paths = [entry["path"] for entry in tree["entries"]]
        self.assertIn("graduation", paths)
        self.assertIn("graduation/experiments", paths)
        self.assertIn("graduation/experiments/lpips-notes.md", paths)
        self.assertIn("graduation/paper.txt", paths)
        self.assertIn("music/post-tonal.md", paths)

    def test_search_files_finds_lpips(self) -> None:
        result = self.discovery.files.search("LPIPS", root="workspace")
        self.assertGreaterEqual(result["count"], 1)
        paths = [item["path"] for item in result["results"]]
        self.assertIn("graduation/experiments/lpips-notes.md", paths)

    def test_read_file_returns_bounded_content(self) -> None:
        content = self.discovery.files.read_text(
            "graduation/experiments/lpips-notes.md", root="workspace"
        )
        self.assertIn("LPIPS", content["content"])
        self.assertLessEqual(content["chars"], 8000)
        self.assertFalse(content["truncated"])

    def test_files_snapshot_reload_stable_no_duplicates(self) -> None:
        from harness.client import HarnessClient
        from scripts.harness_bridge import BridgeSession, handle_message

        cfg = HarnessConfig.load()
        cfg.memory_dir = self.fixture["mem"]
        cfg.task_file = self.fixture["mem"] / "tasks.md"
        cfg.pages_dir = self.fixture["pages"]
        cfg.file_roots = {"workspace": str(self.fixture["workspace"])}
        cfg.trace_dir = self.fixture["mem"].parent / "traces"
        cfg.trace_dir.mkdir(parents=True, exist_ok=True)
        client = HarnessClient(cfg)
        session = BridgeSession()
        first = handle_message(client, session, {"type": "files_snapshot", "id": 1})
        second = handle_message(client, session, {"type": "files_snapshot", "id": 2})
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["sections"], second["sections"])
        paths = [
            entry["path"]
            for section in first["sections"]
            for entry in section["entries"]
        ]
        self.assertEqual(len(paths), len(set(paths)))  # 중복 없음
        self.assertIn("graduation/experiments/lpips-notes.md", paths)


class Test6InvalidPaths(unittest.TestCase):
    """Test 6 — traversal/루트 밖/민감 경로 → 하드 실패, 내용 0 노출."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.fixture = _build_fixture(tmp)
        self.discovery = _make_discovery(self.fixture)

    def test_path_traversal_rejected(self) -> None:
        for bad in ("../outside.txt", "graduation/../../x.md", ".."):
            with self.assertRaises(FileStoreError):
                self.discovery.files.read_text(bad, root="workspace")
            with self.assertRaises(FileStoreError):
                self.discovery.files.list_tree("workspace", bad)

    def test_absolute_and_drive_paths_rejected(self) -> None:
        for bad in ("C:/Windows/win.ini", "/etc/passwd", "D:/secret.txt"):
            with self.assertRaises(FileStoreError):
                self.discovery.files.read_text(bad, root="workspace")

    def test_sensitive_paths_blocked_with_zero_content(self) -> None:
        with self.assertRaises(FileStoreError):
            self.discovery.files.read_text(".env", root="workspace")
        with self.assertRaises(FileStoreError):
            self.discovery.files.read_text("api-key.txt", root="workspace")
        # 검색도 민감 파일 내용을 노출하지 않는다
        for query in ("SECRET_TOKEN", "should-never-leak", "sk-"):
            result = self.discovery.files.search(query, root="workspace")
            self.assertEqual(result["count"], 0, f"민감 내용 노출: {query}")

    def test_unknown_root_rejected(self) -> None:
        with self.assertRaises(FileStoreError):
            self.discovery.files.list_tree("C_System")

    def test_no_roots_means_hard_failure_not_silent_scan(self) -> None:
        empty = Discovery(self.fixture["mem"], file_roots=None)
        with self.assertRaises(FileStoreError):
            empty.files.list_tree(None)


class TestIdentitySeparation(unittest.TestCase):
    """PART G — identity와 matched content 분리 검증."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.fixture = _build_fixture(tmp)
        self.discovery = _make_discovery(self.fixture)

    def test_task_identity_is_id_not_search_text(self) -> None:
        result = self.discovery.search_context("LPIPS")
        task = next(
            item for item in result["results"] if item["type"] == "task"
        )
        self.assertEqual(task["id"], "t-aaaa11111111")
        self.assertEqual(task["matched_on"], "title")

    def test_search_result_serializable(self) -> None:
        result = self.discovery.search_context("LPIPS")
        json.dumps(result, ensure_ascii=False)  # 예외 없이 직렬화 가능


if __name__ == "__main__":
    unittest.main()
