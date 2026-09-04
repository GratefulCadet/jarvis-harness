from __future__ import annotations

from pathlib import Path

from harness.tools.create_task import build_create_task_tool
from harness.tools.get_project_context import build_get_project_context_tool
from harness.tools.memory_context import MemoryContextReader, UnknownProjectError
from harness.tools.registry import Tool, ToolRegistry, validate_arguments
from harness.tools.schemas import (
    list_current_tasks as list_current_tasks_schema,
    propose_next_action as propose_next_action_schema,
)

"""§8 초기 tool 4개 등록.

handler 보유 (Slice 1·4 reference 구현 — memory 대상):
- get_project_context (read) — local-jarvis memory/*.md 조회
- create_task (write) — memory/tasks.md 기록, Permission Gate(§8.3-2) 적용

나머지 2개는 schema-only로 등록해 registry가 명확한 안내 오류를 반환한다
(list_current_tasks/propose_next_action = JARVIS 내부 로직 대상 — §8.1).
"""

__all__ = [
    "MemoryContextReader",
    "Tool",
    "ToolRegistry",
    "UnknownProjectError",
    "build_default_registry",
    "validate_arguments",
]


def build_default_registry(
    memory_dir: str | Path | None = None,
    task_file: str | Path | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_get_project_context_tool(memory_dir))
    registry.register(
        Tool(
            schema=list_current_tasks_schema(),
            kind="read",
            handler=None,
            unavailable_hint="JARVIS 내부 task 저장소 연동 단계에서 등록될 예정",
        )
    )
    registry.register(
        Tool(
            schema=propose_next_action_schema(),
            kind="read",
            handler=None,
            unavailable_hint="JARVIS 내부 next-action 로직 연동 단계에서 등록될 예정",
        )
    )
    registry.register(build_create_task_tool(memory_dir, task_file))
    return registry
