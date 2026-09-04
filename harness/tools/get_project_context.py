from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.tools.memory_context import MemoryContextReader
from harness.tools.registry import Tool
from harness.tools.schemas import get_project_context as get_project_context_schema

"""Slice 1 read tool: get_project_context(project_id).

문맥 원천 = 참조 앱(local-jarvis)의 memory/*.md (부록 D 기준 결정 — read-only 조회).
handler는 Slice 1 reference 구현이며, JARVIS 연동 시 앱이 자체 handler로
교체 등록할 수 있다(동일 schema·gate 유지, §8.2).
"""


def build_get_project_context_tool(
    memory_dir: str | Path | None,
) -> Tool:
    schema = get_project_context_schema()

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

    resolved = Path(memory_dir)

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments["project_id"]
        reader = MemoryContextReader(resolved)
        return reader.read_project(project_id)

    return Tool(schema=schema, kind="read", handler=handler)
