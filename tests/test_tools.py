from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.client import HarnessClient
from harness.config import HarnessConfig
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

    def test_default_registry_exposes_four_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registry = registry_with_memory(Path(temp))
            names = sorted(schema.name for schema in registry.schemas())
            self.assertEqual(
                names,
                [
                    "create_task",
                    "get_project_context",
                    "list_current_tasks",
                    "propose_next_action",
                ],
            )


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
                    "propose_next_action",
                },
            )

    def test_memory_dir_none_yields_clear_unavailable_error(self) -> None:
        registry = build_default_registry(memory_dir=None)
        result = registry.execute("get_project_context", {"project_id": "local-jarvis"})
        self.assertFalse(result.ok)
        self.assertIn("memory_dir", result.error)


if __name__ == "__main__":
    unittest.main()
