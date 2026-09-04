from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.config import HarnessConfig
from harness.models import ChatResponse, ToolCall
from scripts.harness_bridge import (
    BridgeSession,
    handle_message,
    resolve_memory_dir,
    run_bridge,
)

"""Electron ↔ Harness JSONL 브리지 프로토콜 검증 (Task 1).

요구사항 매핑:
- chat → final / awaiting_confirmation 응답                          (handle_message)
- awaiting_confirmation이 제안된 call을 정확히 보존                  (exact-call)
- confirm이 그 call을 그대로 confirmed_calls로 전달                  (exact-call)
- reject → 0변이, 모델 호출 없음                                     (reject)
- ping / shutdown / 잘못된 JSON → 안전한 응답                       (protocol)
- 예외 → status=error (Electron이 ERROR 상태 표시)                  (failure)
- 기본 memory_dir은 격리 scratch, JARVIS_BRIDGE_MEMORY_DIR 우선      (scratch)
"""


class FakeClient:
    """HarnessClient 최소 fake — chat_with_tools 인자와 결과를 기록/스크립트."""

    def __init__(
        self,
        responses: list[ChatResponse],
        memory_dir: Path,
        trace_dir: Path,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.config = HarnessConfig(
            runtime="mock",
            memory_dir=memory_dir,
            trace_dir=trace_dir,
        )

    def chat_with_tools(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        response = self.responses.pop(0) if self.responses else ChatResponse(
            content="[fake-final]"
        )
        response.trace_id = "trace-fake"
        return response


def proposed_call(title="새 task") -> ToolCall:
    return ToolCall(
        name="create_task",
        arguments={"project_id": "jarvis-app", "title": title, "reason": "브리지 검증"},
    )


class ResolveMemoryDirTests(unittest.TestCase):
    def test_default_is_project_scratch(self) -> None:
        memory_dir, scratch = resolve_memory_dir(None)
        self.assertTrue(scratch)
        self.assertIn("data", memory_dir.parts)

    def test_env_override_disables_scratch(self) -> None:
        import os
        import unittest.mock as mock

        with mock.patch.dict(
            os.environ, {"JARVIS_BRIDGE_MEMORY_DIR": "C:/tmp/real-memory"}
        ):
            memory_dir, scratch = resolve_memory_dir(None)
        self.assertFalse(scratch)
        self.assertEqual(memory_dir, Path("C:/tmp/real-memory"))


class HandleMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = self.root / "memory"
        self.memory.mkdir(parents=True)
        self.trace = self.root / "traces"
        self.trace.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _client(self, *responses: ChatResponse) -> FakeClient:
        return FakeClient(list(responses), self.memory, self.trace)

    def test_ping(self) -> None:
        client = self._client()
        response = handle_message(client, BridgeSession(), {"type": "ping", "id": 1})
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["id"], 1)

    def test_chat_final(self) -> None:
        client = self._client(ChatResponse(content="현재 task 1개입니다."))
        response = handle_message(
            client, BridgeSession(),
            {"type": "chat", "id": 7, "text": "할 일을 보여줘.", "project_id": "jarvis-app"},
        )
        self.assertEqual(response["status"], "final")
        self.assertEqual(response["text"], "현재 task 1개입니다.")
        self.assertEqual(response["id"], 7)
        self.assertEqual(client.calls[0]["metadata"]["project_id"], "jarvis-app")

    def test_chat_awaiting_confirmation_preserves_exact_call(self) -> None:
        call = proposed_call(title="PiP 통합 확인")
        client = self._client(
            ChatResponse(content="", tool_calls=[call], finish_reason="awaiting_confirmation")
        )
        response = handle_message(
            client, BridgeSession(),
            {"type": "chat", "id": 2, "text": "새 task를 추가해줘.", "project_id": "jarvis-app"},
        )
        self.assertEqual(response["status"], "awaiting_confirmation")
        self.assertEqual(response["tool_call"]["name"], "create_task")
        self.assertEqual(response["tool_call"]["arguments"], call.arguments)
        self.assertIsNone(client.calls[0]["confirmed_calls"])

    def test_confirm_passes_exact_call(self) -> None:
        call = proposed_call()
        client = self._client(ChatResponse(content="생성했습니다."))
        session = BridgeSession()
        session.last_text = "새 task를 추가해줘."
        response = handle_message(
            client, session,
            {"type": "confirm", "id": 3, "tool_call": {
                "name": call.name, "arguments": call.arguments, "id": call.id,
            }},
        )
        self.assertEqual(response["status"], "final")
        confirmed = client.calls[0]["confirmed_calls"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["name"], "create_task")
        self.assertEqual(confirmed[0]["arguments"], call.arguments)
        # 모델이 인자를 바꿀 수 없다 — confirmed_calls가 정확히 제안본
        self.assertEqual(confirmed[0]["arguments"]["title"], "새 task")

    def test_reject_is_zero_mutation_no_model_call(self) -> None:
        client = self._client()
        response = handle_message(
            client, BridgeSession(),
            {"type": "reject", "id": 4, "tool_call": {
                "name": "create_task", "arguments": {"project_id": "jarvis-app", "title": "x"},
            }},
        )
        self.assertEqual(response["status"], "rejected")
        self.assertEqual(client.calls, [], "reject는 모델 호출 없음 — 0변이")

    def test_unknown_type_error(self) -> None:
        client = self._client()
        response = handle_message(client, BridgeSession(), {"type": "explode", "id": 5})
        self.assertEqual(response["status"], "error")

    def test_chat_exception_becomes_error(self) -> None:
        client = self._client()

        def boom(*args, **kwargs):
            raise RuntimeError("ollama down")

        client.chat_with_tools = boom  # type: ignore[method-assign]
        response = handle_message(
            client, BridgeSession(),
            {"type": "chat", "id": 6, "text": "안녕", "project_id": "jarvis-app"},
        )
        self.assertEqual(response["status"], "error")
        self.assertIn("ollama down", response["error"])

    def test_tool_loop_limit_becomes_error(self) -> None:
        client = self._client(
            ChatResponse(
                content="tool 루프 한도에 도달해 중단합니다.",
                tool_calls=[proposed_call()],
                finish_reason="tool_loop_limit",
            )
        )
        response = handle_message(
            client, BridgeSession(),
            {"type": "chat", "id": 8, "text": "계속해", "project_id": "jarvis-app"},
        )
        self.assertEqual(response["status"], "error")


class RunBridgeProtocolTests(unittest.TestCase):
    def test_full_line_protocol(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        memory = root / "memory"
        memory.mkdir(parents=True)
        client = FakeClient(
            [
                ChatResponse(content="task 1개 있습니다."),
                ChatResponse(content="", tool_calls=[proposed_call()], finish_reason="awaiting_confirmation"),
                ChatResponse(content="생성했습니다."),
            ],
            memory,
            root / "traces",
        )
        lines = [
            json.dumps({"type": "ping", "id": 1}),
            json.dumps({"type": "chat", "id": 2, "text": "목록 보여줘", "project_id": "jarvis-app"}),
            "이건 JSON이 아님",
            json.dumps({"type": "chat", "id": 3, "text": "task 추가해줘", "project_id": "jarvis-app"}),
            json.dumps({"type": "reject", "id": 4, "tool_call": {"name": "create_task", "arguments": {}}}),
            json.dumps({"type": "shutdown", "id": 5}),
        ]
        outputs: list[str] = []
        run_bridge(
            client,
            read_line=lambda: lines.pop(0) if lines else "",
            write_line=outputs.append,
        )
        parsed = [json.loads(line) for line in outputs]
        statuses = [entry["status"] for entry in parsed]
        self.assertEqual(
            statuses,
            ["ok", "final", "error", "awaiting_confirmation", "rejected", "ok"],
        )
        self.assertEqual(parsed[1]["id"], 2)
        self.assertEqual(parsed[3]["tool_call"]["name"], "create_task")
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()