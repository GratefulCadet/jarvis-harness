from __future__ import annotations

import json
import urllib.error
import urllib.request

from harness.adapters.base import register_adapter
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse, ToolCall

_KEYS = ("llama_cpp:openai_compat", "openai:openai_compat", "ollama:openai_compat")

"""OpenAI-compatible `/v1/chat/completions` adapter — llama.cpp server / 원격 / Ollama /v1.

Slice 2 wire 매핑 (OpenAI 포맷):
- 요청: `tools: [{"type": "function", "function": {name, description, parameters}}]`
- 응답 tool_calls: `choices[0].message.tool_calls[]` — `id`·`function.arguments`(JSON string)
- 되돌림 메시지: assistant는 id 포함 tool_calls, 결과는
  `{"role": "tool", "tool_call_id": <id>, "content": <string>}`

streaming은 여전히 다음 slice.
"""


def _to_wire_message(message: dict) -> dict:
    """Harness 중립 메시지 → OpenAI wire 메시지."""
    role = message.get("role")
    if role == "assistant" and message.get("tool_calls"):
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": [
                {
                    "id": call.get("id") or f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(
                            call.get("arguments") or {},
                            ensure_ascii=False,
                        ),
                    },
                }
                for index, call in enumerate(message["tool_calls"])
                if isinstance(call, dict) and call.get("name")
            ],
        }
    if role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.get("tool_call_id") or "",
            "content": str(message.get("content") or ""),
        }
    return dict(message)


def _parse_arguments(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"_raw": raw}
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}


def _parse_tool_calls(message: dict) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in message.get("tool_calls") or []:
        function = item.get("function") or {}
        name = str(function.get("name") or "")
        if not name:
            continue
        calls.append(
            ToolCall(
                name=name,
                arguments=_parse_arguments(function.get("arguments")),
                id=item.get("id") or None,
            )
        )
    return calls


@register_adapter(_KEYS[0])
@register_adapter(_KEYS[1])
@register_adapter(_KEYS[2])
class OpenAICompatAdapter:
    """OpenAI-compatible `/v1/chat/completions` — llama.cpp server / 원격 / Ollama /v1.

    Slice 2: tool_calls 지원 (streaming 제외).
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
            "messages": [_to_wire_message(message) for message in request.messages],
            "stream": False,
            "temperature": self._temperature,
            "max_tokens": self._num_predict,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
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
            tool_calls=_parse_tool_calls(message),
            finish_reason=str(choice.get("finish_reason", "stop")),
            trace_id=request.trace_id,
        )
