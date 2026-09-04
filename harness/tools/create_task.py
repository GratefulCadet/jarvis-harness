from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.tools.registry import Tool
from harness.tools.schemas import create_task as create_task_schema
from harness.tools.task_store import TaskStore

"""Slice 4 write tool: create_task(project_id, title, reason).

reference 저장소 = 참조 앱(local-jarvis)의 memory/tasks.md (task_store.py 참고 —
앱에 task 추상화가 없어 앱의 markdown 관례를 따른다). write 분류 + Permission
Gate(§8.3-2)가 적용되므로 사용자 confirm 없이는 handler가 호출되지 않는다.
JARVIS가 자체 저장소를 도입하면 동일 schema·gate로 handler를 교체 등록할 수 있다.
"""


def build_create_task_tool(
    memory_dir: str | Path | None,
    task_file: str | Path | None = None,
) -> Tool:
    schema = create_task_schema()

    if memory_dir is None:
        return Tool(
            schema=schema,
            kind="write",
            handler=None,
            unavailable_hint=(
                "memory_dir 미설정. configs/harness.yaml의 tools.memory_dir 또는 "
                "HARNESS_MEMORY_DIR 환경변수로 JARVIS memory 경로를 지정하세요"
            ),
        )

    resolved_memory = Path(memory_dir)
    if task_file is None:
        resolved_task = resolved_memory / "tasks.md"
    else:
        candidate = Path(task_file)
        resolved_task = candidate if candidate.is_absolute() else resolved_memory / candidate

    # 생성 시점에 위치 제약(task_file ⊆ memory_dir)을 검증해 설정 오류를 빨리 드러낸다
    store = TaskStore(resolved_task, memory_dir=resolved_memory)

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return store.create(
            arguments["project_id"],
            arguments.get("title"),
            arguments.get("reason"),
        )

    return Tool(schema=schema, kind="write", handler=handler)