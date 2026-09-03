from __future__ import annotations

import json
import urllib.error
import urllib.request

from harness.adapters.base import register_adapter
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse


@register_adapter("ollama:native_chat")
class OllamaNativeAdapter:
    """Ollama native `/api/chat` 호출.

    참조 앱(local-jarvis) `tools/voice_assistant.py::ask_ollama`와 같은 엔드포인트·옵션을
    protocol 뒤로 숨긴다. urllib·stdlib만 사용(openai SDK 불필요).
    """

    def __init__(self, config: HarnessConfig) -> None:
        self._model = config.model
        self._url = f"{config.base_url.rstrip('/')}/api/chat"
        self._timeout = config.timeout_s
        self._temperature = config.temperature
        self._num_predict = config.num_predict

    def chat(self, request: ChatRequest) -> ChatResponse:
        payload = {
            "model": self._model,
            "messages": request.messages,
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._num_predict,
            },
        }
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

        return ChatResponse(
            content=str(result["message"]["content"]).strip(),
            finish_reason=str(result.get("done_reason", "stop")),
            trace_id=request.trace_id,
        )
