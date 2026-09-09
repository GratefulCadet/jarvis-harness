from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.tools.discovery import Discovery
from harness.tools.file_store import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_RESULTS,
    DEFAULT_READ_CHARS,
    FileStore,
    parse_roots,
)
from harness.tools.registry import Tool
from harness.tools.schemas import (
    list_files as list_files_schema,
    list_projects as list_projects_schema,
    read_file as read_file_schema,
    search_context as search_context_schema,
    search_files as search_files_schema,
    search_projects as search_projects_schema,
)

"""Context Discovery + file access read tools (PART B/C/D).

모든 tool은 read 분류 — Permission Gate(confirm) 없이 실행된다(§8.3-2).
Discovery는 기존 canonical 원천(MemoryContextReader·TaskStore·PageStore·
FileStore)을 읽는 adapter일 뿐 새 저장소를 만들지 않는다(§3).
"""


def _build_discovery(
    memory_dir: str | Path | None,
    task_file: str | Path | None,
    pages_dir: str | Path | None,
    file_roots: str | dict[str, str] | None,
) -> Discovery | None:
    if memory_dir is None:
        return None
    return Discovery(memory_dir, task_file=task_file, pages_dir=pages_dir, file_roots=file_roots)


def build_list_projects_tool(
    memory_dir: str | Path | None,
) -> Tool:
    """PART B-1 — 모든 프로젝트 나열 (id 없이도)."""
    schema = list_projects_schema()
    if memory_dir is None:
        return Tool(
            schema=schema,
            kind="read",
            handler=None,
            unavailable_hint=(
                "memory_dir 미설정. configs/harness.yaml의 tools.memory_dir 또는 "
                "HARNESS_MEMORY_DIR 환경변수로 JARVIS memory 경로를 지정하세요"
            ),
        )
    discovery = _build_discovery(memory_dir, None, None, None)

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        projects = discovery.list_projects_detailed()
        return {
            "count": len(projects),
            "projects": projects,
        }

    return Tool(schema=schema, kind="read", handler=handler)


def build_search_projects_tool(
    memory_dir: str | Path | None,
    task_file: str | Path | None = None,
) -> Tool:
    """PART B — 이름/내용으로 프로젝트 검색."""
    schema = search_projects_schema()
    if memory_dir is None:
        return Tool(
            schema=schema,
            kind="read",
            handler=None,
            unavailable_hint=(
                "memory_dir 미설정. configs/harness.yaml의 tools.memory_dir 또는 "
                "HARNESS_MEMORY_DIR 환경변수로 JARVIS memory 경로를 지정하세요"
            ),
        )
    discovery = _build_discovery(memory_dir, task_file, None, None)

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return discovery.search_projects(arguments["query"])

    return Tool(schema=schema, kind="read", handler=handler)


def build_search_context_tool(
    memory_dir: str | Path | None,
    task_file: str | Path | None = None,
    pages_dir: str | Path | None = None,
    file_roots: str | dict[str, str] | None = None,
) -> Tool:
    """PART C — 도메인 통합 검색."""
    schema = search_context_schema()
    if memory_dir is None:
        return Tool(
            schema=schema,
            kind="read",
            handler=None,
            unavailable_hint=(
                "memory_dir 미설정. configs/harness.yaml의 tools.memory_dir 또는 "
                "HARNESS_MEMORY_DIR 환경변수로 JARVIS memory 경로를 지정하세요"
            ),
        )
    discovery = _build_discovery(memory_dir, task_file, pages_dir, file_roots)

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return discovery.search_context(
            arguments["query"], limit=arguments.get("limit") or 10
        )

    return Tool(schema=schema, kind="read", handler=handler)


def build_file_tools(
    file_roots: str | dict[str, str] | None,
) -> list[Tool]:
    """PART D — read-only 파일 tool 3개 (list/read/search)."""
    tools: list[Tool] = []

    files = None
    if isinstance(file_roots, dict) and file_roots:
        files = FileStore(file_roots)
    else:
        files = FileStore(parse_roots(file_roots))

    # list_files
    schema = list_files_schema()
    if not files.roots:
        tools.append(Tool(
            schema=schema,
            kind="read",
            handler=None,
            unavailable_hint=(
                "승인된 파일 루트가 없습니다. JARVIS_FILE_ROOTS(name=path,...) "
                "또는 configs/harness.yaml tools.file_roots로 사용자가 직접 승인한 "
                "루트만 등록할 수 있습니다 (임의 시스템 경로는 자동 승인되지 않음)."
            ),
        ))
    else:
        def list_handler(arguments: dict[str, Any]) -> dict[str, Any]:
            return files.list_tree(
                root=arguments.get("root"),
                relative=arguments.get("path"),
                max_depth=arguments.get("depth") or DEFAULT_MAX_DEPTH,
            )
        tools.append(Tool(schema=schema, kind="read", handler=list_handler))

    # read_file
    schema = read_file_schema()
    if not files.roots:
        tools.append(Tool(
            schema=schema,
            kind="read",
            handler=None,
            unavailable_hint=(
                "승인된 파일 루트가 없습니다. JARVIS_FILE_ROOTS(name=path,...) "
                "또는 configs/harness.yaml tools.file_roots로 등록하세요."
            ),
        ))
    else:
        def read_handler(arguments: dict[str, Any]) -> dict[str, Any]:
            return files.read_text(
                arguments["path"],
                root=arguments.get("root"),
                max_chars=arguments.get("max_chars") or DEFAULT_READ_CHARS,
            )
        tools.append(Tool(schema=schema, kind="read", handler=read_handler))

    # search_files
    schema = search_files_schema()
    if not files.roots:
        tools.append(Tool(
            schema=schema,
            kind="read",
            handler=None,
            unavailable_hint=(
                "승인된 파일 루트가 없습니다. JARVIS_FILE_ROOTS(name=path,...) "
                "또는 configs/harness.yaml tools.file_roots로 등록하세요."
            ),
        ))
    else:
        def search_handler(arguments: dict[str, Any]) -> dict[str, Any]:
            return files.search(
                arguments["query"],
                root=arguments.get("root"),
                limit=arguments.get("limit") or DEFAULT_MAX_RESULTS,
            )
        tools.append(Tool(schema=schema, kind="read", handler=search_handler))

    return tools
