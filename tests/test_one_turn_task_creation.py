from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

"""Milestone A 수용 테스트 — "할 일 하나만 추가해줘"가 한 턴에 제안에 도달한다.

V4 §16 step 8 분류에 따른 원인(prompt/schema) 수정의 행위 수용 기준:

1. 자연어 1회 요청 → awaiting_confirmation(create_task) 도달.
   - bridge가 프로젝트 문맥을 시스템 메시지로 주입해 모델이 유효한 project_id를
     발화 첫 턴에 알게 된다(추측 read 제거).
2. 승인 → 실제 create_task 실행 + trace 이벤트, 최종 답변 생성.
3. read tool이 성공한 턴의 최종 답변 grounding이 하네스 주입 지침 때문에
   꺼지지 않는다(__grounding 플래그).

모델 응답만 스크립트하고, tool 실행·검증·gate·trace는 실제 코드를 쓴다 —
스크립트는 "모델이 제안에 도달했다"는 사실을 흉내 내는 것이 아니라, 주입된
컨텍스트와 지침이 모델 요청에 실제로 실리는지(pipe through)를 검증한다.
"""

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse, ToolCall
from harness.response_grounding import ground_final_response
from harness.tools import build_default_registry
from scripts.harness_bridge import BridgeSession, handle_message


class _PolicyAwareAdapter:
    """chat_with_tools 정책 흐름을 스크립트하는 fake (RuntimeAdapter Protocol).

    - 첫 모델 호출(user 발화만 있음): create_task를 제안해 gate에 걸린다.
    - 이후 호출(tool 결과가 실림): 텍스트로 최종 답변.
    받은 ChatRequest를 전부 기록해 주입 메시지(프로젝트 문맥·continuation)가
    실제로 모델 요청에 실리는지 검증한다.
    """

    def __init__(self, proposal: tuple[str, dict]) -> None:
        self._proposal = proposal
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        saw_tool_result = any(
            message.get("role") == "tool" for message in request.messages
        )
        if saw_tool_result:
            return ChatResponse(
                content="새 작업을 만들었어.",
                finish_reason="stop",
                trace_id=request.trace_id,
            )
        name, arguments = self._proposal
        return ChatResponse(
            content="",
            tool_calls=[ToolCall(name=name, arguments=dict(arguments), id="call-0")],
            finish_reason="stop",
            trace_id=request.trace_id,
        )


def _build_fixture(tmp: Path) -> dict[str, Path]:
    mem = tmp / "mem"
    mem.mkdir()
    (mem / "projects.md").write_text(
        "# Projects\n\n## Active\n\n"
        "- graduation-thesis: 졸업논문 프로젝트\n"
        "- vocal-app: 보컬 앱 아이디어\n",
        encoding="utf-8",
    )
    (mem / "tasks.md").write_text(
        "# Tasks\n\n"
        "## graduation-thesis\n\n"
        "- [ ] t-open1: 실험 결과 정리 — LPIPS 지표 표로 정리\n"
        "- [ ] t-open2: 논문 초안 개요 작성 — 3장 구조 잡기\n",
        encoding="utf-8",
    )
    traces = tmp / "traces"
    traces.mkdir()
    return {"mem": mem, "traces": traces}


class OneTurnTaskCreationTests(unittest.TestCase):
    """자연어 1회 요청 → awaiting_confirmation(create_task) → 승인 → 실행."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _build_fixture(Path(self.tmp.name))
        self.config = HarnessConfig(
            runtime="mock",
            memory_dir=self.fixture["mem"],
            task_file=self.fixture["mem"] / "tasks.md",
            trace_dir=self.fixture["traces"],
            trace_enabled=True,
        )
        self.adapter = _PolicyAwareAdapter((
            "create_task",
            {"project_id": "graduation-thesis", "title": "표지 페이지 작성"},
        ))
        self.client = HarnessClient(
            self.config,
            adapter=self.adapter,
            tools=build_default_registry(
                memory_dir=self.config.memory_dir,
                task_file=self.config.task_file,
            ),
        )
        self.session = BridgeSession()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _handle(self, msg: dict) -> dict:
        return handle_message(self.client, self.session, msg)

    def test_clear_request_reaches_create_task_proposal_in_one_turn(self) -> None:
        """수용 기준 — 자연어 1회 요청이 제안 상태로 도달하고 유효 project_id를 쓴다."""
        response = self._handle({
            "type": "chat",
            "id": "t1",
            "text": "할 일 하나만 추가해줘. 제목은 '표지 페이지 작성'이야.",
            "project_id": "graduation-thesis",
        })
        self.assertEqual(response["status"], "awaiting_confirmation", response)
        call = response["tool_call"]
        self.assertEqual(call["name"], "create_task")
        self.assertEqual(call["arguments"].get("project_id"), "graduation-thesis")
        self.assertEqual(call["arguments"].get("title"), "표지 페이지 작성")

    def test_bridge_injects_project_context_system_message(self) -> None:
        """주입 근거 — 모델 요청에 프로젝트 문맥이 시스템 메시지로 실린다.

        원인 수정 전 bridge는 user 발화만 넘겼다. create_task의 project_id는
        registry 검증(Active 목록 대조)을 통과해야 하므로 모델은 발화 첫 턴에
        제안할 정보가 없었다(§16 step 8 — prompt/schema).
        """
        self._handle({
            "type": "chat",
            "id": "t2",
            "text": "할 일 하나만 추가해줘.",
            "project_id": "graduation-thesis",
        })
        request = self.adapter.requests[0]
        system_texts = [
            str(message.get("content") or "")
            for message in request.messages
            if message.get("role") == "system"
        ]
        self.assertTrue(
            any("graduation-thesis" in text for text in system_texts),
            f"프로젝트 문맥이 주입되지 않았다: {system_texts}",
        )
        self.assertTrue(
            any("do not ask the user" in text.lower() for text in system_texts)
        )
        # 주입은 시스템 메시지 — 사용자 발화 교체가 아니어야 한다.
        self.assertIn("할 일 하나만 추가해줘.", [
            str(message.get("content") or "")
            for message in request.messages
            if message.get("role") == "user"
        ])

    def test_confirmed_proposal_executes_and_final_answer_is_grounded(self) -> None:
        """승인 → 실제 create_task 실행(ok 이벤트) → 최종 답변. confirm도 문맥을 이어받는다."""
        proposed = self._handle({
            "type": "chat",
            "id": "t3",
            "text": "할 일 하나만 추가해줘. 제목은 '표지 페이지 작성'이야.",
            "project_id": "graduation-thesis",
        })
        self.assertEqual(proposed["status"], "awaiting_confirmation")

        approved = self._handle({
            "type": "confirm",
            "id": "t4",
            "tool_call": proposed["tool_call"],
        })
        self.assertEqual(approved["status"], "final", approved)

        tasks_text = (self.fixture["mem"] / "tasks.md").read_text(encoding="utf-8")
        self.assertIn("표지 페이지 작성", tasks_text)

        # trace에 성공한 create_task 실행이 남는다(이후 resume 신호의 원천).
        events = []
        for path in self.fixture["traces"].glob("*.json"):
            entry = json.loads(path.read_text(encoding="utf-8"))
            for item in entry.get("tool_results", []):
                events.append((
                    item["call"]["name"],
                    item["result"].get("ok"),
                    item["result"].get("requires_confirmation"),
                ))
        self.assertIn(("create_task", True, False), events)

        # 승인 후 최종 답변 호출에도 프로젝트 문맥이 실린다(모델이 id를 모르는
        # 상태로 답을 만들지 않는다). continuation 지침은 사용자 발화가 아니다.
        final_request = self.adapter.requests[-1]
        system_texts = [
            str(message.get("content") or "")
            for message in final_request.messages
            if message.get("role") == "system"
        ]
        self.assertTrue(
            any("graduation-thesis" in text for text in system_texts),
            f"confirm 경로에서 프로젝트 문맥이 유실됐다: {system_texts}",
        )
        for message in final_request.messages:
            self.assertNotIn("__grounding", message)


class ContinuationGroundingFlagTests(unittest.TestCase):
    """read 성공 턴의 grounding이 하네스 주입 지침 때문에 꺼지지 않는다 (Milestone A)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = _build_fixture(Path(self.tmp.name))
        self.config = HarnessConfig(
            runtime="mock",
            memory_dir=self.fixture["mem"],
            task_file=self.fixture["mem"] / "tasks.md",
            trace_dir=self.fixture["traces"],
            trace_enabled=True,
        )
        self.client = HarnessClient(
            self.config,
            tools=build_default_registry(
                memory_dir=self.config.memory_dir,
                task_file=self.config.task_file,
            ),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_injected_instruction_does_not_disable_grounding(self) -> None:
        """__grounding 플래그가 붙은 주입 지침은 '내부 질문'이 아니다.

        수정 전에는 continuation 지침(role=user, "tools" 단어 포함)이 user
        발화로 스캔돼 read tool이 한 번만 성공해도 grounding이 꺼졌고, 모델이
        tool 이름을 흘려도 그대로 통과했다.
        """
        messages = [
            {"role": "user", "content": "할 일 하나만 추가해줘"},
            {
                "role": "user",
                "content": "The internal reads above are complete. Now answer "
                           "the ordinary user directly. Do not mention tools.",
                "__grounding": True,
            },
        ]
        grounded = ground_final_response(
            "새 작업을 create_task로 만들었어.", messages, self.client.tools
        )
        self.assertNotIn("create_task", grounded)

    def test_real_user_internal_question_still_disables_grounding(self) -> None:
        """진짜 사용자 발화의 구현 질문은 여전히 grounding을 건드리지 않는다.

        _is_internal_question은 영어 마커(tool/api/schema/…)만 본다 — 기존 계약.
        """
        messages = [
            {"role": "user", "content": "create_task tool schema가 어떻게 돼? 디버그 중이야"},
        ]
        content = "create_task는 project_id와 title을 받는다."
        self.assertEqual(
            ground_final_response(content, messages, self.client.tools), content
        )

    def test_read_turn_keeps_grounded_final_answer_through_client(self) -> None:
        """client 라운드트립 — read 성공 후 툴 이름 흘림이 여전히 교정된다."""

        class _ReadThenLeakAdapter:
            def __init__(self) -> None:
                self.requests: list[ChatRequest] = []

            def chat(self, request: ChatRequest) -> ChatResponse:
                self.requests.append(request)
                if any(m.get("role") == "tool" for m in request.messages):
                    return ChatResponse(
                        content="새 작업을 create_task로 만들었어.",
                        finish_reason="stop",
                        trace_id=request.trace_id,
                    )
                return ChatResponse(
                    content="",
                    tool_calls=[ToolCall(
                        name="get_project_context",
                        arguments={"project_id": "graduation-thesis"},
                        id="call-0",
                    )],
                    finish_reason="stop",
                    trace_id=request.trace_id,
                )

        adapter = _ReadThenLeakAdapter()
        client = HarnessClient(
            self.config, adapter=adapter, tools=self.client.tools
        )
        response = client.chat_with_tools([
            {"role": "user", "content": "프로젝트 상황 보고 할 일 추가해줘"}
        ])
        self.assertEqual(response.finish_reason, "stop")
        self.assertNotIn("create_task", response.content or "")


if __name__ == "__main__":
    unittest.main()
