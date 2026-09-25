from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

"""Resume Briefing (M1) 결정적 수용 테스트 — North Star "복귀 → 이어서 시작".

시나리오: 사용자가 "졸업논문 계속하자" 한 문장으로 복귀를 요청하면
프로젝트·진행 중 task·관련 파일·마지막 활동·다음 행동 제안이 한 번에 조립된다.

검증 원칙:
- scratch fixture만 사용한다 — 실제 사용자 memory·파일·trace를 건드리지 않는다.
- 조립 결과는 기존 canonical 원천에서만 오고, 없는 것은 지어내지 않는다.
- resume_briefing은 read 분류 — Permission Gate 없이 실행되고 아무것도 쓰지 않는다.
- next_action는 파생·일시적 표현뿐 — 영속 엔티티를 만들지 않는다(V4 §6).
"""

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.tools import build_default_registry
from harness.tools.file_store import FileStore
from harness.tools.resource_links import ProjectResources, default_links_file
from harness.tools.workspace import WorkspaceManager, default_registry_file
from scripts.harness_bridge import BridgeSession, handle_message


def _build_fixture(tmp: Path) -> dict[str, Path]:
    """projects/tasks/workspace/traces scratch fixture (전체 테스트 공용)."""
    mem = tmp / "mem"
    mem.mkdir()
    (mem / "projects.md").write_text(
        "# Projects\n\n## Active\n\n"
        "- graduation-thesis: 졸업논문 프로젝트\n"
        "- vocal-app: 보컬 앱 아이디어\n",
        encoding="utf-8",
    )
    (mem / "tasks.md").write_text(
        "# Tasks\n\n"
        "## graduation-thesis\n\n"
        "- [ ] t-open1: 실험 결과 정리 — LPIPS 지표 표로 정리\n"
        "- [ ] t-open2: 논문 초안 개요 작성 — 3장 구조 잡기\n"
        "- [x] t-done1: 자료 수집 — 관련논문 5편 정리\n\n"
        "## vocal-app\n\n"
        "- [ ] t-open3: 아이디어 스케치 — 보컬 앱 컨셉 정리\n",
        encoding="utf-8",
    )
    workspace = tmp / "workspace"
    (workspace / "graduation").mkdir(parents=True)
    (workspace / "graduation" / "paper.txt").write_text(
        "thesis draft text\n", encoding="utf-8"
    )
    (workspace / "graduation" / "notes.md").write_text(
        "LPIPS experiment notes\n", encoding="utf-8"
    )
    traces = tmp / "traces"
    traces.mkdir()
    return {"mem": mem, "workspace": workspace, "traces": traces}


def _write_trace(
    traces: Path,
    trace_id: str,
    project_id: str,
    ts: str,
    content: str,
    tool_names: list[str],
) -> None:
    """bridge trace 규약(request.metadata.project_id)에 맞춘 가짜 trace 1건."""
    entry = {
        "trace_id": trace_id,
        "ts": ts,
        "request": {
            "messages": [],
            "tools": [],
            "context_keys": [],
            "metadata": {"source": "electron_bridge", "project_id": project_id},
        },
        "response": {
            "content": content,
            "tool_calls": [],
            "finish_reason": "stop",
        },
        "error": None,
        "latency_ms": {"e2e": 10},
        "tool_results": [
            {
                "call": {"id": f"call-{name}", "name": name, "arguments": {}},
                "result": {
                    "ok": True,
                    "data": {},
                    "error": None,
                    "requires_confirmation": False,
                },
            }
            for name in tool_names
        ],
        "turns": 1,
    }
    (traces / f"{trace_id}.json").write_text(
        json.dumps(entry, ensure_ascii=False), encoding="utf-8"
    )


def _make_registry(fixture: dict[str, Path]):
    return build_default_registry(
        memory_dir=fixture["mem"],
        task_file=fixture["mem"] / "tasks.md",
        file_roots={"workspace": str(fixture["workspace"])},
        trace_dir=fixture["traces"],
    )


class ResumeBriefingAssemblyTests(unittest.TestCase):
    """North Star 시나리오 — '졸업논문 계속하자' 한 발화로 복귀 브리핑 조립."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _build_fixture(Path(self.tmp.name))
        self.registry = _make_registry(self.fixture)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _brief(self, arguments: dict) -> dict:
        result = self.registry.execute("resume_briefing", arguments)
        self.assertTrue(result.ok, result.error)
        return result.data

    def test_resolves_project_from_query_and_assembles(self) -> None:
        """A — '졸업논문 계속하자' → 프로젝트 확정 + task/counts/next_action."""
        data = self._brief({"query": "졸업논문"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["project"]["id"], "graduation-thesis")
        self.assertTrue(data["project"]["resolved_by"].startswith("query:"))
        self.assertEqual(data["tasks"]["open_count"], 2)
        self.assertEqual(data["tasks"]["completed_count"], 1)
        self.assertEqual(data["tasks"]["total_count"], 3)
        self.assertEqual(
            [task["id"] for task in data["tasks"]["open"]],
            ["t-open1", "t-open2"],
        )
        self.assertEqual(data["tasks"]["completed"][0]["id"], "t-done1")

    def test_next_action_is_derived_first_open_task(self) -> None:
        """B — next_action는 파생 표현: 목록 순서 첫 미완료 task + 근거 명시."""
        data = self._brief({"query": "졸업논문"})
        next_action = data["next_action"]
        self.assertIsNotNone(next_action)
        self.assertEqual(next_action["task_id"], "t-open1")
        self.assertEqual(next_action["title"], "실험 결과 정리")
        self.assertEqual(next_action["reason"], "LPIPS 지표 표로 정리")
        self.assertIn("첫 번째", next_action["basis"])

    def test_explicit_project_id_wins_over_query(self) -> None:
        """F — 명시적 project_id가 추정 검색(query)을 이긴다."""
        data = self._brief({"query": "보컬", "project_id": "graduation-thesis"})
        self.assertEqual(data["project"]["id"], "graduation-thesis")
        self.assertEqual(data["project"]["resolved_by"], "project_id")

    def test_unknown_query_is_unresolved_not_fabricated(self) -> None:
        """C — 모르는 프로젝트는 확정하지 않는다. task/next_action를 지어내지 않는다."""
        data = self._brief({"query": "존재하지않는프로젝트zzz"})
        self.assertEqual(data["status"], "unresolved")
        self.assertNotIn("next_action", data)
        self.assertNotIn("tasks", data)
        self.assertIn("graduation-thesis", [p["id"] for p in data["projects"]])

    def test_unknown_project_id_lists_projects(self) -> None:
        data = self._brief({"project_id": "no-such-project"})
        self.assertEqual(data["status"], "unresolved")
        self.assertIn("vocal-app", [p["id"] for p in data["projects"]])

    def test_requires_query_or_project_id(self) -> None:
        result = self.registry.execute("resume_briefing", {})
        self.assertFalse(result.ok)
        self.assertIn("query", result.error)

    def test_read_only_no_writes_and_gate_skipped(self) -> None:
        """read 분류 — Permission Gate 없이 실행되고 canonical 원천을 쓰지 않는다.

        FileRef 레지스트리(file_refs.json)는 스캔으로 재구성 가능한 파생 상태라
        읽기 중 갱신될 수 있다(§3·§16). canonical 파일은 그대로여야 한다.
        """
        self.assertEqual(self.registry.classify("resume_briefing"), "read")
        mem = self.fixture["mem"]
        workspace = self.fixture["workspace"]
        before_tasks = (mem / "tasks.md").read_text(encoding="utf-8")
        before_projects = (mem / "projects.md").read_text(encoding="utf-8")
        before_links = (mem / "resource_links.json").read_text(encoding="utf-8") \
            if (mem / "resource_links.json").exists() else None
        before_paper = (workspace / "graduation" / "paper.txt").read_text(encoding="utf-8")
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertTrue(result.ok)
        self.assertFalse(result.requires_confirmation)
        self.assertEqual((mem / "tasks.md").read_text(encoding="utf-8"), before_tasks)
        self.assertEqual((mem / "projects.md").read_text(encoding="utf-8"), before_projects)
        after_links = (mem / "resource_links.json").read_text(encoding="utf-8") \
            if (mem / "resource_links.json").exists() else None
        self.assertEqual(after_links, before_links)
        self.assertEqual(
            (workspace / "graduation" / "paper.txt").read_text(encoding="utf-8"),
            before_paper,
        )


class ResumeBriefingResourceTests(unittest.TestCase):
    """관련 자료 — persisted ResourceLink → 현재 locator resolve."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _build_fixture(Path(self.tmp.name))
        mem = self.fixture["mem"]
        # 실제 조립 계층과 동일한 FileRef 스캔 + 명시적 링크 생성
        files = FileStore({"workspace": str(self.fixture["workspace"])})
        workspace = WorkspaceManager(files, default_registry_file(mem))
        workspace.scan_root()
        ref_paper = workspace.registry.by_path("workspace", "graduation/paper.txt")
        ref_notes = workspace.registry.by_path("workspace", "graduation/notes.md")
        self.assertIsNotNone(ref_paper)
        self.assertIsNotNone(ref_notes)
        resources = ProjectResources(default_links_file(mem), mem, workspace=workspace)
        resources.link_project_file("graduation-thesis", ref_paper.id, "reference")
        resources.link_task_file("t-open1", ref_notes.id, "result")
        self.registry = _make_registry(self.fixture)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resources_resolved_from_links(self) -> None:
        """D — 프로젝트 링크 + task 링크가 relation과 현재 경로로 실린다."""
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertTrue(result.ok, result.error)
        resources = result.data["resources"]
        self.assertEqual(len(resources), 2)
        project_entry = next(e for e in resources if e["source"] == "project")
        task_entry = next(e for e in resources if e["source"] == "task")
        self.assertEqual(project_entry["relation"], "reference")
        self.assertEqual(project_entry["file"]["path"], "graduation/paper.txt")
        self.assertEqual(task_entry["task_id"], "t-open1")
        self.assertEqual(task_entry["relation"], "result")
        self.assertEqual(task_entry["file"]["path"], "graduation/notes.md")
        self.assertEqual(task_entry["file"]["status"], "ok")

    def test_next_action_carries_task_resources(self) -> None:
        """E — 다음 행동 제안이 그 task에 연결된 자료를 함께 실어 나른다."""
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        next_action = result.data["next_action"]
        self.assertEqual(next_action["task_id"], "t-open1")
        self.assertEqual(len(next_action["resources"]), 1)
        self.assertEqual(
            next_action["resources"][0]["file"]["path"], "graduation/notes.md"
        )

    def test_no_links_notes_honest_gap(self) -> None:
        """F — 링크가 없으면 자료를 지어내지 않고 note로 남긴다 (vocal-app)."""
        result = self.registry.execute("resume_briefing", {"query": "보컬"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["resources"], [])
        self.assertTrue(
            any("연결된 파일" in note for note in result.data["notes"])
        )


class ResumeBriefingActivityTests(unittest.TestCase):
    """마지막 활동 — 이 프로젝트 trace만, 기록이 없으면 지어내지 않는다."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _build_fixture(Path(self.tmp.name))
        self.registry = _make_registry(self.fixture)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_last_activity_from_project_traces_only(self) -> None:
        """G — 프로젝트 trace는 요약과 tool 사용을 실고, 다른 프로젝트 trace는 제외."""
        traces = self.fixture["traces"]
        long_content = "이전 세션에서 실험 결과를 정리했습니다. " * 12
        _write_trace(
            traces, "trace-1", "graduation-thesis",
            "2026-09-20T10:00:00+00:00", long_content, ["read_file"],
        )
        _write_trace(
            traces, "trace-2", "vocal-app",
            "2026-09-21T09:00:00+00:00", "보컬 앱 아이디어 메모.", [],
        )
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        activity = result.data["last_activity"]
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["tools_used"], ["read_file"])
        self.assertIn("실험 결과", activity[0]["summary"])
        # 발췌는 명시적으로 절단된다
        self.assertTrue(activity[0]["summary"].endswith("…"))
        self.assertLessEqual(len(activity[0]["summary"]), 241)

    def test_latest_activity_first(self) -> None:
        traces = self.fixture["traces"]
        _write_trace(
            traces, "trace-a", "graduation-thesis",
            "2026-09-19T10:00:00+00:00", "오래된 세션.", [],
        )
        _write_trace(
            traces, "trace-b", "graduation-thesis",
            "2026-09-22T10:00:00+00:00", "가장 최근 세션.", [],
        )
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        activity = result.data["last_activity"]
        self.assertEqual(activity[0]["summary"], "가장 최근 세션.")
        self.assertEqual(activity[1]["summary"], "오래된 세션.")

    def test_no_traces_no_fabrication(self) -> None:
        """H — 기록된 활동이 없으면 빈 목록 + note. 지어내지 않는다."""
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertEqual(result.data["last_activity"], [])
        self.assertTrue(
            any("최근 활동" in note for note in result.data["notes"])
        )

    def test_completed_tasks_note_when_nothing_open(self) -> None:
        """I — 미완료 task가 없으면 next_action 없음 + 명시적 note."""
        mem = self.fixture["mem"]
        (mem / "tasks.md").write_text(
            "# Tasks\n\n## graduation-thesis\n\n"
            "- [x] t-done1: 자료 수집 — 관련논문 5편 정리\n",
            encoding="utf-8",
        )
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertTrue(result.ok, result.error)
        self.assertIsNone(result.data["next_action"])
        self.assertEqual(result.data["tasks"]["open_count"], 0)
        self.assertTrue(
            any("미완료 task가 없습니다" in note for note in result.data["notes"])
        )


class ResumeBriefingLinkAccuracyTests(unittest.TestCase):
    """M2 — 자료 연결 정확도: 명시적 Task↔File 링크가 복귀 브리핑에 실린다.

    경로: 사용자 UI 행동 → bridge link_task_file → canonical ResourceLink
    → resume_briefing이 "그 작업의 관련 파일"로 귀속해 반환한다.
    연결되지 않은 파일은 절대 뜨지 않는다 — workspace 전체 노출 없음.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _build_fixture(Path(self.tmp.name))
        mem = self.fixture["mem"]
        # 브리지와 동일한 FileRef identity 계층 스캔
        files = FileStore({"workspace": str(self.fixture["workspace"])})
        self.workspace = WorkspaceManager(files, default_registry_file(mem))
        self.workspace.scan_root()
        self.ref_notes = self.workspace.registry.by_path(
            "workspace", "graduation/notes.md"
        )
        self.ref_paper = self.workspace.registry.by_path(
            "workspace", "graduation/paper.txt"
        )
        self.assertIsNotNone(self.ref_notes)
        self.assertIsNotNone(self.ref_paper)
        cfg = HarnessConfig(
            runtime="mock",
            trace_dir=self.fixture["traces"],
            memory_dir=mem,
        )
        cfg.task_file = mem / "tasks.md"
        cfg.file_roots = {"workspace": str(self.fixture["workspace"])}
        self.client = HarnessClient(cfg)
        self.registry = _make_registry(self.fixture)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _send(self, payload: dict) -> dict:
        return handle_message(self.client, BridgeSession(), payload)

    def _brief(self) -> dict:
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertTrue(result.ok, result.error)
        return result.data

    def test_link_task_file_surfaces_in_briefing_and_next_action(self) -> None:
        """J — 링크 1개: task 귀속으로 실리고, 미연결 파일은 뜨지 않는다."""
        response = self._send({
            "type": "link_task_file", "id": 1,
            "task_id": "t-open1", "file_id": self.ref_notes.id,
            "relation": "result",
        })
        self.assertEqual(response["status"], "ok", response)
        self.assertTrue(response["created"])

        data = self._brief()
        resources = data["resources"]
        self.assertEqual(len(resources), 1)
        entry = resources[0]
        self.assertEqual(entry["source"], "task")
        self.assertEqual(entry["task_id"], "t-open1")
        self.assertEqual(entry["relation"], "result")
        self.assertEqual(entry["file"]["path"], "graduation/notes.md")

        # 다음 행동(첫 미완료 = t-open1)의 자료로 실린다
        self.assertEqual(data["next_action"]["task_id"], "t-open1")
        self.assertEqual(len(data["next_action"]["resources"]), 1)
        # task별 귀속 — 조회 대상 task에 files 필드, 실제 연결만
        open_by_id = {t["id"]: t for t in data["tasks"]["open"]}
        self.assertEqual(
            [f["path"] for f in open_by_id["t-open1"]["files"]],
            ["graduation/notes.md"],
        )
        self.assertEqual(open_by_id["t-open2"]["files"], [])
        # 미연결 파일(paper.txt)은 어디에도 뜨지 않는다
        all_paths = [e["file"]["path"] for e in resources]
        self.assertNotIn("graduation/paper.txt", all_paths)

    def test_link_is_idempotent_and_unlink_removes(self) -> None:
        """K — 중복 링크 금지(멱등), unlink 시 브리핑에서도 사라진다."""
        first = self._send({
            "type": "link_task_file", "id": 1,
            "task_id": "t-open1", "file_id": self.ref_notes.id,
            "relation": "reference",
        })
        self.assertTrue(first["created"])
        second = self._send({
            "type": "link_task_file", "id": 2,
            "task_id": "t-open1", "file_id": self.ref_notes.id,
            "relation": "reference",
        })
        self.assertFalse(second["created"])
        self.assertEqual(len(self._brief()["resources"]), 1)

        removed = self._send({
            "type": "unlink_task_file", "id": 3,
            "link_id": first["link"]["id"],
        })
        self.assertEqual(removed["status"], "ok", removed)
        data = self._brief()
        self.assertEqual(data["resources"], [])
        open_by_id = {t["id"]: t for t in data["tasks"]["open"]}
        self.assertEqual(open_by_id["t-open1"]["files"], [])

    def test_project_and_task_links_keep_attribution(self) -> None:
        """L — 프로젝트 링크와 task 링크가 구분돼 귀속되고 섞이지 않는다."""
        self._send({
            "type": "link_project_file", "id": 1,
            "project_id": "graduation-thesis", "file_id": self.ref_paper.id,
            "relation": "reference",
        })
        self._send({
            "type": "link_task_file", "id": 2,
            "task_id": "t-open1", "file_id": self.ref_notes.id,
            "relation": "result",
        })
        data = self._brief()
        self.assertEqual(len(data["resources"]), 2)
        project_entry = next(e for e in data["resources"] if e["source"] == "project")
        task_entry = next(e for e in data["resources"] if e["source"] == "task")
        self.assertIsNone(project_entry["task_id"])
        self.assertEqual(project_entry["file"]["path"], "graduation/paper.txt")


class ResumeBriefingNextActionQualityTests(unittest.TestCase):
    """M4: 세션 히스토리 기반 다음 행동(next_action) 품질 검증."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _build_fixture(Path(self.tmp.name))
        self.registry = _make_registry(self.fixture)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_next_action_prioritizes_touched_task_from_recent_session(self) -> None:
        """최근 세션에서 직접 다룬 task가 목록의 첫 번째가 아니더라도 우선 추천된다."""
        traces = self.fixture["traces"]
        # t-open1이 목록 첫 번째이지만, 최근 세션에서는 t-open2('초안 개요 작성')를 다룸
        trace_entry = {
            "trace_id": "trace-t2",
            "ts": "2026-09-24T12:00:00+00:00",
            "request": {
                "metadata": {"project_id": "graduation-thesis"},
                "messages": [{"role": "user", "content": "t-open2 작업 상황을 기록해줘"}],
            },
            "tool_results": [
                {
                    "call": {
                        "name": "update_task",
                        "arguments": {"task_id": "t-open2", "project_id": "graduation-thesis"},
                    }
                }
            ],
            "response": {"content": "t-open2 태스크를 업데이트했습니다."},
        }
        (traces / "trace_t2.json").write_text(json.dumps(trace_entry), encoding="utf-8")

        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertTrue(result.ok, result.error)
        next_action = result.data["next_action"]
        self.assertEqual(next_action["task_id"], "t-open2")
        self.assertEqual(next_action["basis"], "최근 세션에서 진행 중이던 작업")

    def test_next_action_prioritizes_task_linked_to_touched_file(self) -> None:
        """최근 세션에서 특정 파일을 열람/작업했다면 해당 파일과 연결된 task를 우선 추천한다."""
        traces = self.fixture["traces"]
        mem = self.fixture["mem"]
        ws = self.fixture["workspace"]
        files = FileStore({"workspace": str(ws)})
        workspace = WorkspaceManager(files, default_registry_file(mem))
        workspace.scan_root()
        ref_paper = workspace.registry.by_path("workspace", "graduation/paper.txt")
        self.assertIsNotNone(ref_paper)

        resources = ProjectResources(default_links_file(mem), mem, workspace=workspace)
        # t-open2에 graduation/paper.txt 연결
        resources.link_task_file("t-open2", ref_paper.id, "reference")

        trace_entry = {
            "trace_id": "trace-f",
            "ts": "2026-09-24T12:00:00+00:00",
            "request": {
                "metadata": {
                    "project_id": "graduation-thesis",
                    "active_file": {"name": "paper.txt", "path": "graduation/paper.txt"},
                },
                "messages": [{"role": "user", "content": "paper.txt 내용 요약해줘"}],
            },
            "tool_results": [
                {
                    "call": {
                        "name": "read_file",
                        "arguments": {"path": "graduation/paper.txt"},
                    }
                }
            ],
            "response": {"content": "paper.txt 요약 완료"},
        }
        (traces / "trace_f.json").write_text(json.dumps(trace_entry), encoding="utf-8")

        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertTrue(result.ok, result.error)
        next_action = result.data["next_action"]
        self.assertEqual(next_action["task_id"], "t-open2")
        self.assertIn("최근 세션 작업 파일", next_action["basis"])

    def test_next_action_fallback_to_first_open_task_when_no_session_signals(self) -> None:
        """세션 신호가 없을 때는 첫 번째 미완료 task로 안전하게 폴백한다."""
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertTrue(result.ok, result.error)
        next_action = result.data["next_action"]
        self.assertEqual(next_action["task_id"], "t-open1")
        self.assertEqual(next_action["basis"], "미완료 task 중 목록 순서 첫 번째")


class ResumeBriefingLiveSessionSignalTests(unittest.TestCase):
    """M5: 실제 bridge 작업이 만든 trace로 복귀 브리핑이 이어지는지 검증.

    M1–M4 테스트는 trace를 손으로 만들어 넣었다. 실제로는 task 생성/수정/완료와
    link가 모델 tool loop를 거치지 않아 trace에 남지 않았고, 그래서
    touched_task_ids가 항상 비어 있었다. 이 테스트는 fixture로 trace를 주입하지
    않고 실제 bridge 핸들러를 호출한 뒤, 같은 trace 디렉터리에서 resume_briefing이
    그 신호를 읽는지 확인한다.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _build_fixture(Path(self.tmp.name))
        self.session = BridgeSession()
        config = HarnessConfig(
            runtime="mock",
            memory_dir=self.fixture["mem"],
            task_file=self.fixture["mem"] / "tasks.md",
            trace_dir=self.fixture["traces"],
            file_roots={"workspace": str(self.fixture["workspace"])},
            trace_enabled=True,
        )
        self.client = HarnessClient(config)
        self.registry = _make_registry(self.fixture)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _handle(self, msg: dict) -> dict:
        return handle_message(self.client, self.session, msg)

    def _brief(self) -> dict:
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertTrue(result.ok, result.error)
        return result.data

    def test_update_task_records_trace_and_drives_next_action(self) -> None:
        """실제 update_task 호출 → trace 생성 → 복귀 시 그 task가 이어진다."""
        # t-open1이 목록 첫 번째지만 사용자는 t-open2를 마지막으로 다뤘다.
        response = self._handle({
            "type": "update_task",
            "id": "r1",
            "project_id": "graduation-thesis",
            "task_id": "t-open2",
            "title": "논문 초안 개요 작성 — 3장 구조 잡기",
        })
        self.assertEqual(response["status"], "ok")

        traces = list(self.fixture["traces"].glob("*.json"))
        self.assertEqual(len(traces), 1, "update_task이 trace를 남겨야 한다")
        entry = json.loads(traces[0].read_text(encoding="utf-8"))
        self.assertEqual(
            entry["request"]["metadata"]["project_id"], "graduation-thesis"
        )
        self.assertEqual(
            entry["tool_results"][0]["call"]["arguments"]["task_id"], "t-open2"
        )

        data = self._brief()
        self.assertEqual(data["next_action"]["task_id"], "t-open2")
        self.assertEqual(data["next_action"]["basis"], "최근 세션에서 진행 중이던 작업")

    def test_create_task_records_new_task_id_for_next_resume(self) -> None:
        """실제 create_task는 생성된 task_id를 남겨 방금 만든 작업이 이어진다."""
        response = self._handle({
            "type": "create_task",
            "id": "r2",
            "project_id": "graduation-thesis",
            "title": "표지 페이지 작성",
        })
        self.assertEqual(response["status"], "ok")
        new_id = response["task"]["id"]

        data = self._brief()
        self.assertEqual(data["next_action"]["task_id"], new_id)
        self.assertEqual(data["next_action"]["title"], "표지 페이지 작성")

    def test_completing_a_task_does_not_propose_it_as_next_action(self) -> None:
        """완료한 작업은 미완료 목록에 없으므로 다음 행동으로 제안되지 않는다."""
        self._handle({
            "type": "update_task",
            "id": "r3",
            "project_id": "graduation-thesis",
            "task_id": "t-open1",
            "done": True,
        })
        data = self._brief()
        self.assertNotEqual(data["next_action"]["task_id"], "t-open1")
        self.assertEqual(data["next_action"]["task_id"], "t-open2")

    def test_activity_trace_is_not_reported_as_user_intent(self) -> None:
        """시스템 작업 기록은 최근 활동으로 보이지만 사용자 발화로 승격되지 않는다."""
        self._handle({
            "type": "create_task",
            "id": "r4",
            "project_id": "graduation-thesis",
            "title": "결론 장 개요",
        })
        data = self._brief()
        self.assertTrue(data["last_activity"])
        self.assertEqual(data["last_activity"][0]["kind"], "system")
        self.assertNotIn("결론 장 개요", (data["notes"] or []))

    def test_link_task_file_records_file_signal_for_real_link(self) -> None:
        """실제 link_task_file이 파일 경로까지 남겨 파일 연결 task로 이어진다."""
        mem = self.fixture["mem"]
        workspace = WorkspaceManager(
            FileStore({"workspace": str(self.fixture["workspace"])}),
            default_registry_file(mem),
        )
        workspace.scan_root()
        ref = workspace.registry.by_path("workspace", "graduation/paper.txt")
        self.assertIsNotNone(ref)

        response = self._handle({
            "type": "link_task_file",
            "id": "r5",
            "task_id": "t-open2",
            "file_id": ref.id,
            "relation": "reference",
        })
        self.assertEqual(response["status"], "ok")

        # link는 task_id를 함께 남기므로 직접 다룬 task로 우선 매칭된다.
        # 파일 신호까지 남았는지도 확인한다 — 그래야 파일 기반 매칭 경로가
        # 실제 런타임에 존재한다.
        entry = json.loads(
            next(self.fixture["traces"].glob("*.json")).read_text(encoding="utf-8")
        )
        arguments = entry["tool_results"][0]["call"]["arguments"]
        self.assertEqual(arguments["path"], "graduation/paper.txt")
        self.assertEqual(arguments["name"], "paper.txt")
        self.assertEqual(
            entry["request"]["metadata"]["project_id"], "graduation-thesis"
        )

        data = self._brief()
        self.assertEqual(data["next_action"]["task_id"], "t-open2")
        self.assertEqual(
            next(
                (r["file"]["name"] for r in data["next_action"]["resources"]),
                None,
            ),
            "paper.txt",
        )

    def test_active_file_context_is_recorded_when_bridge_sends_it(self) -> None:
        """렌더러가 보낸 active_file이 있으면 그 파일도 세션 신호로 남는다."""
        self._handle({
            "type": "update_task",
            "id": "r6",
            "project_id": "graduation-thesis",
            "task_id": "t-open2",
            "done": False,
            "active_file": {
                "root_id": "workspace",
                "path": "graduation/paper.txt",
                "name": "paper.txt",
                # 이 필드는 신뢰하지 않으며 기록되지 않아야 한다.
                "content": "secret file body",
            },
        })
        entry = json.loads(
            next(self.fixture["traces"].glob("*.json")).read_text(encoding="utf-8")
        )
        active_file = entry["request"]["metadata"]["active_file"]
        self.assertEqual(active_file["path"], "graduation/paper.txt")
        self.assertEqual(active_file["name"], "paper.txt")
        self.assertNotIn("content", active_file)

    def test_unresolvable_link_does_not_match_every_touched_file(self) -> None:
        """파일명이 비어 있는 링크는 어떤 touched file과도 매칭되지 않는다 (M5 수정).

        이전 구현은 빈 fpath에 대해 endswith('')가 항상 참이어서, resolve 실패한
        링크 하나만 있어도 임의의 파일이 그 task로 매칭되었다.
        """
        from harness.tools.resume_briefing import _file_matches

        self.assertFalse(_file_matches("src/anything.py", "", ""))
        self.assertFalse(_file_matches("", "paper.txt", "graduation/paper.txt"))
        # 실제로 같은 파일인 경우는 계속 매칭된다.
        self.assertTrue(
            _file_matches("graduation/paper.txt", "paper.txt", "graduation/paper.txt")
        )
        self.assertTrue(
            _file_matches("C:/ws/graduation/paper.txt", "paper.txt", "graduation/paper.txt")
        )
        # 다른 파일은 매칭되지 않는다.
        self.assertFalse(
            _file_matches("graduation/notes.md", "paper.txt", "graduation/paper.txt")
        )
