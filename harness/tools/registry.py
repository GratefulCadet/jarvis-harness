from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from harness.models import ToolResult, ToolSchema

"""Tool registry + schema 검증 + Permission Gate (docs/harness-design.md §4.5–4.6, §8.3).

역할 분담(§8.2): LLM은 tool 선택·argument 생성, JARVIS(앱)는 실제 실행,
Harness는 schema 검증과 권한 검사만 담당한다. 이 모듈의 Tool.handler는
"JARVIS가 나중에 등록할 실행 함수"의 자리 — Slice 1에서는 read tool
(get_project_context)만 참조 데이터(memory)에 대한 reference handler를 가진다.
"""

ToolKind = Literal["read", "write"]
Preflight = Callable[[dict[str, Any]], None]

_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
}


def validate_arguments(schema: ToolSchema, arguments: Mapping[str, Any]) -> list[str]:
    """§8.3-1 schema 검증 — 오류 목록 반환(비면 통과)."""
    errors: list[str] = []
    if not isinstance(arguments, dict):
        return ["arguments는 object여야 합니다"]

    params = schema.parameters or {}
    properties = params.get("properties", {}) if isinstance(params, dict) else {}
    required = params.get("required", []) if isinstance(params, dict) else []

    for name in required:
        if name not in arguments or arguments[name] is None:
            errors.append(f"필수 인자 누락: {name!r}")

    for name, value in arguments.items():
        prop = properties.get(name) if isinstance(properties, dict) else None
        if prop is None:
            errors.append(f"정의되지 않은 인자: {name!r}")
            continue
        prop_type = prop.get("type")
        check = _TYPE_CHECKS.get(prop_type) if prop_type else None
        if prop_type and check and not check(value):
            errors.append(
                f"인자 {name!r}는 {prop_type}이어야 합니다 (받은 값: {value!r})"
            )
        enum = prop.get("enum")
        if isinstance(enum, list) and value not in enum:
            errors.append(f"인자 {name!r}는 다음 중 하나여야 합니다: {enum}")
    return errors


@dataclass
class Tool:
    """등록 단위: schema + read/write 분류 + (선택) 실행 handler.

    handler가 None이면 registry.execute는 실행하지 않고 안내 오류를 반환한다
    (예: `create_task` — JARVIS 연동 후 앱이 등록).
    """

    schema: ToolSchema
    kind: ToolKind = "read"
    handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Permission Gate 전에 수행하는 side-effect-free domain validation (선택)
    preflight: Preflight | None = None
    # handler 미등록 상태에서 반환할 안내 문구 (선택)
    unavailable_hint: str | None = None


class ToolRegistry:
    """Tool 보관·조회·검증·실행. HarnessClient가 단일 접점으로 노출한다."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.schema.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def schemas(self) -> list[ToolSchema]:
        """Return every registered schema, including diagnostic-only tools."""
        return [tool.schema for tool in self._tools.values()]

    def available_schemas(self) -> list[ToolSchema]:
        """Return only schemas whose handlers are executable right now.

        Registration is useful for diagnostics and future integrations, but it is
        not a truthful model capability boundary: a schema-only tool cannot be
        selected successfully by the current runtime.
        """
        return [
            tool.schema for tool in self._tools.values() if tool.handler is not None
        ]

    def classify(self, name: str) -> ToolKind | None:
        tool = self._tools.get(name)
        return tool.kind if tool else None

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        approve_write: bool = False,
    ) -> ToolResult:
        """§8.3 검증 흐름: schema 검증 → 권한 검사 → (통과 시) 실행.

        실패 사유(검증 오류 등)는 error에 담아 LLM에 되돌려 수정·재시도하게 한다.
        """
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(sorted(self._tools)) or "(없음)"
            return ToolResult(
                tool_call_id=name,
                ok=False,
                error=f"알 수 없는 tool: {name!r}. 사용 가능: {available}",
            )

        if tool.handler is None:
            hint = tool.unavailable_hint or "JARVIS 연동 단계에서 handler가 등록될 예정"
            return ToolResult(
                tool_call_id=name,
                ok=False,
                error=f"tool {name!r} handler 미등록 — {hint}",
            )

        errors = validate_arguments(tool.schema, arguments)
        if errors:
            return ToolResult(
                tool_call_id=name,
                ok=False,
                error="인자 검증 실패: " + "; ".join(errors),
            )

        if tool.preflight is not None:
            try:
                tool.preflight(dict(arguments))
            except Exception as exc:
                return ToolResult(
                    tool_call_id=name,
                    ok=False,
                    error=f"사전 검증 실패: {exc}",
                )

        if tool.kind == "write" and not approve_write:
            # §8.3-2 Permission Gate — confirm 없이는 실행하지 않는다.
            return ToolResult(
                tool_call_id=name,
                ok=False,
                requires_confirmation=True,
                error=(
                    f"write tool {name!r} 실행은 사용자 확인(confirm)이 필요합니다. "
                    "승인 후 approve_write=True로 재시도하세요. (§8.3)"
                ),
            )

        try:
            data = tool.handler(dict(arguments))
            return ToolResult(tool_call_id=name, ok=True, data=data)
        except Exception as exc:  # 실행 실패 — 사유를 LLM에 되돌린다
            return ToolResult(
                tool_call_id=name,
                ok=False,
                error=f"tool {name!r} 실행 실패: {exc}",
            )
