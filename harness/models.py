from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # OpenAI-compatible wire가 tool 결과를 tool_call_id로 짝지을 때 사용.
    # Ollama native(/api/chat)는 id 없이 순서로 짝지으므로 None이어도 된다.
    id: str | None = None


@dataclass
class ToolResult:
    tool_call_id: str
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    # Permission Gate (§8.3): write tool이 사용자 confirm 없이 실행 요청되면 True.
    # True면 ok=False이며 handler는 호출되지 않았다.
    requires_confirmation: bool = False


@dataclass
class ChatRequest:
    messages: list[dict[str, str]]
    tools: list[ToolSchema] | None = None
    context: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    trace_id: str | None = None


class RuntimeAdapter(Protocol):
    """모델 runtime 교체 지점 (§6.2). Slice 0는 동기 시그니처."""

    def chat(self, request: ChatRequest) -> ChatResponse: ...
