from __future__ import annotations

import time
from typing import Any, Sequence

from harness.adapters.base import create_adapter
from harness.config import HarnessConfig
from harness.models import (
    ChatRequest,
    ChatResponse,
    RuntimeAdapter,
    ToolSchema,
)
from harness.trace import TraceRecorder


class HarnessClient:
    """JARVIS가 접촉하는 유일한 LLM 접점 (§4.1).

    Slice 0: tool loop 없이 1회 왕복(chat) + trace. system prompt 관리는 다음 slice.
    """

    def __init__(
        self,
        config: HarnessConfig | None = None,
        adapter: RuntimeAdapter | None = None,
        trace: TraceRecorder | None = None,
    ) -> None:
        self._config = config if config is not None else HarnessConfig.load()
        self._adapter = adapter if adapter is not None else create_adapter(self._config)
        self._trace = trace if trace is not None else TraceRecorder(
            trace_dir=self._config.trace_dir,
            enabled=self._config.trace_enabled,
        )

    @property
    def config(self) -> HarnessConfig:
        return self._config

    @property
    def adapter(self) -> RuntimeAdapter:
        return self._adapter

    def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        tools: Sequence[ToolSchema] | None = None,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatResponse:
        request = ChatRequest(
            messages=list(messages),
            tools=list(tools) if tools else None,
            context=context,
            metadata=metadata or {},
            trace_id=self._trace.new_trace_id(),
        )
        meta = {
            "model": self._config.model,
            "runtime": self._config.runtime,
            "prompt_version": self._config.prompt_version,
        }

        started = time.perf_counter()
        try:
            response = self._adapter.chat(request)
            latency_ms = int((time.perf_counter() - started) * 1000)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self._trace.record(request, None, latency_ms, meta=meta, error=str(exc))
            raise

        response.trace_id = request.trace_id
        self._trace.record(request, response, latency_ms, meta=meta)
        return response
