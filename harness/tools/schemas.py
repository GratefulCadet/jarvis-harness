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
    description = "내부적으로 확인할 프로젝트 식별자"
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
        "프로젝트의 목표·문맥·최근 결정을 내부적으로 읽는다.",
        _project_id_param(),
        ["project_id"],
    )


def list_current_tasks() -> ToolSchema:
    """현재 진행 중 task 목록 조회 (read)."""
    return _schema(
        "list_current_tasks",
        "지정한 프로젝트의 현재 진행 중인 task 목록을 내부적으로 읽는다.",
        _project_id_param("현재 진행 중 task 조회용"),
        ["project_id"],
    )


def propose_next_action() -> ToolSchema:
    """다음 행동 후보 제안 (read — JARVIS 내부 로직 사용)."""
    return _schema(
        "propose_next_action",
        "지정한 프로젝트의 다음 행동 후보를 계산하는 읽기 기능이다. 현재 handler가 연결된 runtime에서만 사용한다.",
        _project_id_param(),
        ["project_id"],
    )


def resume_briefing() -> ToolSchema:
    """복귀 브리핑 조립 (read — Resume & Continue).

    사용자가 중단 작업을 이어가려 할 때 가장 먼저 호출해 프로젝트·task·자료·
    마지막 활동·다음 행동 제안을 한 번에 받는다.
    """
    return _schema(
        "resume_briefing",
        "사용자가 중단했던 작업을 이어가려 할 때(예: '계속하자', '이어서', '다시 시작') "
        "가장 먼저 호출하는 읽기 기능이다. 프로젝트 확정, 진행 중·완료 task, "
        "연결된 파일, 마지막 활동 기록, 다음 행동 제안을 한 번에 반환한다. "
        "반환 내용을 한국어로 요약해 제시하고 이어서 할 일을 안내한다. "
        "파일이나 상태를 변경하지 않는다.",
        {
            "query": {
                "type": "string",
                "description": (
                    "프로젝트를 찾는 데 쓰는 사용자 표현 — '졸업논문', 프로젝트 "
                    "이름이나 하던 작업 내용. project_id가 있으면 생략 가능."
                ),
            },
            "project_id": {
                "type": "string",
                "description": (
                    "이미 알고 있는 프로젝트 식별자. 있으면 query보다 우선한다. "
                    "생략 가능."
                ),
            },
        },
        [],
    )


def create_task() -> ToolSchema:
    """새 task 생성 (write — 사용자 확인 필요, §8.4)."""
    return _schema(
        "create_task",
        "지정한 프로젝트에 새 task를 생성한다. 상태 변경은 실행 전에 Permission Gate 확인이 필요하다.",
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


def list_projects() -> ToolSchema:
    """프로젝트 전체 나열 (read — Context Discovery)."""
    return _schema(
        "list_projects",
        "현재 등록된 모든 프로젝트의 내부 목록과 task 수를 읽는다.",
        {},
        [],
    )


def search_projects() -> ToolSchema:
    """프로젝트 이름/내용 검색 (read — Context Discovery)."""
    return _schema(
        "search_projects",
        "사람이 기억하는 이름·별명·설명·하던 작업 내용으로 프로젝트 후보를 내부적으로 검색한다.",
        {
            "query": {
                "type": "string",
                "description": "검색어 — 프로젝트 이름, 별명, 그 프로젝트에서 하던 작업 내용 등",
            },
        },
        ["query"],
    )


def search_context() -> ToolSchema:
    """도메인 통합 검색 (read — Context Discovery)."""
    return _schema(
        "search_context",
        "프로젝트·task·지식 페이지·승인된 파일을 한 번에 내부적으로 검색하고 출처를 반환한다.",
        {
            "query": {
                "type": "string",
                "description": "검색어 — 기억하는 단어·주제·이름",
            },
            "limit": {
                "type": "integer",
                "description": "최대 결과 수 (기본 10, 최대 25). 생략 가능.",
            },
        },
        ["query"],
    )


def list_files() -> ToolSchema:
    """승인된 루트의 파일 구조 조회 (read — File access, §7)."""
    return _schema(
        "list_files",
        "승인된 파일 루트 아래의 폴더·파일 구조를 읽기 전용으로 반환한다. path와 root는 내부 상대 위치 지정에 사용한다.",
        {
            "root": {
                "type": "string",
                "description": "루트 이름 (생략 시 첫 번째 루트). 생략 가능.",
            },
            "path": {
                "type": "string",
                "description": "루트 기준 상대 경로 (생략 시 루트). 생략 가능.",
            },
            "depth": {
                "type": "integer",
                "description": "최대 탐색 깊이 (기본 4). 생략 가능.",
            },
        },
        [],
    )


def read_file() -> ToolSchema:
    """승인된 루트의 텍스트 파일 읽기 (read — File access, §7)."""
    return _schema(
        "read_file",
        "승인된 파일 루트 안의 텍스트 파일 내용을 읽기 전용으로 반환한다. 민감·숨김 파일과 쓰기는 차단된다.",
        {
            "path": {
                "type": "string",
                "description": "루트 기준 상대 파일 경로",
            },
            "root": {
                "type": "string",
                "description": "루트 이름 (생략 시 첫 번째 루트). 생략 가능.",
            },
            "max_chars": {
                "type": "integer",
                "description": "최대 반환 문자 수 (기본 8000). 생략 가능.",
            },
        },
        ["path"],
    )


def search_files() -> ToolSchema:
    """승인된 루트에서 파일 검색 (read — File access, §7)."""
    return _schema(
        "search_files",
        "승인된 파일 루트 안에서 파일 이름·경로·텍스트 내용을 읽기 전용으로 검색한다. root로 검색 범위를 제한할 수 있다.",
        {
            "query": {
                "type": "string",
                "description": "검색어 — 파일 이름 또는 본문에 포함된 단어",
            },
            "root": {
                "type": "string",
                "description": "루트 이름 (생략 시 모든 루트). 생략 가능.",
            },
            "limit": {
                "type": "integer",
                "description": "최대 결과 수 (기본 20). 생략 가능.",
            },
        },
        ["query"],
    )
