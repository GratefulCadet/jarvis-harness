from __future__ import annotations

import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.models import ChatRequest, ChatResponse


def _serializable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


class TraceRecorder:
    """trace_id 단위 JSON 기록 (§9.1).

    Slice 0: e2e latency만 기록. PII 필터(§9.2)·TTFT(streaming)는 다음 slice.
    """

    def __init__(self, trace_dir: Path, enabled: bool = True) -> None:
        self._trace_dir = Path(trace_dir)
        self._enabled = enabled

    @staticmethod
    def new_trace_id() -> str:
        return uuid.uuid4().hex[:12]

    def record(
        self,
        request: ChatRequest,
        response: ChatResponse | None,
        latency_ms: int,
        *,
        meta: dict[str, Any] | None = None,
        error: str | None = None,
        tool_results: list[Any] | None = None,
        turns: int | None = None,
    ) -> Path | None:
        if not self._enabled:
            return None

        trace_id = request.trace_id or self.new_trace_id()
        self._trace_dir.mkdir(parents=True, exist_ok=True)

        entry: dict[str, Any] = {
            "trace_id": trace_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "request": {
                "messages": [_serializable(message) for message in request.messages],
                "tools": [_serializable(tool) for tool in (request.tools or [])],
                "context_keys": sorted((request.context or {}).keys()),
                "metadata": request.metadata,
            },
            "response": (
                {
                    "content": response.content,
                    "tool_calls": [_serializable(call) for call in response.tool_calls],
                    "finish_reason": response.finish_reason,
                }
                if response is not None
                else None
            ),
            "error": error,
            "latency_ms": {"e2e": latency_ms},
            "pii_filtered": False,  # §9.2 — 다음 slice에서 활성화
        }
        # §9.1 tool_results·turns — Slice 2 chat_with_tools 루프에서 채움
        if tool_results is not None:
            entry["tool_results"] = [_serializable(item) for item in tool_results]
        if turns is not None:
            entry["turns"] = turns
        if meta:
            entry.update(meta)

        path = (
            self._trace_dir
            / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{trace_id}.json"
        )
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def record_activity(
        self,
        *,
        project_id: str,
        operation: str,
        arguments: dict[str, Any],
        summary: str,
        active_file: dict[str, Any] | None = None,
    ) -> Path | None:
        """모델을 거치지 않은 deterministic 작업의 세션 신호를 같은 형태로 기록 (M5).

        bridge의 task create/edit/done/delete와 task↔file link/unlink은 모델 tool
        loop를 통하지 않으므로 기존 trace에 남지 않았다. 그 결과 복귀 브리핑의
        touched_task_ids가 실제로는 항상 비어 있었다. 여기서는 record()와 동일한
        entry 모양으로 기록해 세션 연속성 신호가 런타임에 실제로 존재하게 한다.

        작업 결과 자체는 남기지 않는다 — 대상 식별자와 요약만 기록한다.
        metadata.activity가 있으면 이 entry는 사용자 발화가 아니라 시스템 작업이다.
        """
        metadata: dict[str, Any] = {
            "source": "electron_bridge",
            "project_id": project_id,
            "activity": operation,
        }
        if active_file:
            metadata["active_file"] = active_file

        request = ChatRequest(
            messages=[{"role": "user", "content": summary}],
            metadata=metadata,
        )
        response = ChatResponse(content=summary)
        return self.record(
            request,
            response,
            0,
            tool_results=[
                {
                    "call": {
                        "id": f"activity_{operation}",
                        "name": operation,
                        "arguments": dict(arguments),
                    },
                    "result": {
                        "ok": True,
                        "data": {},
                        "error": None,
                        "requires_confirmation": False,
                    },
                }
            ],
            turns=1,
        )
