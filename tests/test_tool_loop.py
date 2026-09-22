from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse, ToolCall
from harness.tools import Tool, build_default_registry
from harness.tools.schemas import create_task as create_task_schema_fn

PROJECTS_MD = """# Projects

## Active

- local-jarvis: 로컬 우선 개인 AI 비서 기반 구축
"""

PROFILE_MD = """# Profile

## Confirmed

- 기본 응답 언어: 한국어
"""


def make_memory_dir(root: Path) -> Path:
    memory = root / "memory"
    memory.mkdir(parents=True)
    (memory / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
    (memory / "profile.md").write_text(PROFILE_MD, encoding="utf-8")
    return memory


class ScriptedFakeAdapter:
    """chat_with_tools 루프를 검증하는 scripted fake (RuntimeAdapter Protocol).

    script: 순서대로 반환할 ChatResponse 목록. 소진 후에는 텍스트로 답한다.
    requests: 받은 ChatRequest 전체를 기록해 루프가 무엇을 되돌렸는지 검증한다.
    """

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = list(script)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        response = self._script.pop(0) if self._script else ChatResponse(content="[fake-final]")
        response.trace_id = request.trace_id
        return response


def make_client(root: Path, script: list[ChatResponse]) -> tuple[HarnessClient, ScriptedFakeAdapter]:
    config = HarnessConfig(
        runtime="mock",
        trace_dir=Path(root) / "traces",
        memory_dir=make_memory_dir(Path(root)),
    )
    adapter = ScriptedFakeAdapter(script)
    registry = build_default_registry(memory_dir=config.memory_dir)
    client = HarnessClient(config, adapter=adapter, tools=registry)
    return client, adapter


def gpc_call(project_id: str = "local-jarvis") -> ToolCall:
    return ToolCall(name="get_project_context", arguments={"project_id": project_id})


def read_trace(root: Path) -> dict:
    written = list((Path(root) / "traces").glob("*.json"))
    assert len(written) == 1, f"expected 1 trace, got {len(written)}"
    return json.loads(written[0].read_text(encoding="utf-8"))


USER_MSG = [{"role": "user", "content": "local-jarvis 프로젝트 문맥을 조회해 요약해줘."}]


class ToolLoopDirectAnswerTests(unittest.TestCase):
    def test_text_answer_without_tool_calls_is_single_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, adapter = make_client(Path(temp), [
                ChatResponse(content="안녕하세요. 무엇을 도와드릴까요?"),
            ])
            response = client.chat_with_tools(list(USER_MSG))
            self.assertEqual(response.content, "안녕하세요. 무엇을 도와드릴까요?")
            self.assertEqual(response.finish_reason, "stop")
            self.assertEqual(response.tool_calls, [])
            self.assertEqual(len(adapter.requests), 1)
            self.assertEqual(adapter.requests[0].trace_id, response.trace_id)

            entry = read_trace(Path(temp))
            self.assertEqual(entry["turns"], 1)
            self.assertEqual(entry["tool_results"], [])


class ToolLoopExecuteTests(unittest.TestCase):
    def test_tool_call_then_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, adapter = make_client(Path(temp), [
                ChatResponse(content="", tool_calls=[gpc_call()]),
                ChatResponse(content="핵심 목표: 로컬 우선 개인 AI 비서 기반 구축."),
            ])
            response = client.chat_with_tools(list(USER_MSG))

            self.assertEqual(response.content, "핵심 목표: 로컬 우선 개인 AI 비서 기반 구축.")
            self.assertEqual(response.finish_reason, "stop")
            self.assertEqual(len(adapter.requests), 2)

            # 2번째 요청에 assistant(tool_calls) echo + role=tool 결과가 되돌려짐
            second = adapter.requests[1]
            roles = [message["role"] for message in second.messages]
            self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"])
            assistant = second.messages[2]
            self.assertEqual(assistant["tool_calls"][0]["name"], "get_project_context")
            tool_message = second.messages[3]
            payload = json.loads(tool_message["content"])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["project_id"], "local-jarvis")
            self.assertIn("한국어", payload["data"]["files"][0]["content"])
            self.assertTrue(second.tools, "tools schema가 계속 전달되어야 한다")

            entry = read_trace(Path(temp))
            self.assertEqual(entry["turns"], 2)
            self.assertEqual(len(entry["tool_results"]), 1)
            self.assertTrue(entry["tool_results"][0]["result"]["ok"])

    def test_multi_tool_call_batch_results_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, adapter = make_client(Path(temp), [
                ChatResponse(content="", tool_calls=[
                    gpc_call("local-jarvis"),
                    gpc_call("ghost"),  # unknown project — 실패도 tool 메시지로 되돌림
                ]),
                ChatResponse(content="두 번 조회했습니다."),
            ])
            response = client.chat_with_tools(list(USER_MSG))
            self.assertEqual(response.content, "두 번 조회했습니다.")

            second = adapter.requests[1]
            tool_messages = [m for m in second.messages if m["role"] == "tool"]
            self.assertEqual(len(tool_messages), 2)
            first_payload = json.loads(tool_messages[0]["content"])
            self.assertTrue(first_payload["ok"])
            second_payload = json.loads(tool_messages[1]["content"])
            self.assertFalse(second_payload["ok"])
            self.assertIn("ghost", second_payload["error"])

            entry = read_trace(Path(temp))
            self.assertEqual(len(entry["tool_results"]), 2)


class ToolLoopErrorFeedbackTests(unittest.TestCase):
    def test_validation_failure_is_fed_back_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, adapter = make_client(Path(temp), [
                # model이 필수 인자(project_id)를 빠뜨림
                ChatResponse(content="", tool_calls=[
                    ToolCall(name="get_project_context", arguments={}),
                ]),
                ChatResponse(content="인자를 보완해 재시도하겠습니다."),
            ])
            response = client.chat_with_tools(list(USER_MSG))
            self.assertEqual(response.content, "인자를 보완해 재시도하겠습니다.")

            second = adapter.requests[1]
            tool_message = next(m for m in second.messages if m["role"] == "tool")
            payload = json.loads(tool_message["content"])
            self.assertFalse(payload["ok"])
            self.assertIn("인자 검증 실패", payload["error"])

    def test_unknown_tool_error_is_fed_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, adapter = make_client(Path(temp), [
                ChatResponse(content="", tool_calls=[
                    ToolCall(name="delete_everything", arguments={}),
                ]),
                ChatResponse(content="사용 가능한 tool로 다시 시도하겠습니다."),
            ])
            response = client.chat_with_tools(list(USER_MSG))
            self.assertEqual(response.content, "사용 가능한 tool로 다시 시도하겠습니다.")

            second = adapter.requests[1]
            tool_message = next(m for m in second.messages if m["role"] == "tool")
            payload = json.loads(tool_message["content"])
            self.assertIn("알 수 없는 tool", payload["error"])

    def test_invalid_create_task_can_be_repaired_before_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            invalid = ToolCall(
                name="create_task",
                arguments={
                    "project_id": "current_project_id",
                    "title": "repair me",
                },
            )
            corrected = ToolCall(
                name="create_task",
                arguments={
                    "project_id": "local-jarvis",
                    "title": "repair me",
                },
            )
            client, adapter = make_client(Path(temp), [
                ChatResponse(content="", tool_calls=[invalid]),
                ChatResponse(content="", tool_calls=[corrected]),
            ])
            response = client.chat_with_tools(list(USER_MSG))

            self.assertEqual(response.finish_reason, "awaiting_confirmation")
            self.assertEqual(response.tool_calls, [corrected])
            self.assertEqual(len(adapter.requests), 2)
            first_result = next(
                message for message in adapter.requests[1].messages
                if message["role"] == "tool"
            )
            self.assertIn("알 수 없는 프로젝트", first_result["content"])
            self.assertFalse((Path(temp) / "memory" / "tasks.md").exists())


class WriteGateLoopTests(unittest.TestCase):
    def _client_with_write_handler(
        self, root: Path, script: list[ChatResponse]
    ) -> tuple[HarnessClient, ScriptedFakeAdapter, list]:
        config = HarnessConfig(
            runtime="mock",
            trace_dir=Path(root) / "traces",
            memory_dir=make_memory_dir(Path(root)),
        )
        called: list[dict] = []

        def handler(arguments: dict) -> dict:
            called.append(arguments)
            return {"created": True, "title": arguments["title"]}

        registry = build_default_registry(memory_dir=config.memory_dir)
        registry.register(
            Tool(schema=create_task_schema_fn(), kind="write", handler=handler)
        )
        adapter = ScriptedFakeAdapter(script)
        client = HarnessClient(config, adapter=adapter, tools=registry)
        return client, adapter, called

    def test_write_tool_stops_loop_awaiting_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            proposed = ToolCall(
                name="create_task",
                arguments={"project_id": "local-jarvis", "title": "Slice 2 검증"},
            )
            client, adapter, called = self._client_with_write_handler(
                Path(temp),
                [
                    ChatResponse(content="", tool_calls=[proposed]),
                    ChatResponse(content="(호출되면 안 됨)"),
                ],
            )
            response = client.chat_with_tools(list(USER_MSG))

            self.assertEqual(response.finish_reason, "awaiting_confirmation")
            self.assertIn("확인", response.content)
            self.assertEqual([call.name for call in response.tool_calls], ["create_task"])
            self.assertEqual(called, [])  # confirm 전 handler 실행 금지 (§8.4)
            self.assertEqual(len(adapter.requests), 1)  # 루프 중단 — 재호출 없음

            entry = read_trace(Path(temp))
            self.assertEqual(entry["turns"], 1)
            self.assertTrue(entry["tool_results"][0]["result"]["requires_confirmation"])

    def test_approved_write_runs_and_loop_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client, adapter, called = self._client_with_write_handler(
                Path(temp),
                [
                    ChatResponse(content="", tool_calls=[
                        ToolCall(
                            name="create_task",
                            arguments={"project_id": "local-jarvis", "title": "Slice 2 검증"},
                        ),
                    ]),
                    ChatResponse(content="task를 생성했습니다."),
                ],
            )
            response = client.chat_with_tools(
                list(USER_MSG), approve_write=True
            )

            self.assertEqual(response.finish_reason, "stop")
            self.assertEqual(response.content, "task를 생성했습니다.")
            self.assertEqual(len(called), 1)  # 승인 후 handler 실행
            self.assertEqual(len(adapter.requests), 2)

            entry = read_trace(Path(temp))
            self.assertEqual(entry["turns"], 2)
            self.assertTrue(entry["tool_results"][0]["result"]["ok"])


class ToolLoopLimitTests(unittest.TestCase):
    def test_max_turns_cap_stops_without_executing_last_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            executed: list[str] = []
            config = HarnessConfig(
                runtime="mock",
                trace_dir=Path(temp) / "traces",
                memory_dir=make_memory_dir(Path(temp)),
            )

            def handler(arguments: dict) -> dict:
                executed.append(arguments["project_id"])
                return {"ok": True}

            registry = build_default_registry(memory_dir=config.memory_dir)
            # get_project_context 실행 횟수를 세기 위해 기존 handler를 spy로 감싼다
            gpc_tool = registry.get("get_project_context")
            original_handler = gpc_tool.handler

            def spy_handler(arguments: dict) -> dict:
                executed.append(arguments["project_id"])
                return original_handler(arguments)

            registry.register(
                Tool(
                    schema=gpc_tool.schema,
                    kind=gpc_tool.kind,
                    handler=spy_handler,
                )
            )
            tool_calls = [gpc_call()]
            adapter = ScriptedFakeAdapter(
                [ChatResponse(content="", tool_calls=tool_calls)] * 5
            )
            client = HarnessClient(config, adapter=adapter, tools=registry)

            response = client.chat_with_tools(
                list(USER_MSG), max_turns=3
            )

            self.assertEqual(response.finish_reason, "tool_loop_limit")
            self.assertIn("한도", response.content)
            self.assertEqual(len(adapter.requests), 3)  # 정확히 3회만 모델 호출
            self.assertEqual(executed, ["local-jarvis", "local-jarvis"])  # 3번째 turn은 미실행
            self.assertEqual([call.name for call in response.tool_calls], ["get_project_context"])

            entry = read_trace(Path(temp))
            self.assertEqual(entry["turns"], 3)
            self.assertEqual(len(entry["tool_results"]), 2)
            self.assertEqual(entry["response"]["finish_reason"], "tool_loop_limit")


if __name__ == "__main__":
    unittest.main()
