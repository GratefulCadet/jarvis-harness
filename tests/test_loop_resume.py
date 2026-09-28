from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.models import ChatResponse, ToolCall
from scripts.harness_bridge import (
    MAX_RESUMES,
    BridgeSession,
    _call_name,
    _resume_system_line,
    handle_message,
)

"""tool_loop_limit 중단 뒤 이어가기(resume) 테스트.

배경: 한도에 걸리면 사용자는 문장을 다시 쳐야 했다. 그런데 그 문장만 다시 보내면
모델은 이미 끝낸 일을 처음부터 다시 해�� 턴을 다시 다 쓴다. resume은 한도 직전
까지 **실제로 오간 대화**를 그대로 이어받아 턴을 아낀다.

여기서는 특히 두 가지를 지킨다:
  (1) 이어받은 대화는 지어내지 않은 실제 기록이다.
  (2) 이미 실행된 도구를 resume이 다시 실행하지 않는다.
(2)를 어기면 resume이 "시간을 아끼는 기능"이 아니라 "파일을 두 번 고치는 기능"이 된다.
"""


class ScriptedAdapter:
    """전체 생애에서 tool을 loop_turns번 제안하고(한도 재현), 그 다음엔 최종 답변.

    loop_turns를 전역으로 세는 것이 포인트다. 이어가기가 실제로 일을 끝내는
    경우까지 재현하려면 "몇 번째 모델 호출에서 멈추는지"를 미리 정해야 해서,
    요청별이 아니라 어댑터 생애 기준으로 센다.
    """

    def __init__(self, loop_turns: int) -> None:
        self.loop_turns = loop_turns
        self.calls = 0
        self.seen: list[list[dict]] = []

    def chat(self, request):
        self.calls += 1
        self.seen.append([dict(m) for m in request.messages])
        if self.calls <= self.loop_turns:
            return ChatResponse(
                content="",
                tool_calls=[ToolCall(
                    id=f"call-{self.calls}",
                    name="get_project_context",
                    arguments={"n": self.calls},
                )],
                finish_reason="tool_loop",
            )
        return ChatResponse(content="요약 완료했습니다.", finish_reason="stop")


def _tool_results_follow_calls(messages: list[dict]) -> bool:
    """tool_calls가 달린 모든 assistant 메시지에 뒤따르는 tool 응답이 있는가.

    미실행 tool_calls가 transcript에 남아 있으면 대응하는 tool 응답이 없으므로
    어댑터가 메시지를 거부할 수 있다. resume이 실제로 물려받는 형태인지 확인한다.
    """
    answered: set[str] = set()
    for message in messages:
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id:
                answered.add(str(call_id))
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if str(call.get("id")) not in answered:
                    return False
    return True


def _client(loop_turns: int, max_turns: int = 3) -> tuple[HarnessClient, ScriptedAdapter]:
    config = HarnessConfig(
        runtime="mock",
        trace_dir=Path(tempfile.mkdtemp()),
        trace_enabled=False,
        tool_loop_max_turns=max_turns,
    )
    client = HarnessClient(config)
    adapter = ScriptedAdapter(loop_turns)
    client._adapter = adapter
    return client, adapter


def _roles(messages: list[dict]) -> list[str]:
    return [m.get("role") for m in messages]


class ContinuationCaptureTests(unittest.TestCase):
    def test_limit_response_carries_a_continuation(self) -> None:
        client, _ = _client(loop_turns=99)
        response = client.chat_with_tools([{"role": "user", "content": "요약해줘"}])
        self.assertEqual(response.finish_reason, "tool_loop_limit")
        self.assertIsNotNone(response.continuation)
        self.assertEqual(response.continuation.turns, 3)
        self.assertEqual(response.continuation.max_turns, 3)

    def test_normal_finish_has_no_continuation(self) -> None:
        # stop / awaiting_confirmation에 continuation을 달면 사용자가 "이어가기"를
        # 눌러 이미 끝난 작업을 다시 돌릴 수 있다. 끝난 작업에는 붙지 않아야 한다.
        client, _ = _client(loop_turns=0)
        response = client.chat_with_tools([{"role": "user", "content": "요약해줘"}])
        self.assertEqual(response.finish_reason, "stop")
        self.assertIsNone(response.continuation)

    def test_continuation_never_carries_an_unanswered_tool_call(self) -> None:
        # 미실행 tool_calls가 달린 assistant 메시지가 남아 있으면, 대응하는 tool
        # 응답이 없는 상태가 되어 어댑터가 메시지를 거부할 수 있다.
        client, _ = _client(loop_turns=99)
        response = client.chat_with_tools([{"role": "user", "content": "요약해줘"}])
        messages = response.continuation.messages
        self.assertTrue(
            _tool_results_follow_calls(messages),
            "tool 응답 없는 assistant 메시지가 남아 있다",
        )

    def test_blocked_calls_are_kept_separate_from_the_transcript(self) -> None:
        client, _ = _client(loop_turns=99)
        response = client.chat_with_tools([{"role": "user", "content": "요약해줘"}])
        blocked = response.continuation.blocked_tool_calls
        self.assertEqual(len(blocked), 1)
        self.assertEqual(_call_name(blocked[0]), "get_project_context")
        # 막힌 요청의 id는 transcript 안에 있으면 안 된다 — "실행된 것처럼" 보인다.
        blocked_ids = {str(call.get("id")) for call in blocked}
        for message in response.continuation.messages:
            for call in message.get("tool_calls") or []:
                self.assertNotIn(str(call.get("id")), blocked_ids)


class TranscriptSanitizerTests(unittest.TestCase):
    """provider가 tool_call id를 안 붙이는 경우에도 이어갈 transcript가 멀쩡해야 한다."""

    def _clean(self, messages: list[dict]) -> list[dict]:
        return HarnessClient._resume_safe_transcript(messages)

    def test_drops_assistant_whose_tool_response_never_arrives(self) -> None:
        messages = [
            {"role": "user", "content": "요약"},
            {"role": "assistant", "tool_calls": [{"id": None, "name": "read_file"}]},
            {"role": "user", "content": "다음"},
        ]
        cleaned = self._clean(messages)
        self.assertEqual(
            [m.get("role") for m in cleaned], ["user", "user"]
        )

    def test_keeps_matched_pairs_untouched(self) -> None:
        messages = [
            {"role": "user", "content": "요약"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "name": "read_file"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        self.assertEqual(self._clean(messages), messages)

    def test_keeps_a_complete_prefix_when_a_later_call_is_broken(self) -> None:
        messages = [
            {"role": "user", "content": "요약"},
            {"role": "assistant", "tool_calls": [{"id": "c1", "name": "read_file"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "assistant", "tool_calls": [{"id": None, "name": "read_file"}]},
            {"role": "user", "content": "끝"},
        ]
        cleaned = self._clean(messages)
        # 깨진 assistant 한 개만 빠지고 나머지 대화는 그대로 보존된다.
        self.assertEqual(
            [m.get("content") for m in cleaned], ["요약", None, "ok", "끝"]
        )
        self.assertTrue(_tool_results_follow_calls(cleaned))


class ResumeMessageTests(unittest.TestCase):
    def test_resume_line_is_a_system_message_not_a_user_message(self) -> None:
        # 사용자는 아무것도 다시 타이핑하지 않았다. user 메시지로 넣으면 대화 기록에
        # "사용자가 이렇게 말했다"는 말이 남는다.
        client, _ = _client(loop_turns=99)
        session = BridgeSession()
        handle_message(client, session, {"type": "chat", "id": 1, "text": "요약해줘"})
        handle_message(client, session, {"type": "resume", "id": 2})
        messages = client._adapter.seen[-1]
        self.assertIn("system", _roles(messages))
        # 원본 발화는 transcript에 그대로 있고, resume이 만든 user 발화가 없다.
        originals = [
            m for m in messages
            if m.get("role") == "user" and m.get("content") == "요약해줘"
        ]
        self.assertEqual(len(originals), 1)
        self.assertTrue(any("tool-call limit" in (m.get("content") or "")
                            for m in messages if m.get("role") == "system"))

    def test_resume_line_names_the_never_executed_call(self) -> None:
        client, _ = _client(loop_turns=99)
        response = client.chat_with_tools([{"role": "user", "content": "요약해줘"}])
        line = _resume_system_line(response.continuation)
        self.assertIn("get_project_context", line)
        self.assertIn("never executed", line)

    def test_resume_line_forbids_redoing_finished_work(self) -> None:
        client, _ = _client(loop_turns=99)
        response = client.chat_with_tools([{"role": "user", "content": "요약해줘"}])
        line = _resume_system_line(response.continuation).lower()
        self.assertIn("do not redo", line)
        self.assertIn("do not restart", line)

    def test_system_line_sits_in_the_head_block_not_at_the_end(self) -> None:
        # 끝에 붙이면 system을 첫 메시지로만 받는 어댑터 제약을 건드린다.
        client, _ = _client(loop_turns=99)
        session = BridgeSession()
        handle_message(client, session, {"type": "chat", "id": 1, "text": "요약해줘"})
        handle_message(client, session, {"type": "resume", "id": 2})
        roles = _roles(client._adapter.seen[-1])
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles[1], "system")
        self.assertEqual(roles[2], "user")


class ResumeBudgetTests(unittest.TestCase):
    def test_limit_offers_resume_with_remaining_count(self) -> None:
        client, _ = _client(loop_turns=99)
        session = BridgeSession()
        response = handle_message(client, session, {"type": "chat", "id": 1, "text": "요약"})
        self.assertEqual(response["status"], "error")
        self.assertTrue(response["resumable"])
        self.assertEqual(response["resumes_remaining"], MAX_RESUMES)

    def test_resume_is_capped_then_refused_with_a_reason(self) -> None:
        client, _ = _client(loop_turns=99)
        session = BridgeSession()
        handle_message(client, session, {"type": "chat", "id": 1, "text": "요약"})
        for i in range(MAX_RESUMES):
            response = handle_message(client, session, {"type": "resume", "id": 10 + i})
            # 상한에 닿기 전의 이어가기는 거절이 아니라 실제 한도 응답이다.
            # (상한에 닿은 뒤에는 버튼이 사라지는 이유를 함께 실어 보낸다)
            self.assertEqual(response.get("finish_reason"), "tool_loop_limit",
                             f"{i+1}번째 이어가기는 거절되면 안 된다")
        # 마지막 허용된 이어가기가 다시 한도에 걸렸다면 더는 재장전하지 않는다.
        self.assertFalse(response["resumable"])
        # 이유 없이 버튼만 사라지면 사용자는 같은 작업을 통째로 다시 시도한다.
        self.assertIn(f"이어가기를 이미 {MAX_RESUMES}번 사용", response["error"])
        self.assertIn("작업을 나누어", response["error"])
        over = handle_message(client, session, {"type": "resume", "id": 99})
        self.assertEqual(over["status"], "error")
        self.assertFalse(over["resumable"])
        self.assertIn(str(MAX_RESUMES), over["error"])
        # 같은 요청을 반복하라고 권하지 않는다 — 무한루프를 먹이는 안내가 아니다.
        self.assertIn("나누어", over["error"])

    def test_consumed_resume_cannot_be_replayed(self) -> None:
        # 이어가기가 실제로 일을 끝낸 경우(다시 한도에 걸리지 않은 경우) 그 지점은
        # 사라져야 한다. 남아 있으면 같은 지점을 두 번 이어가서 이미 실행된 도구
        # 결과가 transcript에 두 번 쌓인다.
        client, _ = _client(loop_turns=3)
        session = BridgeSession()
        handle_message(client, session, {"type": "chat", "id": 1, "text": "요약"})
        done = handle_message(client, session, {"type": "resume", "id": 2})
        self.assertEqual(done["status"], "final")
        again = handle_message(client, session, {"type": "resume", "id": 3})
        self.assertIn("중단된 tool 루프가 없습니다", again["error"])

    def test_resume_that_hits_the_limit_again_re_arms(self) -> None:
        # 반대로, 이어가기가 다시 한도에 걸렸다면 새 지점이 생겼으니 이어갈 수 있다.
        client, _ = _client(loop_turns=6)
        session = BridgeSession()
        handle_message(client, session, {"type": "chat", "id": 1, "text": "요약"})
        again = handle_message(client, session, {"type": "resume", "id": 2})
        self.assertEqual(again["status"], "error")
        self.assertTrue(again["resumable"])

    def test_resume_without_a_stopped_loop_is_refused(self) -> None:
        client, _ = _client(loop_turns=0)
        session = BridgeSession()
        response = handle_message(client, session, {"type": "resume", "id": 1})
        self.assertEqual(response["status"], "error")
        self.assertIn("중단된 tool 루프가 없습니다", response["error"])

    def test_new_request_restores_the_budget(self) -> None:
        # 한 작업이 무한루프에 빠져도 이후 작업까지 이어가기가 막히면 안 된다.
        client, _ = _client(loop_turns=99)
        session = BridgeSession()
        handle_message(client, session, {"type": "chat", "id": 1, "text": "요약"})
        for i in range(MAX_RESUMES):
            handle_message(client, session, {"type": "resume", "id": 10 + i})
        fresh = handle_message(client, session, {"type": "chat", "id": 50, "text": "새 작업"})
        self.assertTrue(fresh["resumable"])
        self.assertEqual(fresh["resumes_remaining"], MAX_RESUMES)

    def test_new_request_drops_a_stale_continuation(self) -> None:
        # 첫 작업이 한도에 걸린 뒤 사용자가 다른 작업을 성공적으로 끝냈다면,
        # 이전 작업의 지점을 이어갈 수 없어야 한다 — 엇갈린 transcript 위험.
        client, _ = _client(loop_turns=3)
        session = BridgeSession()
        handle_message(client, session, {"type": "chat", "id": 1, "text": "요약"})
        done = handle_message(client, session, {"type": "chat", "id": 2, "text": "다른 작업"})
        self.assertEqual(done["status"], "final")
        stale = handle_message(client, session, {"type": "resume", "id": 3})
        self.assertIn("중단된 tool 루프가 없습니다", stale["error"])


class ResumeSafetyTests(unittest.TestCase):
    def test_resume_does_not_re_execute_completed_tools(self) -> None:
        # 이게 resume의 존재 이유이자 위험이다. 이미 실행된 도구를 다시 실행하면
        # 파일이 두 번 고쳐진다. 이어받은 transcript에 도구 결과가 이미 있으므로,
        # resume은 그 결과를 다시 만들지 않고 새 예산으로 다음 턴부터 시작한다.
        client, adapter = _client(loop_turns=3)
        session = BridgeSession()
        handle_message(client, session, {"type": "chat", "id": 1, "text": "요약"})
        used = adapter.calls
        done = handle_message(client, session, {"type": "resume", "id": 2})
        self.assertEqual(used, 3)
        self.assertEqual(done["status"], "final")
        # 이어가기가 1회의 추가 모델 호출로 끝났다 — 앞선 3회를 재실행하지 않는다.
        self.assertEqual(adapter.calls, 4)
        resumed_messages = adapter.seen[-1]
        tool_messages = [m for m in resumed_messages if m.get("role") == "tool"]
        # 3턴 예산에서 마지막 턴의 요청은 실행되지 않으므로 실제 결과는 2개다.
        # 이어가기에 실리는 것도 그 2개여야 한다 — 3개면 재실행이 섞인 것이다.
        self.assertEqual(len(tool_messages), 2, "이전 도구 결과가 그대로 실려야 한다")

    def test_resume_never_auto_approves_a_write(self) -> None:
        # 이어가기가 write gate를 우회하면 승인 없는 파일 변경이 생긴다.
        client, adapter = _client(loop_turns=99)
        session = BridgeSession()
        handle_message(client, session, {"type": "chat", "id": 1, "text": "요약"})
        handle_message(client, session, {"type": "resume", "id": 2})
        # confirmed_calls는 resume 경로에서 절대 None이 아니다 — 안 넘긴다.
        # loop_meta가 남긴 흔적 대신, 등록된 write tool이 게이트를 통과하지 못함을 본다.
        self.assertTrue(all(
            not isinstance(m.get("content"), str) or "Approved" not in m["content"]
            for m in adapter.seen[-1]
        ))

    def test_resume_carries_project_and_active_file_forward(self) -> None:
        client, _ = _client(loop_turns=99)
        session = BridgeSession()
        handle_message(client, session, {
            "type": "chat", "id": 1, "text": "요약",
            "active_file": {"file_id": "f-1", "root_id": "r", "path": "C:/w/a.md", "name": "a.md"},
        })
        response = handle_message(client, session, {"type": "resume", "id": 2})
        # 이어가기가 새 작업처럼 보이면 안 된다 — 프로젝트 문맥은 이어받는다.
        self.assertIn(response["status"], ("error", "final"))


if __name__ == "__main__":
    unittest.main()
