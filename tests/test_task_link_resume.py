from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

"""Milestone B — 생성 직후 관련 자료까지 이어지는 작업 복귀 (결정적 e2e).

사용자 outcome: "사용자가 작업 중 Task를 새로 만들었을 때, 그 순간 보고 있던
relevant file을 명시적으로 연결할 수 있고, 나중에 '계속하자'로 복귀했을 때
그 Task와 실제 관련 파일이 함께 복원된다."

이 테스트는 그 경로를 canonical 경로만으로 관통한다:

    chat(모델 제안) → confirm(실제 TaskStore 생성) → link_task_file(실제
    ResourceLink) → resume_briefing(복귀 조립) → unlink_task_file(해제)

모델 응답만 스크립트하고, tool 실행·검증·gate·trace·링크·복귀 조립은 실제
코드를 쓴다. 자동 linking이 없다는 것도 함께 검증한다: 명시적 link 전에는
복귀 브리핑에 파일이 나오지 않는다.
"""

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse, ToolCall
from harness.tools import build_default_registry
from harness.tools.file_store import FileStore
from harness.tools.workspace import WorkspaceManager, default_registry_file
from scripts.harness_bridge import BridgeSession, handle_message


class _TaskProposalAdapter:
    """create_task를 제안한 뒤, 승인된 실행 뒤에는 텍스트로 답하는 scripted fake.

    브리지 렌더러가 쓰는 것과 같은 events를 만들어 내야 하므로 실제
    HarnessClient 루프(registry·gate·trace)를 그대로 태운다.
    """

    def __init__(self, title: str, reason: str) -> None:
        self._proposal = {"project_id": "graduation-thesis", "title": title, "reason": reason}
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        saw_tool_result = any(
            message.get("role") == "tool" for message in request.messages
        )
        if saw_tool_result:
            return ChatResponse(
                content="작업을 만들었어.",
                finish_reason="stop",
                trace_id=request.trace_id,
            )
        return ChatResponse(
            content="",
            tool_calls=[
                ToolCall(name="create_task", arguments=dict(self._proposal), id="call-0")
            ],
            finish_reason="stop",
            trace_id=request.trace_id,
        )


def _build_fixture(tmp: Path) -> dict[str, Path]:
    mem = tmp / "mem"
    mem.mkdir()
    (mem / "projects.md").write_text(
        "# Projects\n\n## Active\n\n"
        "- graduation-thesis: 졸업논문 프로젝트\n",
        encoding="utf-8",
    )
    (mem / "tasks.md").write_text(
        "# Tasks\n\n"
        "## graduation-thesis\n\n"
        "- [ ] t-open1: 실험 결과 정리 — LPIPS 지표 표로 정리\n",
        encoding="utf-8",
    )
    workspace = tmp / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("LPIPS 실험 메모\n", encoding="utf-8")
    (workspace / "unrelated.md").write_text("다른 주제의 메모\n", encoding="utf-8")
    traces = tmp / "traces"
    traces.mkdir()
    return {"mem": mem, "workspace": workspace, "traces": traces}


class TaskLinkResumeE2ETests(unittest.TestCase):
    """생성 → 명시적 link → 복귀 복원 → 해제 (end-to-end)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _build_fixture(Path(self.tmp.name))
        self.config = HarnessConfig(
            runtime="mock",
            memory_dir=self.fixture["mem"],
            task_file=self.fixture["mem"] / "tasks.md",
            trace_dir=self.fixture["traces"],
            file_roots={"workspace": str(self.fixture["workspace"])},
            trace_enabled=True,
        )
        self.adapter = _TaskProposalAdapter(
            "표지 페이지 작성", "논문 제출용 표지를 만들어야 한다"
        )
        self.registry = build_default_registry(
            memory_dir=self.config.memory_dir,
            task_file=self.config.task_file,
            file_roots=self.config.file_roots,
            # resume_briefing도 같은 trace 디렉터리를 읽어야 한다 — 실제
            # bridge처럼 client와 registry가 동일한 config를 쓴다.
            trace_dir=self.config.trace_dir,
        )
        self.client = HarnessClient(
            self.config, adapter=self.adapter, tools=self.registry
        )
        self.session = BridgeSession()
        # FileRef identity — 브리지와 동일한 스캔 계층으로 만든다.
        self.workspace_manager = WorkspaceManager(
            FileStore({"workspace": str(self.fixture["workspace"])}),
            default_registry_file(self.fixture["mem"]),
        )
        self.workspace_manager.scan_root()
        self.notes_ref = self.workspace_manager.registry.by_path("workspace", "notes.md")
        self.other_ref = self.workspace_manager.registry.by_path("workspace", "unrelated.md")
        self.assertIsNotNone(self.notes_ref)
        self.assertIsNotNone(self.other_ref)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _handle(self, msg: dict) -> dict:
        return handle_message(self.client, self.session, msg)

    def _brief(self) -> dict:
        result = self.registry.execute("resume_briefing", {"query": "졸업논문"})
        self.assertTrue(result.ok, result.error)
        return result.data

    def _create_via_model(self) -> str:
        """자연어 요청 → 한 턴 제안 → 승인 → 실제 생성. 생성된 id를 돌려준다."""
        proposed = self._handle({
            "type": "chat",
            "id": "m1",
            "text": "할 일 하나만 추가해줘. 제목은 '표지 페이지 작성'이야.",
            "project_id": "graduation-thesis",
        })
        self.assertEqual(proposed["status"], "awaiting_confirmation", proposed)
        self.assertEqual(proposed["tool_call"]["name"], "create_task")

        approved = self._handle({
            "type": "confirm",
            "id": "m2",
            "tool_call": proposed["tool_call"],
        })
        self.assertEqual(approved["status"], "final", approved)

        # 렌더러가 쓰는 것과 같은 경로: 성공한 create_task 실행의 data에서 id.
        created = None
        for event in approved.get("events") or []:
            if (
                event.get("kind") == "tool"
                and event.get("name") == "create_task"
                and event.get("ok") is True
                and not event.get("requires_confirmation")
            ):
                created = (event.get("data") or {}).get("id")
        self.assertTrue(created, f"생성된 task id가 events에 없다: {approved.get('events')}")
        return created

    def test_created_task_has_no_resource_until_user_links(self) -> None:
        """자동 linking 없음 — 명시적 link 전에는 복귀 브리핑에 파일이 없다."""
        task_id = self._create_via_model()
        data = self._brief()
        self.assertEqual(data["next_action"]["task_id"], task_id)
        self.assertEqual(data["next_action"]["resources"], [])
        self.assertEqual(data["resources"], [])

    def test_explicit_link_surfaces_file_in_resume(self) -> None:
        """사용자 명시적 link → canonical ResourceLink → 복귀 시 그 파일이 실린다."""
        task_id = self._create_via_model()
        linked = self._handle({
            "type": "link_task_file",
            "id": "l1",
            "task_id": task_id,
            "file_id": self.notes_ref.id,
            "relation": "result",
        })
        self.assertEqual(linked["status"], "ok", linked)
        self.assertTrue(linked["created"])

        data = self._brief()
        self.assertEqual(data["next_action"]["task_id"], task_id)
        paths = [
            entry["file"]["path"]
            for entry in data["next_action"]["resources"]
        ]
        self.assertEqual(paths, ["notes.md"])
        self.assertEqual(data["next_action"]["resources"][0]["relation"], "result")

        # 연결하지 않은 파일은 어디에도 뜨지 않는다.
        all_paths = [entry["file"]["path"] for entry in data["resources"]]
        self.assertNotIn("unrelated.md", all_paths)
        open_by_id = {t["id"]: t for t in data["tasks"]["open"]}
        self.assertEqual(
            [f["path"] for f in open_by_id[task_id]["files"]],
            ["notes.md"],
        )

    def test_second_link_and_unlink_behaviour(self) -> None:
        """멱등 링크, 다른 파일 추가, 해제 후 복귀 상태."""
        task_id = self._create_via_model()
        first = self._handle({
            "type": "link_task_file", "id": "l1",
            "task_id": task_id, "file_id": self.notes_ref.id,
        })
        again = self._handle({
            "type": "link_task_file", "id": "l2",
            "task_id": task_id, "file_id": self.notes_ref.id,
        })
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])

        listed = self._handle({
            "type": "list_task_resources", "id": "l3", "task_id": task_id,
        })
        self.assertEqual(len(listed["resources"]), 1)

        removed = self._handle({
            "type": "unlink_task_file", "id": "l4", "link_id": first["link"]["id"],
        })
        self.assertEqual(removed["status"], "ok", removed)
        self.assertTrue(removed["removed"])

        data = self._brief()
        self.assertEqual(data["next_action"]["resources"], [])
        self.assertEqual(data["resources"], [])

    def test_link_requires_file_identity_not_path(self) -> None:
        """근거 없는 제안 금지 — 경로는 link 대상이 되지 않는다."""
        task_id = self._create_via_model()
        response = self._handle({
            "type": "link_task_file", "id": "l1",
            "task_id": task_id, "file_id": "workspace/notes.md",
        })
        self.assertEqual(response["status"], "error")
        self.assertIn("file_id", response["error"])

    def test_created_task_beyond_lookup_window_still_carries_its_file(self) -> None:
        """목록 뒤쪽에 생긴 task가 추천될 때 자기 자료가 비어 있으면 안 된다.

        라이브 검증에서 발견된 갭: 리소스를 미완료 목록 앞 N개에서만 모으는
        반면 추천은 "최근 다룬 task"를 고를 수 있어서, 방금 만든 task(항상
        목록 끝)가 자기 파일을 잃고 복귀했다.
        """
        tasks = self.fixture["mem"] / "tasks.md"
        existing = "".join(
            f"- [ ] t-bulk{i}: 기존 작업 {i} — filler\n" for i in range(1, 7)
        )
        tasks.write_text(
            "# Tasks\n\n## graduation-thesis\n\n" + existing,
            encoding="utf-8",
        )

        task_id = self._create_via_model()
        linked = self._handle({
            "type": "link_task_file", "id": "l1",
            "task_id": task_id, "file_id": self.notes_ref.id,
        })
        self.assertTrue(linked["created"], linked)

        data = self._brief()
        self.assertEqual(data["next_action"]["task_id"], task_id)
        self.assertEqual(
            [entry["file"]["path"] for entry in data["next_action"]["resources"]],
            ["notes.md"],
        )


if __name__ == "__main__":
    unittest.main()
