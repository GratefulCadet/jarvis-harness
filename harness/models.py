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
class LoopContinuation:
    """tool_loop_limit으로 끊겼을 때 "이어서 갈 수 있는 지점".

    왜 실제 대화만 저장하는가: resume은 사용자가 문장을 다시 쓰지 않고 작업을
    이어가게 하는 기능이다. 그런데 이전 턴의 요청/응답/도구 결과를 지어내면
    모델이 "이미 한 것처럼" 말하면서 실제로는 안 한 일을 보고하거나, 진짜로
    실행된 도구 결과를 다시 실행한다. 그래서 한도 직전까지 실제로 오간
    messages를 그대로 넘겨야 한다.

    messages는 **마지막으로 실행된 도구 결과까지**다. 한도 순간 미실행 상태로
    막힌 tool_calls는 여기에 넣지 않는다 — tool_calls가 달린 assistant
    메시지 뒤에 대응하는 tool 응답이 없으면 어댑터가 메시지를 거부하기 때문.
    대신 blocked_tool_calls로 따로 보관해 "뭘 하려다 막혔는지"를 문장으로
    알려줄 수 있게 한다.
    """

    messages: list[dict[str, Any]]
    blocked_tool_calls: list[ToolCall] = field(default_factory=list)
    turns: int = 0
    max_turns: int = 0


@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    trace_id: str | None = None
    # tool_loop_limit으로 끊겼을 때만 채워진다. 정상 종료(stop/awaiting)에서는 None.
    continuation: LoopContinuation | None = None


class RuntimeAdapter(Protocol):
    """모델 runtime 교체 지점 (§6.2). Slice 0는 동기 시그니처."""

    def chat(self, request: ChatRequest) -> ChatResponse: ...
