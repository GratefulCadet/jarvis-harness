from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse, ToolCall
from harness.tools import build_default_registry
from harness.tools.task_store import TaskStore, TaskValidationError

"""Slice 4 — create_task 저장소 + confirm 흐름 검증.

요구사항 매핑 (커버 테스트):
- Qwen/fake adapter가 create_task 제안 → gate 차단, 상태 변이 0       (Client: 12)
- finish_reason == awaiting_confirmation + 정확한 call 보존             (Client: 12)
- 승인 → 정확히 그 call만 1회 실행, 결과가 모델에 피드백, 최종 응답      (Client: 13)
- 재시도해도 중복 생성 없음 (멱등)                                     (Client: 14)
- 잘못된 인자 → 쓰기 없음                                              (Client: 15)
- 알 수 없는/금지된 write → 차단                                        (Client: 16)
- 승인 후 모델이 새 write 제안 → 재차단 (1회 승인 = 1회 실행)           (Client: 17)
- 기존 read tool(get_project_context)은 confirm 없이 동작 유지          (Client: 18)
"""

PROJECTS_MD = """# Projects

## Active

- local-jarvis: 로컬 우선 개인 AI 비서 기반 구축
- content-lab: 콘텐츠 실험 운영

## Routing

- 상세 프로젝트 상태는 확정된 정보만 추가한다.
"""

PROFILE_MD = """# Profile

## Confirmed

- 기본 응답 언어: 한국어
"""


def make_memory_dir(root: Path) -> Path:
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
    (memory / "profile.md").write_text(PROFILE_MD, encoding="utf-8")
    return memory


def make_store(root: Path) -> TaskStore:
    memory = make_memory_dir(root)
    return TaskStore(memory / "tasks.md", memory_dir=memory)


class ScriptedFakeAdapter:
    """chat_with_tools 루프 검증용 scripted fake (RuntimeAdapter Protocol)."""

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = list(script)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        response = self._script.pop(0) if self._script else ChatResponse(content="[fake-final]")
        response.trace_id = request.trace_id
        return response


def make_client(root: Path, script: list[ChatResponse]) -> tuple[HarnessClient, ScriptedFakeAdapter]:
    config = HarnessConfig(
        runtime="mock",
        trace_dir=Path(root) / "traces",
        memory_dir=make_memory_dir(Path(root)),
    )
    adapter = ScriptedFakeAdapter(script)
    registry = build_default_registry(memory_dir=config.memory_dir)
    client = HarnessClient(config, adapter=adapter, tools=registry)
    return client, adapter


def create_task_call(title: str = "Slice 4 검증", project: str = "local-jarvis") -> ToolCall:
    return ToolCall(
        name="create_task",
        arguments={"project_id": project, "title": title, "reason": "confirm 흐름 검증"},
    )


def tasks_file(root: Path) -> Path:
    return Path(root) / "memory" / "tasks.md"


def read_trace(root: Path) -> dict:
    written = list((Path(root) / "traces").glob("*.json"))
    assert len(written) == 1, f"expected 1 trace, got {len(written)}"
    return json.loads(written[0].read_text(encoding="utf-8"))


USER_MSG = [{"role": "user", "content": "local-jarvis 프로젝트에 새 task를 생성해줘."}]


class TaskStoreTests(unittest.TestCase):
    def test_create_writes_entry_and_returns_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = make_store(Path(temp))
            result = store.create("local-jarvis", "Slice 4 검증", "confirm 흐름 검증")
            self.assertTrue(result["created"])
            self.assertTrue(result["id"].startswith("t-"))
            text = tasks_file(Path(temp)).read_text(encoding="utf-8")
            self.assertIn("## local-jarvis", text)
            self.assertIn("- [ ] " + result["id"] + ": Slice 4 검증 — confirm 흐름 검증", text)

    def test_reason_optional(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = make_store(Path(temp))
            store.create("local-jarvis", "제목만 있음")
            text = tasks_file(Path(temp)).read_text(encoding="utf-8")
            self.assertIn("- [ ] t-", text)
            self.assertNotIn("—", text.splitlines()[-2])

    def test_identical_create_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = make_store(Path(temp))
            first = store.create("local-jarvis", "같은 제목", "같은 이유")
            second = store.create("local-jarvis", "같은 제목", "같은 이유")
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["id"], second["id"])
            tasks = store.list_tasks("local-jarvis")
            self.assertEqual(len(tasks), 1)

    def test_list_tasks_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = make_store(Path(temp))
            store.create("local-jarvis", "A", "이유 A")
            store.create("content-lab", "B")
            local = store.list_tasks("local-jarvis")
            self.assertEqual(len(local), 1)
            self.assertEqual(local[0]["title"], "A")
            self.assertEqual(local[0]["reason"], "이유 A")
            self.assertFalse(local[0]["done"])
            self.assertEqual(len(store.list_tasks("content-lab")), 1)

    def test_unknown_project_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = make_store(Path(temp))
            with self.assertRaises(TaskValidationError) as ctx:
                store.create("ghost", "title")
            self.assertIn("local-jarvis", str(ctx.exception))
            self.assertFalse(tasks_file(Path(temp)).exists())

    def test_unsafe_project_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = make_store(Path(temp))
            for bad in ("../escape", "a/b", "a\\b", ".hidden"):
                with self.assertRaises(TaskValidationError, msg=bad):
                    store.create(bad, "title")
            self.assertFalse(tasks_file(Path(temp)).exists())

    def test_empty_title_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = make_store(Path(temp))
            with self.assertRaises(TaskValidationError):
                store.create("local-jarvis", "   ")
            self.assertFalse(tasks_file(Path(temp)).exists())

    def test_newline_injection_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = make_store(Path(temp))
            with self.assertRaises(TaskValidationError):
                store.create("local-jarvis", "제목\n## 다른 프로젝트\n- [ ] 가짜")
            self.assertFalse(tasks_file(Path(temp)).exists())

    def test_missing_file_creates_with_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = make_store(Path(temp))
            store.create("local-jarvis", "첫 task")
            text = tasks_file(Path(temp)).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# Tasks"))

    def test_preserves_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            tasks = memory / "tasks.md"
            tasks.write_text(
                "# Tasks\n\n## content-lab\n\n- [ ] t-old: 기존 task\n\n정렬되지 않은 줄 보존\n",
                encoding="utf-8",
            )
            store = TaskStore(tasks, memory_dir=memory)
            store.create("local-jarvis", "새 task")
            text = tasks.read_text(encoding="utf-8")
            self.assertIn("t-old: 기존 task", text)
            self.assertIn("정렬되지 않은 줄 보존", text)
            self.assertIn("## local-jarvis", text)
            self.assertIn("새 task", text)

    def test_task_file_outside_memory_dir_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            outside = Path(temp) / "outside" / "tasks.md"
            with self.assertRaises(ValueError) as ctx:
                TaskStore(outside, memory_dir=memory)
            self.assertIn("memory_dir 밖", str(ctx.exception))


class CreateTaskConfirmationFlowTests(unittest.TestCase):
    def test_proposed_create_task_awaits_confirmation_with_zero_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            proposed = create_task_call()
            client, adapter = make_client(
                Path(temp),
                [ChatResponse(content="", tool_calls=[proposed])],
            )
            response = client.chat_with_tools(list(USER_MSG))

            self.assertEqual(response.finish_reason, "awaiting_confirmation")
            self.assertEqual(response.tool_calls, [proposed])
            self.assertFalse(tasks_file(Path(temp)).exists(), "confirm 전 파일 생성 금지")
            self.assertEqual(len(adapter.requests), 1)  # 루프 중단 — 재호출 없음

            entry = read_trace(Path(temp))
            self.assertTrue(entry["tool_results"][0]["result"]["requires_confirmation"])

    def test_confirmed_calls_executes_exact_action_and_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            proposed = create_task_call(title="승인 후 생성")
            client, adapter = make_client(
                Path(temp),
                [ChatResponse(content="task를 생성했습니다.")],
            )
            response = client.chat_with_tools(
                list(USER_MSG), confirmed_calls=[proposed]
            )

            self.assertEqual(response.finish_reason, "stop")
            self.assertEqual(response.content, "task를 생성했습니다.")
            # 정확히 제안된 call만 1회 실행 — 파일에 정확한 title 기록
            tasks = tasks_file(Path(temp)).read_text(encoding="utf-8")
            self.assertIn("승인 후 생성", tasks)
            self.assertEqual(tasks.count("- [ ] t-"), 1)

            # tool result가 모델에 피드백됨
            model_request = adapter.requests[0]
            tool_messages = [m for m in model_request.messages if m["role"] == "tool"]
            self.assertEqual(len(tool_messages), 1)
            payload = json.loads(tool_messages[0]["content"])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["title"], "승인 후 생성")
            self.assertTrue(payload["data"]["created"])

    def test_retry_does_not_duplicate_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            proposed = create_task_call()
            client1, _ = make_client(Path(temp), [ChatResponse(content="생성 완료 1")])
            client1.chat_with_tools(list(USER_MSG), confirmed_calls=[proposed])
            client2, _ = make_client(Path(temp), [ChatResponse(content="생성 완료 2")])
            client2.chat_with_tools(list(USER_MSG), confirmed_calls=[proposed])

            tasks = tasks_file(Path(temp)).read_text(encoding="utf-8")
            self.assertEqual(tasks.count("- [ ] t-"), 1)

    def test_invalid_arguments_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bad_call = ToolCall(
                name="create_task",
                arguments={"project_id": "local-jarvis", "title": "   "},
            )
            client, adapter = make_client(
                Path(temp), [ChatResponse(content="제목을 다시 확인하겠습니다.")]
            )
            response = client.chat_with_tools(
                list(USER_MSG), confirmed_calls=[bad_call]
            )

            self.assertEqual(response.finish_reason, "stop")
            self.assertFalse(tasks_file(Path(temp)).exists(), "잘못된 인자로 쓰기 금지")
            tool_messages = [
                m for m in adapter.requests[0].messages if m["role"] == "tool"
            ]
            payload = json.loads(tool_messages[0]["content"])
            self.assertFalse(payload["ok"])
            self.assertIn("title", payload["error"])

    def test_unknown_confirmed_call_fed_back_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            evil = ToolCall(name="delete_everything", arguments={})
            client, adapter = make_client(
                Path(temp), [ChatResponse(content="알겠습니다. 실행하지 않겠습니다.")]
            )
            response = client.chat_with_tools(
                list(USER_MSG), confirmed_calls=[evil]
            )

            self.assertEqual(response.finish_reason, "stop")
            self.assertFalse(tasks_file(Path(temp)).exists())
            tool_messages = [
                m for m in adapter.requests[0].messages if m["role"] == "tool"
            ]
            payload = json.loads(tool_messages[0]["content"])
            self.assertIn("알 수 없는 tool", payload["error"])

    def test_model_proposed_write_after_confirmation_is_gated_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            approved = create_task_call(title="승인된 task")
            second = create_task_call(title="모델이 새로 제안한 task")
            client, adapter = make_client(
                Path(temp),
                [ChatResponse(content="", tool_calls=[second])],
            )
            response = client.chat_with_tools(
                list(USER_MSG), confirmed_calls=[approved]
            )

            # 승인된 call은 1회 실행되었지만, 새 제안은 재차단
            self.assertEqual(response.finish_reason, "awaiting_confirmation")
            self.assertEqual(response.tool_calls, [second])
            tasks = tasks_file(Path(temp)).read_text(encoding="utf-8")
            self.assertEqual(tasks.count("- [ ] t-"), 1)
            self.assertNotIn("모델이 새로 제안한 task", tasks)

    def test_get_project_context_still_works_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            gpc = ToolCall(name="get_project_context", arguments={"project_id": "local-jarvis"})
            client, adapter = make_client(
                Path(temp),
                [
                    ChatResponse(content="", tool_calls=[gpc]),
                    ChatResponse(content="프로젝트 문맥을 확인했습니다."),
                ],
            )
            response = client.chat_with_tools(list(USER_MSG))

            self.assertEqual(response.finish_reason, "stop")
            self.assertEqual(response.content, "프로젝트 문맥을 확인했습니다.")
            self.assertFalse(tasks_file(Path(temp)).exists(), "read tool은 쓰기 없음")


if __name__ == "__main__":
    unittest.main()