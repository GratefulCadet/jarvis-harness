from __future__ import annotations

import os
from pathlib import Path

from harness.tools.create_task import build_create_task_tool
from harness.tools.discovery_tools import (
    build_file_tools,
    build_list_projects_tool,
    build_search_context_tool,
    build_search_projects_tool,
)
from harness.tools.get_project_context import build_get_project_context_tool
from harness.tools.list_current_tasks import build_list_current_tasks_tool
from harness.tools.memory_context import MemoryContextReader, UnknownProjectError
from harness.tools.registry import Tool, ToolRegistry, validate_arguments
from harness.tools.resume_briefing import build_resume_briefing_tool
from harness.tools.schemas import propose_next_action as propose_next_action_schema

"""§8 초기 tool 등록.

handler 보유 (Slice 1·4·5 + Context Discovery reference 구현 — memory 대상):
- get_project_context (read) — local-jarvis memory/*.md 조회
- list_current_tasks (read) — memory/tasks.md의 현재 task 목록 조회
- create_task (write) — memory/tasks.md 기록, Permission Gate(§8.3-2) 적용
- list_projects (read) — 전체 프로젝트 나열 (Context Discovery)
- search_projects (read) — 이름/작업 내용으로 프로젝트 검색
- search_context (read) — Project/Task/Page/File 통합 검색
- list_files / read_file / search_files (read) — 승인된 루트만 (§7)
- resume_briefing (read) — 복귀 브리핑: 프로젝트·task·자료·마지막 활동·다음 행동 제안

propose_next_action은 schema-only로 등록해 registry가 명확한 안내 오류를
반환한다(JARVIS 내부 next-action 로직 대상 — §8.1).
"""

__all__ = [
    "MemoryContextReader",
    "Tool",
    "ToolRegistry",
    "UnknownProjectError",
    "build_default_registry",
    "validate_arguments",
]


def _resolve_file_roots(config_value: object) -> str | dict[str, str] | None:
    """환경변수 우선, 그다음 config 값 (§16 — 파일시스템 루트는 명시적 승인만)."""
    env_roots = os.environ.get("JARVIS_FILE_ROOTS")
    if env_roots:
        return env_roots
    if isinstance(config_value, dict) and config_value:
        return config_value
    if isinstance(config_value, str) and config_value.strip():
        return config_value
    return None


def build_default_registry(
    memory_dir: str | Path | None = None,
    task_file: str | Path | None = None,
    pages_dir: str | Path | None = None,
    file_roots: str | dict[str, str] | None = None,
    trace_dir: str | Path | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_get_project_context_tool(memory_dir))
    registry.register(build_list_current_tasks_tool(memory_dir, task_file))
    registry.register(build_list_projects_tool(memory_dir, file_roots=file_roots))
    registry.register(
        build_search_projects_tool(memory_dir, task_file, file_roots=file_roots)
    )
    registry.register(
        build_search_context_tool(memory_dir, task_file, pages_dir, file_roots)
    )
    registry.register(
        build_resume_briefing_tool(
            memory_dir,
            task_file,
            pages_dir=pages_dir,
            file_roots=file_roots,
            trace_dir=trace_dir,
        )
    )
    for tool in build_file_tools(file_roots, memory_dir=memory_dir):
        registry.register(tool)
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
