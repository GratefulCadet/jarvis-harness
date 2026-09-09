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


def list_projects() -> ToolSchema:
    """프로젝트 전체 나열 (read — Context Discovery)."""
    return _schema(
        "list_projects",
        (
            "현재 등록된 모든 프로젝트를 나열한다 (id, 제목, task 수). "
            "사용자가 '모든 프로젝트 알려줘', '프로젝트 목록 보여줘'처럼 전체 목록을 "
            "물으면 이 도구를 호출한다. project_id를 모를 때도 후보를 찾기 위해 "
            "먼저 호출할 수 있다. 프로젝트 id를 사용자에게 물어보기 전에 반드시 이 "
            "도구로 확인한다."
        ),
        {},
        [],
    )


def search_projects() -> ToolSchema:
    """프로젝트 이름/내용 검색 (read — Context Discovery)."""
    return _schema(
        "search_projects",
        (
            "사람이 기억하는 이름·설명·하던 작업 내용으로 프로젝트를 검색한다. "
            "정확한 project_id를 모를 때 사용한다. 예: 'JARVIS 프로젝트 찾아줘', "
            "'보컬 앱 아이디어 작업하던 프로젝트 찾아줘'. 프로젝트 id·제목과 각 "
            "프로젝트의 task 제목/이유를 결정적 매칭으로 조회한다. "
            "결과가 2개 이상이면 임의로 고르지 말고 id·제목·matched_text를 "
            "사용자에게 보여주고 어느 쪽인지 되묻는다. 후보를 특정한 뒤에는 "
            "반환된 project_id로 get_project_context를 호출해 정확한 문맥을 조회한다."
        ),
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
        (
            "프로젝트·task·지식 페이지·승인된 파일을 한 번에 통합 검색한다. "
            "'LPIPS 관련해서 작업하던 거 찾아줘'처럼 어느 도메인에 있는지 모르는 "
            "광범위한 기억 검색에 사용한다. 각 결과는 type(id·제목·matched_on)과 "
            "출처(project_id·path 등)를 함께 반환하므로, 결과의 type을 보고 "
            "get_project_context 같은 정확한 조회 tool로 이어간다. 페이지(Page)와 "
            "파일(File)은 프로젝트와 별개로 매칭될 수 있다 — 근거 없이 소속을 추론하지 "
            "말고 결과의 명시적 필드만 사용자에게 말한다."
        ),
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
        (
            "승인된 파일 루트 아래의 폴더·파일 구조를 조회한다 (읽기 전용). "
            "사용자가 파일이나 폴더 구조를 물으면 사용한다. path는 루트 기준 상대 "
            "경로(생략 시 루트 전체). 루트가 여러 개면 root로 하나를 고른다. "
            "승인된 루트가 없으면 설정 방법을 안내한다."
        ),
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
        (
            "승인된 파일 루트 안의 텍스트 파일 내용을 읽는다 (읽기 전용, 크기 제한). "
            "경로는 루트 기준 상대 경로. 민감 파일(.env·키·자격증명 등)과 숨김 파일은 "
            "항상 차단되며, 쓰기는 불가능하다."
        ),
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
        (
            "승인된 파일 루트 안에서 파일 이름·경로·텍스트 내용을 검색한다 "
            "(읽기 전용, 결과 수 제한). '파일에서 LPIPS 찾아줘' 같은 요청에 사용한다. "
            "프로젝트·task·지식 페이지까지 두루 찾으려면 search_context를 먼저 고려한다."
        ),
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
