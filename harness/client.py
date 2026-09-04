from __future__ import annotations

import time
from typing import Any, Mapping, Sequence

from harness.adapters.base import create_adapter
from harness.config import HarnessConfig
from harness.models import (
    ChatRequest,
    ChatResponse,
    RuntimeAdapter,
    ToolResult,
    ToolSchema,
)
from harness.tools import Tool, ToolRegistry, build_default_registry
from harness.trace import TraceRecorder


class HarnessClient:
    """JARVIS가 접촉하는 유일한 LLM 접점 (§4.1).

    Slice 0: 1회 왕복(chat) + trace. Slice 1: tool registry·검증·gate 접점
    (register_tool/execute_tool). system prompt 관리와 tool-call 루프는 다음 slice.
    """

    def __init__(
        self,
        config: HarnessConfig | None = None,
        adapter: RuntimeAdapter | None = None,
        trace: TraceRecorder | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self._config = config if config is not None else HarnessConfig.load()
        self._adapter = adapter if adapter is not None else create_adapter(self._config)
        self._trace = trace if trace is not None else TraceRecorder(
            trace_dir=self._config.trace_dir,
            enabled=self._config.trace_enabled,
        )
        # Slice 1 (§8): schema + 검증 + gate가 붙은 기본 tool set.
        # JARVIS는 register_tool()로 자체 handler를 추가/교체할 수 있다.
        self._tools = tools if tools is not None else build_default_registry(
            memory_dir=self._config.memory_dir
        )

    @property
    def config(self) -> HarnessConfig:
        return self._config

    @property
    def adapter(self) -> RuntimeAdapter:
        return self._adapter

    @property
    def tools(self) -> ToolRegistry:
        """Tool schema·검증·gate 접점 (§8). tool_calls 실행 시 execute_tool() 사용."""
        return self._tools

    def register_tool(self, tool: Tool) -> None:
        """JARVIS가 자체 tool handler를 등록하는 지점 (동일 schema·gate 유지)."""
        self._tools.register(tool)

    def execute_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        approve_write: bool = False,
    ) -> ToolResult:
        """§8.3 흐름대로 schema 검증 → Permission Gate → handler 실행."""
        return self._tools.execute(name, arguments, approve_write=approve_write)

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
