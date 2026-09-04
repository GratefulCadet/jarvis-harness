from __future__ import annotations

from harness.models import ToolSchema

"""초기 tool 4개의 JSON schema (docs/harness-design.md §8.1 `[제안]`).

parameters는 JSON-schema-lite: type object + properties + required.
registry(§8.3 schema 검증)가 이 구조를 그대로 사용한다.
"""


def _schema(
    name: str,
    description: str,
    properties: dict[str, dict],
    required: list[str],
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": required,
        },
    )


def _project_id_param(extra: str = "") -> dict[str, dict]:
    description = "조회 대상 프로젝트 id (예: local-jarvis)"
    if extra:
        description += f". {extra}"
    return {
        "project_id": {
            "type": "string",
            "description": description,
        }
    }


def get_project_context() -> ToolSchema:
    """프로젝트 목표·문맥·최근 결정 조회 (read)."""
    return _schema(
        "get_project_context",
        "프로젝트의 목표·문맥·최근 결정을 조회한다. 프로젝트 작업을 시작하기 전에 호출해 문맥을 복원한다.",
        _project_id_param(),
        ["project_id"],
    )


def list_current_tasks() -> ToolSchema:
    """현재 진행 중 task 목록 조회 (read)."""
    return _schema(
        "list_current_tasks",
        "지정한 프로젝트의 현재 진행 중인 task 목록을 조회한다.",
        _project_id_param("현재 진행 중 task 조회용"),
        ["project_id"],
    )


def propose_next_action() -> ToolSchema:
    """다음 행동 후보 제안 (read — JARVIS 내부 로직 사용)."""
    return _schema(
        "propose_next_action",
        "지정한 프로젝트의 다음 행동(next action) 후보를 제안한다.",
        _project_id_param(),
        ["project_id"],
    )


def create_task() -> ToolSchema:
    """새 task 생성 (write — 사용자 확인 필요, §8.4)."""
    return _schema(
        "create_task",
        "지정한 프로젝트에 새 task를 생성한다. 상태를 변경하는 write tool이므로 실행 전 사용자 확인이 필요하다.",
        {
            **_project_id_param(),
            "title": {
                "type": "string",
                "description": "생성할 task 제목",
            },
            "reason": {
                "type": "string",
                "description": "이 task가 필요한 이유. 없는 경우 생략 가능.",
            },
        },
        ["project_id", "title"],
    )
