from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse, ToolCall
from harness.tools import (
    Tool,
    ToolRegistry,
    build_default_registry,
    validate_arguments,
)
from harness.tools.memory_context import MemoryContextReader, UnknownProjectError
from harness.tools.schemas import (
    create_task as create_task_schema_fn,
    get_project_context as gpc_schema_fn,
)
from harness.tools.task_store import TaskStore

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

BUSINESS_MD = """# Business Context

## Current Hypothesis

- 초안 생성, 후보 비교, 성과 기록을 먼저 구축한다.
"""

HANDOFF_MD = """# Handoff 2026-06-14

- 다음 행동: Slice 1 검증
"""


def make_memory_dir(root: Path, *, with_handoffs: bool = True) -> Path:
    memory = root / "memory"
    memory.mkdir(parents=True)
    (memory / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
    (memory / "profile.md").write_text(PROFILE_MD, encoding="utf-8")
    (memory / "business_context.md").write_text(BUSINESS_MD, encoding="utf-8")
    if with_handoffs:
        handoffs = memory / "chat_handoffs"
        handoffs.mkdir()
        (handoffs / "2026-06-14-demo.md").write_text(HANDOFF_MD, encoding="utf-8")
    return memory


def registry_with_memory(root: Path) -> ToolRegistry:
    return build_default_registry(memory_dir=make_memory_dir(root))


class SchemaValidationTests(unittest.TestCase):
    def test_missing_required_argument_rejected(self) -> None:
        schema = gpc_schema_fn()
        errors = validate_arguments(schema, {})
        self.assertTrue(any("project_id" in error for error in errors))

    def test_unknown_argument_rejected(self) -> None:
        schema = gpc_schema_fn()
        errors = validate_arguments(schema, {"project_id": "local-jarvis", "extra": 1})
        self.assertTrue(any("extra" in error for error in errors))

    def test_valid_arguments_pass(self) -> None:
        schema = gpc_schema_fn()
        self.assertEqual(
            validate_arguments(schema, {"project_id": "local-jarvis"}), []
        )


class MemoryReaderTests(unittest.TestCase):
    def test_list_projects_parses_active_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            projects = MemoryContextReader(memory).list_projects()
            self.assertEqual(
                projects,
                [
                    {"id": "local-jarvis", "title": "로컬 우선 개인 AI 비서 기반 구축"},
                    {"id": "content-lab", "title": "콘텐츠 실험 운영"},
                ],
            )

    def test_read_project_returns_context_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            data = MemoryContextReader(memory).read_project("local-jarvis")
            self.assertEqual(data["project_id"], "local-jarvis")
            names = {file["name"]: file for file in data["files"]}
            self.assertIn("profile.md", names)
            self.assertIn("business_context.md", names)
            self.assertIn("projects.md", names)
            self.assertIn("2026-06-14-demo.md", names)
            self.assertIn("한국어", names["profile.md"]["content"])
            self.assertEqual(
                names["2026-06-14-demo.md"]["kind"], "handoff"
            )

    def test_unknown_project_raises_with_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            reader = MemoryContextReader(memory)
            with self.assertRaises(UnknownProjectError) as ctx:
                reader.read_project("ghost")
            self.assertIn("local-jarvis", str(ctx.exception))

    def test_missing_memory_dir_raises(self) -> None:
        reader = MemoryContextReader(Path("/nonexistent/memory"))
        with self.assertRaises(FileNotFoundError):
            reader.read_project("local-jarvis")


class RegistryGateTests(unittest.TestCase):
    def test_execute_get_project_context_reads_real_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = registry_with_memory(Path(temp))
            result = registry.execute(
                "get_project_context", {"project_id": "local-jarvis"}
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.data["project_id"], "local-jarvis")
            self.assertTrue(result.data["files"])

    def test_unknown_tool_error_lists_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = registry_with_memory(Path(temp))
            result = registry.execute("nope", {})
            self.assertFalse(result.ok)
            self.assertIn("get_project_context", result.error)

    def test_create_task_now_has_reference_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            registry = build_default_registry(memory_dir=memory)
            # confirm 없이 → gate 차단, handler 미실행, 파일 생성 없음 (§8.3-2)
            result = registry.execute(
                "create_task",
                {"project_id": "local-jarvis", "title": "t"},
            )
            self.assertFalse(result.ok)
            self.assertTrue(result.requires_confirmation)
            self.assertFalse((memory / "tasks.md").exists())
            # 승인 → reference 저장소(memory/tasks.md)에 기록
            approved = registry.execute(
                "create_task",
                {"project_id": "local-jarvis", "title": "t"},
                approve_write=True,
            )
            self.assertTrue(approved.ok, approved.error)
            self.assertTrue(approved.data["created"])
            self.assertTrue((memory / "tasks.md").exists())

    def test_write_tool_blocked_without_confirmation(self) -> None:
        registry = ToolRegistry()
        called = []

        def handler(arguments):
            called.append(arguments)
            return {"created": True}

        registry.register(
            Tool(
                schema=create_task_schema_fn(),
                kind="write",
                handler=handler,
            )
        )
        result = registry.execute(
            "create_task", {"project_id": "p", "title": "t"}
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.requires_confirmation)
        self.assertEqual(called, [])  # confirm 전에는 handler 호출 금지 (§8.4)

    def test_write_tool_runs_when_approved(self) -> None:
        registry = ToolRegistry()
        called = []

        def handler(arguments):
            called.append(arguments)
            return {"created": True}

        registry.register(
            Tool(
                schema=create_task_schema_fn(),
                kind="write",
                handler=handler,
            )
        )
        result = registry.execute(
            "create_task",
            {"project_id": "p", "title": "t"},
            approve_write=True,
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(called), 1)

    def test_default_registry_exposes_expected_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = registry_with_memory(Path(temp))
            names = sorted(schema.name for schema in registry.schemas())
            self.assertEqual(
                names,
                [
                    "create_task",
                    "get_project_context",
                    "list_current_tasks",
                    "list_files",
                    "list_projects",
                    "propose_next_action",
                    "read_file",
                    "search_context",
                    "search_files",
                    "search_projects",
                ],
            )


class ListCurrentTasksTests(unittest.TestCase):
    """Slice 5 — list_current_tasks read tool 검증.

    요구사항 매핑:
    - create_task와 같은 저장소(memory/tasks.md) 사용                    (registry roundtrip)
    - read-only — tasks.md 없으면 빈 목록, 파일 생성·변경 없음            (read-only)
    - 형식이 잘못된 내용도 안전하게 처리                                  (malformed)
    - project filter                                                    (filter)
    - 알 수 없는 프로젝트 → 명확한 오류 (create_task와 동일 검증)          (unknown)
    - confirm 불필요 (read 분류, §8.3-2)                                 (no-confirm)
    - client chat_with_tools 경로에서도 confirm 없이 실행                 (client wiring)
    """

    def _registry(self, root: Path) -> ToolRegistry:
        return build_default_registry(memory_dir=make_memory_dir(root))

    def test_list_returns_created_tasks_same_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            registry = build_default_registry(memory_dir=memory)
            # create_task(승인)로 기록한 task를 list_current_tasks가 읽는다
            created = registry.execute(
                "create_task",
                {"project_id": "local-jarvis", "title": "Slice 5 검증", "reason": "read-side 확인"},
                approve_write=True,
            )
            self.assertTrue(created.ok, created.error)

            listed = registry.execute(
                "list_current_tasks", {"project_id": "local-jarvis"}
            )
            self.assertTrue(listed.ok, listed.error)
            self.assertEqual(listed.data["project_id"], "local-jarvis")
            self.assertEqual(listed.data["count"], 1)
            task = listed.data["tasks"][0]
            self.assertEqual(task["id"], created.data["id"])
            self.assertEqual(task["title"], "Slice 5 검증")
            self.assertEqual(task["reason"], "read-side 확인")
            self.assertFalse(task["done"])

    def test_list_without_tasks_file_is_empty_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = self._registry(Path(temp))
            result = registry.execute(
                "list_current_tasks", {"project_id": "local-jarvis"}
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.data["tasks"], [])
            self.assertEqual(result.data["count"], 0)
            self.assertFalse(
                (Path(temp) / "memory" / "tasks.md").exists(),
                "read는 파일을 만들지 않는다",
            )

    def test_list_is_read_only_preserves_tasks_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            tasks = memory / "tasks.md"
            original = (
                "# Tasks\n\n## local-jarvis\n\n- [ ] t-abc: 기존 task\n"
            )
            tasks.write_text(original, encoding="utf-8")
            registry = build_default_registry(memory_dir=memory)
            result = registry.execute(
                "list_current_tasks", {"project_id": "local-jarvis"}
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.data["count"], 1)
            self.assertEqual(
                tasks.read_text(encoding="utf-8"), original, "list는 내용을 바꾸지 않는다"
            )

    def test_list_handles_malformed_content_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            tasks = memory / "tasks.md"
            tasks.write_text(
                "# Tasks\n\n## local-jarvis\n\n- [ ] t-good: 정상 task\n"
                "- [x] t-done: 완료 task — 이유\n"
                "- [ ] 형식이 잘못된 줄 (id 없음)\n"
                "## content-lab\n\n- [ ] t-other: 다른 프로젝트\n\n"
                "## local-jarvis\n\n- [ ] t-again: 두 번째 섹션\n",
                encoding="utf-8",
            )
            registry = build_default_registry(memory_dir=memory)
            result = registry.execute(
                "list_current_tasks", {"project_id": "local-jarvis"}
            )
            self.assertTrue(result.ok, result.error)
            titles = [task["title"] for task in result.data["tasks"]]
            self.assertEqual(titles, ["정상 task", "두 번째 섹션"])
            self.assertEqual(
                [task["done"] for task in result.data["tasks"]],
                [False, False],
            )
            # 잘못된 줄은 무시하고 계속 진행 (크래시 없음)
            self.assertNotIn("형식이 잘못된 줄", titles)
            self.assertNotIn("다른 프로젝트", titles)

    def test_list_filters_by_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            store = TaskStore(memory / "tasks.md", memory_dir=memory)
            store.create("local-jarvis", "로컬 task")
            store.create("content-lab", "콘텐츠 task")
            registry = build_default_registry(memory_dir=memory)

            local = registry.execute(
                "list_current_tasks", {"project_id": "local-jarvis"}
            )
            content = registry.execute(
                "list_current_tasks", {"project_id": "content-lab"}
            )
            self.assertEqual(
                [task["title"] for task in local.data["tasks"]], ["로컬 task"]
            )
            self.assertEqual(
                [task["title"] for task in content.data["tasks"]], ["콘텐츠 task"]
            )

    def test_list_unknown_project_returns_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = self._registry(Path(temp))
            result = registry.execute(
                "list_current_tasks", {"project_id": "ghost"}
            )
            self.assertFalse(result.ok)
            self.assertFalse(result.requires_confirmation)
            self.assertIn("local-jarvis", result.error)

    def test_list_requires_no_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            registry = build_default_registry(memory_dir=memory)
            # approve_write 없이도 read tool은 실행된다 (§8.3-2)
            result = registry.execute(
                "list_current_tasks", {"project_id": "local-jarvis"}
            )
            self.assertTrue(result.ok, result.error)
            self.assertFalse(result.requires_confirmation)

    def test_list_schema_validates_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = self._registry(Path(temp))
            missing = registry.execute("list_current_tasks", {})
            self.assertFalse(missing.ok)
            self.assertIn("project_id", missing.error)
            extra = registry.execute(
                "list_current_tasks",
                {"project_id": "local-jarvis", "extra": 1},
            )
            self.assertFalse(extra.ok)
            self.assertIn("extra", extra.error)

    def test_client_chat_with_tools_runs_list_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            memory = make_memory_dir(Path(temp))
            store = TaskStore(memory / "tasks.md", memory_dir=memory)
            store.create("local-jarvis", "클라이언트 경로 task")
            config = HarnessConfig(
                runtime="mock",
                trace_dir=Path(temp) / "traces",
                memory_dir=memory,
            )
            adapter = _ScriptedFakeAdapter(
                [
                    ChatResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                name="list_current_tasks",
                                arguments={"project_id": "local-jarvis"},
                            )
                        ],
                    ),
                    ChatResponse(content="현재 task 1개를 확인했습니다."),
                ]
            )
            registry = build_default_registry(memory_dir=memory)
            client = HarnessClient(config, adapter=adapter, tools=registry)
            response = client.chat_with_tools(
                [{"role": "user", "content": "현재 task를 확인해줘."}]
            )

            self.assertEqual(response.finish_reason, "stop")
            self.assertEqual(response.content, "현재 task 1개를 확인했습니다.")
            # tool result가 모델에 정상 피드백됨
            model_request = adapter.requests[0]
            tool_messages = [m for m in model_request.messages if m["role"] == "tool"]
            self.assertEqual(len(tool_messages), 1)
            payload = json.loads(tool_messages[0]["content"])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["count"], 1)
            self.assertEqual(payload["data"]["tasks"][0]["title"], "클라이언트 경로 task")


class _ScriptedFakeAdapter:
    """chat_with_tools 루프 검증용 scripted fake (RuntimeAdapter Protocol)."""

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = list(script)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        response = self._script.pop(0) if self._script else ChatResponse(content="[fake-final]")
        response.trace_id = request.trace_id
        return response


class ClientToolWiringTests(unittest.TestCase):
    def test_client_exposes_registry_and_executes_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = HarnessConfig(
                runtime="mock",
                trace_dir=Path(temp) / "traces",
                memory_dir=make_memory_dir(Path(temp)),
            )
            client = HarnessClient(config)
            result = client.execute_tool(
                "get_project_context", {"project_id": "local-jarvis"}
            )
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.data["project_id"], "local-jarvis")

    def test_tool_schemas_serialize_to_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = HarnessConfig(
                runtime="mock",
                trace_dir=Path(temp) / "traces",
                memory_dir=make_memory_dir(Path(temp)),
            )
            client = HarnessClient(config)
            response = client.chat(
                [{"role": "user", "content": "hi"}],
                tools=client.tools.schemas(),
            )
            self.assertIsNotNone(response.trace_id)
            written = list((Path(temp) / "traces").glob("*.json"))
            self.assertEqual(len(written), 1)
            entry = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(
                {tool["name"] for tool in entry["request"]["tools"]},
                {
                    "create_task",
                    "get_project_context",
                    "list_current_tasks",
                    "list_files",
                    "list_projects",
                    "propose_next_action",
                    "read_file",
                    "search_context",
                    "search_files",
                    "search_projects",
                },
            )

    def test_memory_dir_none_yields_clear_unavailable_error(self) -> None:
        registry = build_default_registry(memory_dir=None)
        result = registry.execute("get_project_context", {"project_id": "local-jarvis"})
        self.assertFalse(result.ok)
        self.assertIn("memory_dir", result.error)


if __name__ == "__main__":
    unittest.main()
