from __future__ import annotations

import json
import time
from typing import Any, Mapping, Sequence

from harness.adapters.base import create_adapter
from harness.config import HarnessConfig
from harness.models import (
    ChatRequest,
    ChatResponse,
    RuntimeAdapter,
    ToolCall,
    ToolResult,
    ToolSchema,
)
from harness.tools import Tool, ToolRegistry, build_default_registry
from harness.trace import TraceRecorder
from harness.response_grounding import ground_final_response


_FINAL_RESPONSE_GROUNDING = (
    "The internal reads above are complete. Now answer the ordinary user directly. "
    "Do not mention tools, function names, schemas, IDs, backticks, or invocation "
    "steps. Summarize what you found in natural language and say that JARVIS can "
    "resolve an existing project from its name or context. Keep any external "
    "handoff suggestion separate from the canonical fields."
)


_CAPABILITY_GROUNDING = (
    "You are JARVIS, a user-facing assistant. Treat the tools supplied in this "
    "request as the complete set of executable capabilities for this turn. "
    "Use them internally when needed; do not instruct an ordinary user to call "
    "implementation-level tool names. Describe current writable data structures "
    "only from the supplied schemas; never invent fields. If offering a human "
    "handoff format, label it as external/interchange guidance and distinguish it "
    "from the canonical tool payload. Be honest about unavailable capabilities. "
    "For ordinary system-analysis or capability questions, perform relevant reads "
    "silently and report the findings in natural user-facing language. Do not "
    "enumerate tools, quote schema descriptions, describe invocation steps, or "
    "ask the user for internal IDs. You may recommend a next action from context "
    "and reasoning, but do not claim a tool-backed capability unless it is supplied "
    "here. For ordinary user answers, never output function names, tool names, "
    "API names, schema descriptions, backticks, or a step-by-step invocation "
    "recipe. Say what JARVIS found or can do in natural language. If a project "
    "is needed, resolve it from the user's name or context internally rather than "
    "asking for its ID. Before sending an ordinary answer, self-check it: if it "
    "contains a private function identifier, backticks, an invocation verb, or "
    "asks for an ID, rewrite that sentence as a result or natural capability "
    "statement. Resolve projects by "
    "provide internal IDs. Mention tool names only "
    "when the user explicitly asks about internals, APIs, or debugging. Do not "
    "wrap the user-facing answer in analysis or other internal XML tags."
)


def _tool_message_content(result: ToolResult) -> str:
    """ToolResult → 모델에 되돌릴 role=tool 메시지 content (§8.3-3).

    실패(검증 오류·handler 오류)도 error 문구로 되돌려 모델이 수정·재시도하게 한다.
    requires_confirmation(=write gate 차단)은 모델이 해결할 수 없으므로 여기로
    오지 않는다 — 루프가 중단을 결정한다 (chat_with_tools 참고).
    """
    if result.ok:
        payload: dict[str, Any] = {"ok": True, "data": result.data}
    else:
        payload = {"ok": False, "error": result.error}
    return json.dumps(payload, ensure_ascii=False)


class HarnessClient:
    """JARVIS가 접촉하는 유일한 LLM 접점 (§4.1).

    Slice 0: 1회 왕복(chat) + trace. Slice 1: tool registry·검증·gate 접점
    (register_tool/execute_tool). Slice 2: chat_with_tools — 모델 tool-call 루프
    (판단은 LLM, 검증·권한은 registry, 실행은 등록된 handler).
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
            memory_dir=self._config.memory_dir,
            task_file=self._config.task_file,
            pages_dir=self._config.pages_dir,
            file_roots=self._config.file_roots,
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
        """단일 왕복 원시 접점 (Slice 0).

        모델이 tool_calls를 반환하면 실행 없이 그대로 반환한다 — tool 실행이
        필요한 호출자는 chat_with_tools()를 사용해야 한다 (§5).
        """
        request = ChatRequest(
            messages=list(messages),
            tools=list(tools) if tools else None,
            context=context,
            metadata=metadata or {},
            trace_id=self._trace.new_trace_id(),
        )
        meta = self._meta()
        return self._single_roundtrip(request, meta)

    def chat_with_tools(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[ToolSchema] | None = None,
        context: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        max_turns: int | None = None,
        approve_write: bool = False,
        confirmed_calls: Sequence[ToolCall | Mapping[str, Any]] | None = None,
    ) -> ChatResponse:
        """§5·§8.3 tool-call 루프 — JARVIS가 사용할 기본 접점.

        흐름: 모델 호출 → tool_calls가 있으면 registry로 검증·gate·실행 →
        결과를 role=tool 메시지로 되돌려 모델 재호출 → 최종 텍스트 답변까지 반복.

        종료 조건 (finish_reason):
        - 모델이 tool_calls 없이 텍스트로 답하면 "stop" (정상 완료)
        - write tool이 confirm 없이 요청되면 "awaiting_confirmation" —
          tool_calls에 승인 대기 call 유지, handler는 호출되지 않음(§8.3-2).
        - max_turns(기본 config.tool_loop_max_turns) 초과 시 "tool_loop_limit" —
          마지막 turn의 tool 요청은 실행하지 않고 중단 (§8.3-3).

        승인 경로 (Slice 4, §8.4): awaiting_confirmation 응답을 받은 JARVIS는
        사용자 확인 후 **confirmed_calls**에 해당 tool_calls를 그대로 넘겨 재호출한다.
        이 경우 모델을 재호출하지 않고 **정확히 그 call만** approve 상태로 실행한 뒤
        (모델이 다른 write로 대체할 수 없음) tool result를 바탕으로 모델이 최종
        답변을 생성한다. 이후 모델이 새로 제안하는 write는 다시 gate에 걸린다.
        approve_write=True 재호출(모델 재판단 경로)은 호환용으로 유지한다.
        """
        tool_schemas = (
            list(tools) if tools is not None else self._tools.available_schemas()
        )
        limit = max_turns if max_turns is not None else self._config.tool_loop_max_turns
        limit = max(limit, 1)
        working = [dict(message) for message in messages]
        if not any(message.get("role") == "system" for message in working):
            working.insert(0, {"role": "system", "content": _CAPABILITY_GROUNDING})
        trace_id = self._trace.new_trace_id()
        meta = self._meta()
        loop_meta: dict[str, Any] = {"max_turns": limit, "approve_write": approve_write}

        tool_results: list[dict[str, Any]] = []
        turns = 0
        started = time.perf_counter()
        try:
            if confirmed_calls:
                normalized = [self._as_tool_call(call) for call in confirmed_calls]
                loop_meta["confirmed_calls"] = [
                    self._copy_call(call) for call in normalized
                ]
                # 승인된 call을 assistant tool_calls로 echo — 모델이 결과와 짝지을 수 있게
                working.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [self._copy_call(call) for call in normalized],
                })
                blocked = self._execute_tool_batch(
                    working, normalized, tool_results, approve_write=True
                )
                if blocked:
                    # 방어적: approve_write=True인데 gate가 다시 막을 수는 없지만,
                    # 등록 해제 등 예외 상황 대비 (승인 대기 상태로 중단)
                    final = ChatResponse(
                        content=(
                            f"승인된 call이 다시 차단되었습니다: "
                            f"{', '.join(call.name for call in blocked)}"
                        ),
                        tool_calls=list(blocked),
                        finish_reason="awaiting_confirmation",
                        trace_id=trace_id,
                    )
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    self._trace.record(
                        ChatRequest(
                            messages=list(working),
                            tools=tool_schemas,
                            context=context,
                            metadata=metadata or {},
                            trace_id=trace_id,
                        ),
                        final, latency_ms,
                        meta=meta, tool_results=tool_results, turns=turns,
                    )
                    return final
                # 이후 모델이 새로 제안하는 write는 다시 confirm 필요
                approve_write = False

            meta["loop"] = loop_meta
            for _ in range(limit):
                request = ChatRequest(
                    messages=working,
                    tools=tool_schemas,
                    context=context,
                    metadata=metadata or {},
                    trace_id=trace_id,
                )
                response = self._adapter.chat(request)
                turns += 1

                if not response.tool_calls:
                    response.content = ground_final_response(
                        response.content, working, self._tools
                    )
                    response.trace_id = trace_id
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    self._trace.record(
                        request, response, latency_ms,
                        meta=meta, tool_results=tool_results, turns=turns,
                    )
                    return response

                if turns >= limit:
                    # 한도 도달 — 마지막 turn의 tool 요청은 실행하지 않고 중단
                    blocked_text = ", ".join(
                        f"{call.name}{json.dumps(call.arguments, ensure_ascii=False)}"
                        for call in response.tool_calls
                    )
                    final = ChatResponse(
                        content=(
                            f"tool 루프 한도({limit}회)에 도달해 중단합니다. "
                            f"마지막 tool 요청(실행 안 함): {blocked_text} (§8.3-3)"
                        ),
                        tool_calls=list(response.tool_calls),
                        finish_reason="tool_loop_limit",
                        trace_id=trace_id,
                    )
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    self._trace.record(
                        request, final, latency_ms,
                        meta=meta, tool_results=tool_results, turns=turns,
                    )
                    return final

                # 이전 assistant turn을 echo(모델이 tool 결과와 짝지을 수 있게)
                working.append({
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        self._copy_call(call) for call in response.tool_calls
                    ],
                })

                blocked = self._execute_tool_batch(
                    working, response.tool_calls, tool_results,
                    approve_write=approve_write,
                )

                if not blocked:
                    working.append({
                        "role": "user",
                        "content": _FINAL_RESPONSE_GROUNDING,
                    })

                if blocked:
                    names = ", ".join(call.name for call in blocked)
                    final = ChatResponse(
                        content=(
                            f"write tool 실행은 사용자 확인이 필요해 중단했습니다: {names}. "
                            "확인 후 approve_write=True로 재호출하세요 (§8.3-2)."
                        ),
                        tool_calls=list(blocked),
                        finish_reason="awaiting_confirmation",
                        trace_id=trace_id,
                    )
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    self._trace.record(
                        request, final, latency_ms,
                        meta=meta, tool_results=tool_results, turns=turns,
                    )
                    return final
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self._trace.record(
                request, None, latency_ms,
                meta=meta, error=str(exc),
                tool_results=tool_results, turns=turns,
            )
            raise

        raise RuntimeError("도달 불가 — 루프는 위에서 항상 반환한다")

    def _meta(self) -> dict[str, Any]:
        return {
            "model": self._config.model,
            "runtime": self._config.runtime,
            "prompt_version": self._config.prompt_version,
        }

    def _single_roundtrip(
        self,
        request: ChatRequest,
        meta: dict[str, Any],
    ) -> ChatResponse:
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

    @staticmethod
    def _as_tool_call(call: ToolCall | Mapping[str, Any]) -> ToolCall:
        """ToolCall 또는 dict → ToolCall (confirmed_calls 정규화)."""
        if isinstance(call, ToolCall):
            return call
        return ToolCall(
            name=call["name"],
            arguments=dict(call.get("arguments") or {}),
            id=call.get("id"),
        )

    def _execute_tool_batch(
        self,
        working: list[dict[str, Any]],
        calls: Sequence[ToolCall],
        tool_results: list[dict[str, Any]],
        *,
        approve_write: bool,
    ) -> list[ToolCall]:
        """tool_calls 배치를 registry로 검증·gate·실행하고 결과를 working에 추가.

        gate에 막힌 call이 있으면 그 지점에서 중단하고 막힌 call 목록을 반환한다
        (나머지는 실행하지 않음 — §8.3-2). 반환값이 비면 전부 실행 완료.
        """
        blocked: list[ToolCall] = []
        for index, call in enumerate(calls):
            call_id = call.id or f"call_{index}"
            result = self._tools.execute(
                call.name,
                call.arguments,
                approve_write=approve_write,
            )
            tool_results.append({
                "call": {
                    "id": call_id,
                    "name": call.name,
                    "arguments": dict(call.arguments or {}),
                },
                "result": self._tool_result_dict(result),
            })
            if result.requires_confirmation:
                blocked.append(call)
                break
            working.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": _tool_message_content(result),
            })
        return blocked

    @staticmethod
    def _copy_call(call: ToolCall) -> dict[str, Any]:
        """ToolCall → 중립 메시지 dict (adapter가 wire 포맷으로 변환)."""
        return {
            "id": call.id,
            "name": call.name,
            "arguments": dict(call.arguments or {}),
        }

    @staticmethod
    def _tool_result_dict(result: ToolResult) -> dict[str, Any]:
        return {
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
            "requires_confirmation": result.requires_confirmation,
        }
