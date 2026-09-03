from __future__ import annotations

import json
import urllib.error
import urllib.request

from harness.adapters.base import register_adapter
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse

_KEYS = ("llama_cpp:openai_compat", "openai:openai_compat", "ollama:openai_compat")


@register_adapter(_KEYS[0])
@register_adapter(_KEYS[1])
@register_adapter(_KEYS[2])
class OpenAICompatAdapter:
    """OpenAI-compatible `/v1/chat/completions` — llama.cpp server / 원격 / Ollama /v1.

    Slice 0에서는 채팅 완료 응답(text)만 처리. tool_calls·streaming은 다음 slice.
    """

    def __init__(self, config: HarnessConfig) -> None:
        self._model = config.model
        self._url = f"{config.base_url.rstrip('/')}/v1/chat/completions"
        self._timeout = config.timeout_s
        self._temperature = config.temperature
        self._num_predict = config.num_predict

    def chat(self, request: ChatRequest) -> ChatResponse:
        payload = {
            "model": self._model,
            "messages": request.messages,
            "stream": False,
            "temperature": self._temperature,
            "max_tokens": self._num_predict,
        }
        data = json.dumps(payload).encode("utf-8")
        http_request = urllib.request.Request(
            self._url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self._timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"OpenAI-compatible 서버에 연결할 수 없습니다 ({self._url})."
            ) from exc

        choice = result["choices"][0]
        message = choice.get("message", {})
        return ChatResponse(
            content=str(message.get("content") or "").strip(),
            finish_reason=str(choice.get("finish_reason", "stop")),
            trace_id=request.trace_id,
        )
