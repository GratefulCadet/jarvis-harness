from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.tools.registry import Tool
from harness.tools.schemas import list_current_tasks as list_current_tasks_schema
from harness.tools.task_store import TaskStore

"""Slice 5 read tool: list_current_tasks(project_id).

`create_task`의 read-side 동반자. 같은 저장소(JARVIS memory/tasks.md,
task_store.py)를 사용해 현재 진행 중인 task 목록을 조회한다.

- read 분류 → Permission Gate(confirm) 불필요 (§8.3-2).
- task_file이 없으면 빈 목록, 형식이 잘못된 줄은 무시한다(안전, task_store._parse).
- project_id는 create_task와 동일한 검증(Active 목록 + 안전 식별자)을 거친다.
- 쓰기 없음 — handler는 TaskStore.list_tasks(read-only)만 호출한다.
JARVIS가 자체 task 저장소를 도입하면 동일 schema·gate로 handler를 교체 등록할 수
있다(§8.2) — 이 모듈은 그때까지의 reference 구현이다.
"""


def build_list_current_tasks_tool(
    memory_dir: str | Path | None,
    task_file: str | Path | None = None,
) -> Tool:
    schema = list_current_tasks_schema()

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

    resolved_memory = Path(memory_dir)
    if task_file is None:
        resolved_task = resolved_memory / "tasks.md"
    else:
        candidate = Path(task_file)
        resolved_task = candidate if candidate.is_absolute() else resolved_memory / candidate

    # create_task와 동일한 저장소 설정(위치 제약 §8.4)을 재사용한다
    store = TaskStore(resolved_task, memory_dir=resolved_memory)

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        # "현재 진행 중" task = 아직 완료(done)되지 않은 것만 반환한다
        tasks = [
            task for task in store.list_tasks(arguments["project_id"])
            if not task["done"]
        ]
        return {
            "project_id": arguments["project_id"],
            "count": len(tasks),
            "tasks": tasks,
        }

    return Tool(schema=schema, kind="read", handler=handler)