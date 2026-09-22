from __future__ import annotations

import re
from typing import Any, Sequence

from harness.tools.registry import ToolRegistry


def _is_internal_question(messages: Sequence[dict[str, Any]]) -> bool:
    text = " ".join(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "user"
    ).lower()
    return any(
        marker in text
        for marker in (
            "tool", "api", "schema", "debug", "디버그", "내부", "개발자", "구현", "함수명"
        )
    )


def _grounded_fallback(registry: ToolRegistry) -> str:
    """Safe ordinary-user summary when the model leaks implementation workflow."""
    create_tool = registry.get("create_task")
    fields = ["project_id", "title", "reason"]
    if create_tool is not None:
        properties = (create_tool.schema.parameters or {}).get("properties", {})
        if isinstance(properties, dict):
            fields = sorted(properties)

    parts = [
        "현재 JARVIS는 프로젝트, 작업, 관련 문맥을 내부적으로 확인해 답하는 구조입니다.",
        "외부 LLM에서 받은 작업은 먼저 사람이 읽기 쉬운 프로젝트 힌트와 제목·이유로 정리하면 됩니다.",
        '{"project_hint":"JARVIS", "tasks":[{"title":"작업 제목", "reason":"작업 이유"}]}',
        f"이 예시는 외부 전달 형식이며, 실제 Task 저장에는 {', '.join(fields)} 필드만 사용됩니다.",
        "프로젝트 이름이나 작업 내용을 주시면 JARVIS가 기존 프로젝트를 내부적으로 찾아 연결합니다.",
    ]
    if registry.get("create_project") is None:
        parts.append("현재 새 프로젝트를 임의로 생성하는 기능은 제공하지 않습니다.")
    next_action = registry.get("propose_next_action")
    if next_action is not None and next_action.handler is None:
        parts.append(
            "다음 행동은 현재 읽은 문맥을 바탕으로 대화에서 제안할 수 있지만, "
            "별도 자동 기능으로 가장하지 않습니다."
        )
    return "\n\n".join(parts)


def ground_final_response(
    content: str,
    messages: Sequence[dict[str, Any]],
    registry: ToolRegistry,
) -> str:
    """Keep ordinary final answers outcome-focused and schema-grounded.

    Explicit API/debugging questions retain technical terminology. For ordinary
    users, any detected implementation workflow is replaced by one truthful
    summary rather than attempting brittle phrase-by-phrase repair.
    """
    if _is_internal_question(messages):
        return content

    result = re.sub(r"</?analysis>", "", content, flags=re.IGNORECASE).strip()
    if not result:
        return _grounded_fallback(registry)

    internal_names = {schema.name for schema in registry.schemas()}
    internal_phrases = {
        phrase.strip()
        for schema in registry.schemas()
        for phrase in (schema.description, *schema.description.split("."))
        if phrase.strip()
    }
    leaked = any(name in result for name in internal_names)
    leaked = leaked or any(phrase in result for phrase in internal_phrases)
    leaked = leaked or any(
        marker in result
        for marker in (
            "프로젝트 ID를 물어", "프로젝트 ID를 제공", "프로젝트 ID를 입력",
            "확인하세요", "조회하세요", "검색하세요", "호출하세요",
            "추천받으세요", "사용하세요", "실행하세요", "사용해",
        )
    )
    if leaked:
        return _grounded_fallback(registry)

    create_tool = registry.get("create_task")
    if create_tool is not None:
        supported = set((create_tool.schema.parameters or {}).get("properties", {}))
        candidate_keys = set(re.findall(r"[\"']([A-Za-z_][\w-]*)[\"']\s*:", result))
        unsupported = sorted(candidate_keys - supported)
        if unsupported:
            fields = ", ".join(sorted(supported))
            result += (
                "\n\n참고: 위 JSON은 외부 handoff 예시입니다. "
                f"실제 Task 저장에는 {fields} 필드만 사용됩니다."
            )
    return result
