from __future__ import annotations

import json
import urllib.error
import urllib.request

from harness.adapters.base import register_adapter
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse, ToolCall

"""Ollama native `/api/chat` adapter (Slice 0 → Slice 2: tool_calls 지원).

참조 앱(local-jarvis) `tools/voice_assistant.py::ask_ollama`와 같은 엔드포인트·옵션을
protocol 뒤로 숨긴다. urllib·stdlib만 사용(openai SDK 불필요).

Slice 2 wire 매핑 (Ollama native 포맷):
- 요청: `tools: [{"type": "function", "function": {name, description, parameters}}]`
- 응답 tool_calls: `message.tool_calls[].function.{name, arguments}`
- 되돌림 메시지: assistant는 tool_calls(fn.name/fn.arguments) 포함, 결과는
  `{"role": "tool", "content": <json string>}` — id 없이 순서로 짝지음.
"""


def _to_native_message(message: dict) -> dict:
    """Harness 중립 메시지 → Ollama native wire 메시지.

    system/user/assistant(텍스트)는 그대로, tool 관련 메시지만 변환한다.
    """
    role = message.get("role")
    if role == "assistant" and message.get("tool_calls"):
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": [
                {
                    "function": {
                        "name": call["name"],
                        "arguments": call.get("arguments") or {},
                    }
                }
                for call in message["tool_calls"]
                if isinstance(call, dict) and call.get("name")
            ],
        }
    if role == "tool":
        # Ollama native는 tool_call_id를 쓰지 않는다 — 순서로 tool 결과를 짝지음.
        return {"role": "tool", "content": message.get("content") or ""}
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
                id=None,  # Ollama native는 id를 주지 않는다
            )
        )
    return calls


@register_adapter("ollama:native_chat")
class OllamaNativeAdapter:
    """Ollama native `/api/chat` 호출 (Slice 2: tool_calls 지원)."""

    def __init__(self, config: HarnessConfig) -> None:
        self._model = config.model
        self._url = f"{config.base_url.rstrip('/')}/api/chat"
        self._timeout = config.timeout_s
        self._temperature = config.temperature
        self._num_predict = config.num_predict

    def chat(self, request: ChatRequest) -> ChatResponse:
        payload = {
            "model": self._model,
            "messages": [_to_native_message(message) for message in request.messages],
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._num_predict,
            },
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
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
                f"Ollama 서버에 연결할 수 없습니다 ({self._url}). Ollama가 실행 중인지 확인하세요."
            ) from exc
        except (KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ollama 응답 형식 오류: {exc}") from exc

        message = result.get("message") or {}
        return ChatResponse(
            content=str(message.get("content") or "").strip(),
            tool_calls=_parse_tool_calls(message),
            finish_reason=str(result.get("done_reason", "stop")),
            trace_id=request.trace_id,
        )
