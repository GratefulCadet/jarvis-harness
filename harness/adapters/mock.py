from __future__ import annotations

from harness.adapters.base import register_adapter
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse


@register_adapter("mock:native_chat")
class MockAdapter:
    """Ollama 없이 HarnessClient ↔ trace 왕복을 검증하기 위한 mock."""

    def __init__(self, config: HarnessConfig) -> None:
        self._model = config.model

    def chat(self, request: ChatRequest) -> ChatResponse:
        last_user = next(
            (m["content"] for m in reversed(request.messages) if m.get("role") == "user"),
            "",
        )
        return ChatResponse(
            content=f"[mock:{self._model}] {last_user} — (mock 응답, 실제 LLM 호출 없음)",
            trace_id=request.trace_id,
        )
