from __future__ import annotations

import os
from pathlib import Path

from harness.tools.create_task import build_create_task_tool
from harness.tools.disk_roots import list_disk, read_disk_file  # noqa: F401
from harness.tools.discovery_tools import (
    build_file_tools,
    build_list_projects_tool,
    build_search_context_tool,
    build_search_projects_tool,
)
from harness.tools.edit_proposals import (
    EditProposalStore,
    build_edit_proposal_tool,
    default_edit_proposals_file,
)
from harness.tools.file_store import FileStore, parse_roots
from harness.tools.file_write_tools import build_create_file_tool
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


def build_edit_file_tool(files: FileStore, memory_dir: str | Path | None = None):
    """승인된 루트 안의 기존 파일에 대한 편집 제안 tool (AI Edit V1).

    FileStore/WorkspaceManager를 공유해 bridge의 identity 계층과 같은 FileRef
    레지스트리를 본다 — 그래야 모델이 read_file로 받은 file_id가 그대로
    제안 대상이 된다.
    """
    from harness.tools.workspace import WorkspaceManager, default_registry_file

    workspace = WorkspaceManager(
        files, default_registry_file(Path(memory_dir) if memory_dir else None)
    )
    store = EditProposalStore(default_edit_proposals_file(memory_dir))
    return build_edit_proposal_tool(workspace, store)


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

    # File access 확장(2026-09-26 사용자 결정) — 디스크 전체 읽기 + 승인 루트
    # 안에서의 모델 새 파일 생성(write, 승인 gate). 승인 루트가 없어도 list_disk/
    # read_disk_file은 동작한다(읽기는 전체 디스크로 허용됨).
    from harness.tools.disk_roots import list_disk as _list_disk
    from harness.tools.disk_roots import read_disk_file as _read_disk_file
    from harness.tools.schemas import list_disk as _list_disk_schema
    from harness.tools.schemas import read_disk_file as _read_disk_schema

    from harness.models import ToolSchema

    def _disk_list_handler(arguments: dict[str, object]) -> dict[str, object]:
        return _list_disk(str(arguments.get("path") or ""))

    def _disk_read_handler(arguments: dict[str, object]) -> dict[str, object]:
        return _read_disk_file(
            str(arguments.get("path") or ""), arguments.get("max_chars")
        )

    registry.register(
        Tool(schema=_list_disk_schema(), kind="read", handler=_disk_list_handler)
    )
    registry.register(
        Tool(schema=_read_disk_schema(), kind="read", handler=_disk_read_handler)
    )

    # create_file — 유효 승인 루트(레지스트리+legacy 병합)가 있을 때만 실제 handler를
    # 단다(쓰기는 승인 루트 한정). legacy file_roots가 None이어도 persistent
    # workspace_roots.json에서 resolve한다(build_file_tools와 동일 기준).
    effective_roots = _resolve_file_roots(file_roots)
    if effective_roots is None and memory_dir is not None:
        try:
            from harness.tools.workspace_roots import resolve_effective_roots

            resolved = resolve_effective_roots(memory_dir, None)
            if resolved:
                effective_roots = {k: str(v) for k, v in resolved.items()}
        except Exception:
            effective_roots = None
    if effective_roots:
        store_roots = (
            effective_roots
            if isinstance(effective_roots, dict)
            else parse_roots(effective_roots)
        )
        if store_roots:
            shared_file_store = FileStore(
                {k: str(v) for k, v in store_roots.items()}
            )
            registry.register(
                build_create_file_tool(shared_file_store, memory_dir=memory_dir)
            )
            # AI Edit V1 — edit_file 제안 tool. kind="read"이므로 gate에 막히지
            # 않고 diff를 만들어 사용자에게 보여준다. 실제 쓰기는 bridge의
            # edit_apply(사용자 클릭)에서만 일어난다.
            registry.register(
                build_edit_file_tool(shared_file_store, memory_dir=memory_dir)
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
