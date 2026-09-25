from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.config import HarnessConfig
from harness.models import ChatResponse, ToolCall
from harness.client import _active_file_context as _active_file_system_line
from scripts.harness_bridge import (
    BridgeSession,
    _active_file_context,
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
    def test_default_is_canonical_user_state(self) -> None:
        import os
        import unittest.mock as mock
        # Keep USERPROFILE / APPDATA intact, only remove custom JARVIS overrides
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS_")}
        with mock.patch.dict(os.environ, clean_env, clear=True):
            memory_dir, scratch = resolve_memory_dir(None)
            self.assertFalse(scratch)
            self.assertIn("jarvis-app", memory_dir.parts)

    def test_scratch_flag_forces_scratch(self) -> None:
        import os
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"JARVIS_USE_SCRATCH": "1"}):
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

    def test_migration_copies_absent_files_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as scratch_tmp, tempfile.TemporaryDirectory() as user_tmp:
            s_path = Path(scratch_tmp)
            u_path = Path(user_tmp)

            # scratch has projects.md and tasks.md
            (s_path / "projects.md").write_text("# Scratch Projects", encoding="utf-8")
            (s_path / "tasks.md").write_text("# Scratch Tasks", encoding="utf-8")

            # user state already has existing tasks.md (must not be overwritten)
            (u_path / "tasks.md").write_text("# User Existing Tasks", encoding="utf-8")

            from scripts.harness_bridge import migrate_scratch_to_user_state
            migrated = migrate_scratch_to_user_state(s_path, u_path)

            self.assertIn("projects.md", migrated)
            self.assertNotIn("tasks.md", migrated)
            self.assertEqual((u_path / "projects.md").read_text(encoding="utf-8"), "# Scratch Projects")
            self.assertEqual((u_path / "tasks.md").read_text(encoding="utf-8"), "# User Existing Tasks")


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

    @staticmethod
    def _client_for(*responses: ChatResponse) -> FakeClient:
        """setUp 없이 즉석 FakeClient — ActiveFileContextTests에서 재사용."""
        tmp = tempfile.TemporaryDirectory()
        return FakeClient(
            list(responses),
            Path(tmp.name) / "memory",
            Path(tmp.name) / "traces",
        )

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


class ActiveFileContextTests(unittest.TestCase):
    """Active File 컨텍스트 경계 (V4 Active Workspace File Context).

    요구사항 매핑:
    - identity locator 4개만 남기고 정화 (renderer 임의 값 신뢰 금지)
    - chat → chat_with_tools(context={active_file}) 전달
    - confirm도 같은 컨텍스트 유지 (session 보존)
    - active_file 없으면 context=None (추측 금지)
    """

    def test_sanitizer_keeps_only_identity_fields(self) -> None:
        fields = _active_file_context({
            "active_file": {
                "file_id": "f-abc",
                "root_id": "scratch-root",
                "path": "notes/idea.md",
                "name": "idea.md",
                "content": "이 내용은 무시되어야 한다",
                "absolute": "C:/tmp/notes/idea.md",
            },
        })
        self.assertEqual(fields, {
            "file_id": "f-abc",
            "root_id": "scratch-root",
            "path": "notes/idea.md",
            "name": "idea.md",
        })

    def test_sanitizer_requires_root_and_path(self) -> None:
        self.assertIsNone(_active_file_context({"active_file": {"path": "a.md"}}))
        self.assertIsNone(_active_file_context({"active_file": "notes/idea.md"}))
        self.assertIsNone(_active_file_context({}))

    def test_chat_passes_active_file_context(self) -> None:
        client = HandleMessageTests._client_for(ChatResponse(content="요약입니다."))
        response = handle_message(
            client, BridgeSession(),
            {
                "type": "chat", "id": 11,
                "text": "이 파일을 요약해줘.",
                "project_id": "jarvis-app",
                "active_file": {
                    "file_id": "f-abc",
                    "root_id": "scratch-root",
                    "path": "notes/idea.md",
                    "name": "idea.md",
                    "content": "주입되면 안 되는 내용",
                },
            },
        )
        self.assertEqual(response["status"], "final")
        context = client.calls[0]["context"]
        self.assertEqual(context["active_file"]["path"], "notes/idea.md")
        self.assertNotIn("content", context["active_file"])

    def test_chat_without_active_file_has_no_context(self) -> None:
        client = HandleMessageTests._client_for(ChatResponse(content="답변."))
        handle_message(
            client, BridgeSession(),
            {"type": "chat", "id": 12, "text": "안녕", "project_id": "jarvis-app"},
        )
        self.assertIsNone(client.calls[0]["context"])

    def test_confirm_reuses_last_active_file(self) -> None:
        client = HandleMessageTests._client_for(
            ChatResponse(content="", tool_calls=[proposed_call()], finish_reason="awaiting_confirmation"),
            ChatResponse(content="생성했습니다."),
        )
        session = BridgeSession()
        handle_message(
            client, session,
            {
                "type": "chat", "id": 13,
                "text": "새 task를 추가해줘.",
                "project_id": "jarvis-app",
                "active_file": {
                    "file_id": "f-abc",
                    "root_id": "scratch-root",
                    "path": "notes/idea.md",
                },
            },
        )
        handle_message(
            client, session,
            {
                "type": "confirm", "id": 14,
                "tool_call": {
                    "name": "create_task",
                    "arguments": {"project_id": "jarvis-app", "title": "새 task", "reason": "브리지 검증"},
                },
            },
        )
        context = client.calls[1]["context"]
        self.assertEqual(context["active_file"]["path"], "notes/idea.md")

    def test_client_injects_active_file_system_line(self) -> None:
        """chat_with_tools가 context.active_file을 system 한 줄로 변환하는지 검증."""
        fields = _active_file_context({
            "active_file": {"root_id": "r", "path": "notes/idea.md", "name": "idea.md"},
        })
        line = _active_file_system_line({"active_file": fields})
        self.assertIn("notes/idea.md", line)
        self.assertIn("approved root: r", line)
        self.assertIsNone(_active_file_system_line(None))
        self.assertIsNone(_active_file_system_line({}))
        self.assertIsNone(_active_file_system_line({"active_file": {"path": ""}}))

    def test_quick_action_acceptance_scenarios(self) -> None:
        """
        Acceptance Scenarios A-F:
        A. Active idea.md -> Summarize passes active_file
        B. Switch to other.md -> Explain has other.md context without idea.md leakage
        C. Clear Active File -> subsequent chat carries no active_file context
        D. Dirty unsaved file -> raw content/buffer is not transmitted or accepted
        E. Review action -> read-only analysis response without mutating calls
        F. Explicit filename wins in prompt text
        """
        session = BridgeSession()

        # A. Active idea.md -> Summarize
        client = HandleMessageTests._client_for(ChatResponse(content="idea.md 요약: 핵심 아이디어 정리."))
        res_a = handle_message(
            client, session,
            {
                "type": "chat", "id": 101,
                "text": "이 파일(idea.md)의 내용을 핵심 위주로 요약해줘.",
                "project_id": "jarvis-app",
                "active_file": {
                    "file_id": "f-idea",
                    "root_id": "scratch-root",
                    "path": "notes/idea.md",
                    "name": "idea.md",
                },
            },
        )
        self.assertEqual(res_a["status"], "final")
        self.assertEqual(client.calls[0]["context"]["active_file"]["path"], "notes/idea.md")

        # B. Switch to other.md -> Explain
        client_b = HandleMessageTests._client_for(ChatResponse(content="other.md 구조 설명."))
        res_b = handle_message(
            client_b, session,
            {
                "type": "chat", "id": 102,
                "text": "이 파일(other.md)의 구조와 주요 로직을 설명해줘.",
                "project_id": "jarvis-app",
                "active_file": {
                    "file_id": "f-other",
                    "root_id": "scratch-root",
                    "path": "notes/other.md",
                    "name": "other.md",
                },
            },
        )
        self.assertEqual(res_b["status"], "final")
        self.assertEqual(client_b.calls[0]["context"]["active_file"]["path"], "notes/other.md")
        self.assertNotEqual(client_b.calls[0]["context"]["active_file"]["path"], "notes/idea.md")

        # C. Clear Active File -> subsequent query carries no active_file context
        client_c = HandleMessageTests._client_for(ChatResponse(content="일반 답변입니다."))
        res_c = handle_message(
            client_c, session,
            {
                "type": "chat", "id": 103,
                "text": "오늘 날씨 어때?",
                "project_id": "jarvis-app",
                "active_file": None,
            },
        )
        self.assertEqual(res_c["status"], "final")
        self.assertIsNone(client_c.calls[0]["context"])

        # D. Dirty unsaved file -> unsaved editor buffer is stripped by sanitizer
        client_d = HandleMessageTests._client_for(ChatResponse(content="저장된 파일 기준 답변."))
        res_d = handle_message(
            client_d, session,
            {
                "type": "chat", "id": 104,
                "text": "이 파일(idea.md)의 내용을 핵심 위주로 요약해줘.",
                "project_id": "jarvis-app",
                "active_file": {
                    "file_id": "f-idea",
                    "root_id": "scratch-root",
                    "path": "notes/idea.md",
                    "name": "idea.md",
                    "content": "UNSAVED DIRTY BUFFER CONTENT - SHOULD BE IGNORED",
                },
            },
        )
        self.assertEqual(res_d["status"], "final")
        self.assertNotIn("content", client_d.calls[0]["context"]["active_file"])

        # E. Review action -> read-only response, no mutating tool calls
        client_e = HandleMessageTests._client_for(ChatResponse(content="개선점 검토 결과: 코드 가독성 개선 권장."))
        res_e = handle_message(
            client_e, session,
            {
                "type": "chat", "id": 105,
                "text": "이 파일(idea.md)에서 개선할 점이나 잠재적 버그를 검토해줘.",
                "project_id": "jarvis-app",
                "active_file": {
                    "file_id": "f-idea",
                    "root_id": "scratch-root",
                    "path": "notes/idea.md",
                    "name": "idea.md",
                },
            },
        )
        self.assertEqual(res_e["status"], "final")
        self.assertEqual(res_e["text"], "개선점 검토 결과: 코드 가독성 개선 권장.")
        self.assertNotIn("tool_call", res_e)

        # F. Explicit filename while active_file is attached
        client_f = HandleMessageTests._client_for(ChatResponse(content="specific.md에 대한 답변."))
        res_f = handle_message(
            client_f, session,
            {
                "type": "chat", "id": 106,
                "text": "notes/specific.md 파일에 대해 설명해줘.",
                "project_id": "jarvis-app",
                "active_file": {
                    "file_id": "f-idea",
                    "root_id": "scratch-root",
                    "path": "notes/idea.md",
                    "name": "idea.md",
                },
            },
        )
        self.assertEqual(res_f["status"], "final")
        sent_messages = client_f.calls[0]["messages"]
        user_text = next(m["content"] for m in sent_messages if m.get("role") == "user")
        self.assertIn("notes/specific.md", user_text)



if __name__ == "__main__":
    unittest.main()