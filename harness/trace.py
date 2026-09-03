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
        if meta:
            entry.update(meta)

        path = (
            self._trace_dir
            / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{trace_id}.json"
        )
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
