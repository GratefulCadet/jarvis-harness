from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.config import HarnessConfig
from harness.models import ChatResponse, LoopContinuation, ToolCall
from harness.client import _active_file_context as _active_file_system_line
from scripts.harness_bridge import (
    MAX_RESUMES,
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


class TracedFakeClient(FakeClient):
    """trace_id를 호출마다 지정 — 한 작업 안의 여러 trace를 흉내 낸다."""

    def __init__(self, responses, memory_dir, trace_dir, trace_ids):
        super().__init__(responses, memory_dir, trace_dir)
        self._trace_ids = list(trace_ids)

    def chat_with_tools(self, messages, **kwargs):
        response = super().chat_with_tools(messages, **kwargs)
        response.trace_id = (
            self._trace_ids.pop(0) if self._trace_ids else "trace-fake"
        )
        return response


def write_trace(trace_dir: Path, trace_id: str, tool_names: list[str]) -> None:
    """canonical trace 파일을 직접 쓴다 — bridge는 trace에서 events를 만든다."""
    results = []
    for name in tool_names:
        results.append({
            "call": {"id": f"call-{name}", "name": name, "arguments": {}},
            "result": {
                "ok": True,
                "data": {"proposal": {"proposal_id": f"e-{name}", "path": "a.md"}},
            },
        })
    (trace_dir / f"{trace_id}.json").write_text(
        json.dumps({"trace_id": trace_id, "tool_results": results}, ensure_ascii=False),
        encoding="utf-8",
    )


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

    def test_migration_carries_links_file_refs_and_pages(self) -> None:
        """M6 — 연결·파일 identity·페이지도 영구 상태로 넘어간다.

        이게 빠지면 스크래치에서 만든 Task↔File 연결이 조용히 사라진다 —
        복귀 브리핑이 "연결된 파일 없음"으로 보고하게 된다.
        """
        from scripts.harness_bridge import migrate_scratch_to_user_state

        with tempfile.TemporaryDirectory() as scratch_tmp, tempfile.TemporaryDirectory() as user_tmp:
            s_path, u_path = Path(scratch_tmp), Path(user_tmp)

            (s_path / "projects.md").write_text("# Projects", encoding="utf-8")
            (s_path / "tasks.md").write_text("# Tasks", encoding="utf-8")
            (s_path / "resource_links.json").write_text(
                '{"links": [{"id": "l1"}]}', encoding="utf-8"
            )
            (s_path / "file_refs.json").write_text(
                '{"refs": {"f-abc": {"name": "paper.txt"}}}', encoding="utf-8"
            )
            (s_path / "page_identity.json").write_text(
                '{"pages": {}}', encoding="utf-8"
            )
            # pages/는 하위 구조를 갖는다 — 재귀 복사되어야 한다.
            (s_path / "pages" / "thesis").mkdir(parents=True)
            (s_path / "pages" / "index.md").write_text("index", encoding="utf-8")
            (s_path / "pages" / "thesis" / "intro.md").write_text("intro", encoding="utf-8")

            migrated = migrate_scratch_to_user_state(s_path, u_path)

            for name in (
                "projects.md",
                "tasks.md",
                "resource_links.json",
                "file_refs.json",
                "page_identity.json",
            ):
                self.assertIn(name, migrated)
                self.assertTrue((u_path / name).is_file(), f"{name} 미이관")

            self.assertIn("pages/index.md", migrated)
            self.assertIn("pages/thesis/intro.md", migrated)
            self.assertEqual(
                (u_path / "pages" / "thesis" / "intro.md").read_text(encoding="utf-8"),
                "intro",
            )

    def test_migration_never_overwrites_existing_links_or_pages(self) -> None:
        """M6 — 사용자가 이미 가진 연결·페이지는 절대 덮어쓰지 않는다."""
        from scripts.harness_bridge import migrate_scratch_to_user_state

        with tempfile.TemporaryDirectory() as scratch_tmp, tempfile.TemporaryDirectory() as user_tmp:
            s_path, u_path = Path(scratch_tmp), Path(user_tmp)

            (s_path / "resource_links.json").write_text('{"scratch": true}', encoding="utf-8")
            (s_path / "pages").mkdir()
            (s_path / "pages" / "a.md").write_text("scratch a", encoding="utf-8")
            (s_path / "pages" / "b.md").write_text("scratch b", encoding="utf-8")

            (u_path / "resource_links.json").write_text('{"user": true}', encoding="utf-8")
            (u_path / "pages").mkdir()
            (u_path / "pages" / "a.md").write_text("user a", encoding="utf-8")

            migrated = migrate_scratch_to_user_state(s_path, u_path)

            # 기존 파일 유지, 없는 것만 추가
            self.assertNotIn("resource_links.json", migrated)
            self.assertEqual(
                (u_path / "resource_links.json").read_text(encoding="utf-8"), '{"user": true}'
            )
            self.assertNotIn("pages/a.md", migrated)
            self.assertEqual((u_path / "pages" / "a.md").read_text(encoding="utf-8"), "user a")
            self.assertIn("pages/b.md", migrated)
            self.assertEqual((u_path / "pages" / "b.md").read_text(encoding="utf-8"), "scratch b")

    def test_declared_state_files_match_module_conventions(self) -> None:
        """M6 — STATE_FILES가 각 모듈의 실제 default 경로와 어긋나지 않는다."""
        from harness.tools.project_workspaces import default_project_workspaces_file
        from harness.tools.resource_links import default_links_file
        from harness.tools.workspace import default_registry_file
        from harness.tools.workspace_roots import default_workspace_roots_file
        from scripts.harness_bridge import STATE_FILES, get_canonical_user_state_dir

        memory = get_canonical_user_state_dir()
        expected = {
            default_links_file(memory).name,
            default_registry_file(memory).name,
            default_workspace_roots_file(memory).name,
            default_project_workspaces_file(memory).name,
        }
        self.assertTrue(
            expected.issubset(set(STATE_FILES)),
            f"누락된 canonical 파일: {expected - set(STATE_FILES)}",
        )

    def test_module_docstring_does_not_claim_scratch_is_default(self) -> None:
        """M6 — docstring이 M3 이후 상태를 반대로 말하지 않는다."""
        import scripts.harness_bridge as bridge

        doc = bridge.__doc__ or ""
        self.assertNotIn("기본 memory_dir는 격리 scratch", doc)
        self.assertIn("canonical", doc)


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

    def test_tool_loop_limit_reports_the_real_reason_not_a_generic_error(self) -> None:
        # 앱에 "예상하지 못한 finish_reason"만 올리면 사용자는 한도 때문에
        # 멈췄다는 사실조차 알 수 없다. 한도 사유는 사람이 읽을 수 있는
        # 문구로, 그 문구가 대체로 알려주는 바로 그 값(회수)을 실어 보낸다.
        client = self._client(
            ChatResponse(
                content="tool 루프 한도(4회)에 도달해 중단합니다. 마지막 tool 요청(실행 안 함): create_task",
                tool_calls=[proposed_call()],
                finish_reason="tool_loop_limit",
            )
        )
        response = handle_message(
            client, BridgeSession(),
            {"type": "chat", "id": 9, "text": "계속해", "project_id": "jarvis-app"},
        )
        self.assertEqual(response["status"], "error")
        self.assertNotIn("예상하지 못한", response["error"])
        self.assertIn("한도", response["error"])
        self.assertIn("4회", response["error"])
        self.assertEqual(response["finish_reason"], "tool_loop_limit")
        # 멈췄다는 사실과 함께, 무엇을 하려다 막혔는지도 남는다.
        self.assertEqual(
            [c["name"] for c in response["tool_calls"]], ["create_task"]
        )

    def test_tool_loop_limit_falls_back_when_content_is_empty(self) -> None:
        client = self._client(
            ChatResponse(
                content="",
                tool_calls=[proposed_call()],
                finish_reason="tool_loop_limit",
            )
        )
        response = handle_message(
            client, BridgeSession(),
            {"type": "chat", "id": 10, "text": "계속해", "project_id": "jarvis-app"},
        )
        self.assertEqual(response["status"], "error")
        self.assertIn("max_turns", response["error"])

    def _limit_response(self) -> ChatResponse:
        return ChatResponse(
            content="tool 루프 한도(4회)에 도달해 중단합니다.",
            tool_calls=[proposed_call()],
            finish_reason="tool_loop_limit",
            continuation=LoopContinuation(
                messages=[{"role": "user", "content": "계속해"}],
                blocked_tool_calls=[ToolCall(name="create_task", arguments={})],
                turns=4,
                max_turns=4,
            ),
        )

    def test_resume_final_still_carries_a_proposal_made_before_the_limit(self) -> None:
        # 제안은 한 trace 안에서만 보인다. 한도에 걸린 턴이 제안까지 만든 뒤
        # 끊기고 이어간 턴이 새 trace에서 돌면, 이어간 응답에는 edit_file 결과가
        # 없다. 그래서 제안이 renderer로 올라가지 않고 사용자는 승인 수단을
        # 잃는다 — 같은 작업의 제안이므로 이어서 보여줘야 한다.
        write_trace(self.trace, "t1", ["read_file", "edit_file"])
        write_trace(self.trace, "t2", ["get_project_context"])
        client = TracedFakeClient(
            [self._limit_response(), ChatResponse(content="제안을 만들었습니다.")],
            self.memory,
            self.trace,
            ["t1", "t2"],
        )
        session = BridgeSession()
        handle_message(
            client, session,
            {"type": "chat", "id": 20, "text": "수정해줘", "project_id": "jarvis-app"},
        )
        response = handle_message(client, session, {"type": "resume", "id": 21})
        self.assertEqual(response["status"], "final")
        self.assertEqual(
            (response.get("proposal") or {}).get("proposal_id"), "e-edit_file"
        )

    def test_a_new_task_does_not_inherit_the_previous_proposal(self) -> None:
        # 새 작업에서 예전 제안 카드가 뜨면 사용자가 엉뚱한 diff를 승인한다.
        write_trace(self.trace, "t1", ["edit_file"])
        write_trace(self.trace, "t2", ["get_project_context"])
        client = TracedFakeClient(
            [
                self._limit_response(),
                ChatResponse(content="요약했습니다."),
            ],
            self.memory,
            self.trace,
            ["t1", "t2"],
        )
        session = BridgeSession()
        handle_message(
            client, session,
            {"type": "chat", "id": 22, "text": "수정해줘", "project_id": "jarvis-app"},
        )
        self.assertIsNotNone(session.last_proposal)
        response = handle_message(
            client, session,
            {"type": "chat", "id": 23, "text": "새 작업", "project_id": "jarvis-app"},
        )
        self.assertEqual(response["status"], "final")
        self.assertIsNone(response.get("proposal"))

    def test_loop_limit_offers_resume_while_budget_remains(self) -> None:
        client = self._client(self._limit_response())
        response = handle_message(
            client, BridgeSession(),
            {"type": "chat", "id": 11, "text": "계속해", "project_id": "jarvis-app"},
        )
        self.assertTrue(response["resumable"])
        self.assertEqual(response["resumes_remaining"], MAX_RESUMES)
        self.assertNotIn("이어가기를 이미", response["error"])

    def test_loop_limit_explains_the_cap_when_resume_runs_out(self) -> None:
        # 상한에 닿으면 버튼이 사라진다. 이유를 말하지 않으면 사용자는
        # "고장 났구나"로 읽고 같은 작업을 통째로 다시 시도한다 — 그게 바로
        # 상한이 막으려는 무한 반복이다. 그래서 사라지는 이유를 함께 실어 보낸다.
        #
        # 상한은 resume을 반복해야 닿는다(chat은 예산을 초기화하므로).
        client = self._client(*[self._limit_response() for _ in range(MAX_RESUMES + 1)])
        session = BridgeSession()
        first = handle_message(
            client, session,
            {"type": "chat", "id": 12, "text": "계속해", "project_id": "jarvis-app"},
        )
        self.assertTrue(first["resumable"])
        response = first
        for i in range(MAX_RESUMES):
            response = handle_message(client, session, {"type": "resume", "id": 13 + i})
            self.assertEqual(response["status"], "error")
            self.assertEqual(response["finish_reason"], "tool_loop_limit")
        self.assertFalse(response["resumable"])
        self.assertEqual(response["resumes_remaining"], 0)
        self.assertIn(f"이어가기를 이미 {MAX_RESUMES}번 사용", response["error"])
        self.assertIn("작업을 나누어", response["error"])


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