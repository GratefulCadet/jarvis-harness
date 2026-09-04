from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.models import ChatRequest, ChatResponse, ToolCall
from harness.tools import build_default_registry
from harness.tools.task_store import TaskStore

"""Slice 5 — task 수명주기 전체 흐름 검증 (결정적, fake adapter).

요구사항 매핑 (Task 2 — lifecycle smoke):
- Qwen(모델)이 기존 task를 list_current_tasks로 읽는다            (phase 1 turn 1)
- Qwen이 create_task를 제안 → Permission Gate awaiting_confirmation,
  변이 0 (기존 tasks.md 불변)                                     (phase 1)
- 명시적 승인(confirmed_calls) → 제안된 call만 정확히 1회 실행     (phase 2)
- 새 task가 저장소에 존재                                          (phase 2 파일 검사)
- Qwen이 list_current_tasks로 task를 다시 읽고 근거 있는 답변      (phase 3)
- trace가 각 단계의 호출·결과를 담는다                             (trace 검사)
"""

PROJECTS_MD = """# Projects

## Active

- smoke-test: 수명주기 검증 프로젝트
"""


def make_memory(root: Path) -> Path:
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "projects.md").write_text(PROJECTS_MD, encoding="utf-8")
    return memory


def make_client(root: Path, script: list[ChatResponse]) -> tuple[HarnessClient, "ScriptedFakeAdapter"]:
    config = HarnessConfig(
        runtime="mock",
        trace_dir=Path(root) / "traces",
        memory_dir=make_memory(root),
    )
    adapter = ScriptedFakeAdapter(script)
    registry = build_default_registry(memory_dir=config.memory_dir)
    return HarnessClient(config, adapter=adapter, tools=registry), adapter


def read_trace_tool_results(root: Path, source: str) -> dict[int, list[dict]]:
    """source·phase별 각 trace의 tool_results를 phase 키로 반환.

    세 phase가 같은 초에 실행될 수 있어 파일명 정렬이 아니라 metadata의
    phase 번호로 구분한다.
    """
    traces: dict[int, list[dict]] = {}
    for path in (Path(root) / "traces").glob("*.json"):
        entry = json.loads(path.read_text(encoding="utf-8"))
        metadata = entry.get("request", {}).get("metadata", {})
        if metadata.get("source") != source:
            continue
        traces[int(metadata.get("phase", 0))] = entry.get("tool_results", [])
    return traces


USER_PLAN = [{"role": "user", "content": "smoke-test 프로젝트의 현재 task를 확인하고 새 task를 만들어줘."}]
USER_READBACK = [{"role": "user", "content": "smoke-test 프로젝트의 현재 task 목록을 알려줘."}]


class LifecycleFlowTests(unittest.TestCase):
    def _seed(self, root: Path) -> None:
        memory = make_memory(root)
        store = TaskStore(memory / "tasks.md", memory_dir=memory)
        store.create("smoke-test", "기존 task")

    def test_full_lifecycle_zero_mutation_then_exact_creation_then_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._seed(root)
            tasks = root / "memory" / "tasks.md"
            before = tasks.read_bytes()

            proposed = ToolCall(
                name="create_task",
                arguments={
                    "project_id": "smoke-test",
                    "title": "수명주기 검증 task",
                    "reason": "lifecycle 확인",
                },
            )
            list_call = ToolCall(
                name="list_current_tasks",
                arguments={"project_id": "smoke-test"},
            )

            # --- Phase 1: 읽기 → 제안 → gate 차단, 변이 0 ---
            client1, _ = make_client(
                root,
                [
                    ChatResponse(content="", tool_calls=[list_call]),
                    ChatResponse(content="", tool_calls=[proposed]),
                ],
            )
            response1 = client1.chat_with_tools(
                list(USER_PLAN), metadata={"source": "lifecycle", "phase": 1}
            )

            self.assertEqual(response1.finish_reason, "awaiting_confirmation")
            self.assertEqual(response1.tool_calls, [proposed])
            self.assertEqual(tasks.read_bytes(), before, "승인 전 변이 금지 (0변이)")

            # --- Phase 2: 승인 → 정확히 제안된 call만 1회 실행 ---
            client2, _ = make_client(
                root,
                [ChatResponse(content="새 task를 생성했습니다.")],
            )
            response2 = client2.chat_with_tools(
                list(USER_PLAN),
                confirmed_calls=[proposed],
                metadata={"source": "lifecycle", "phase": 2},
            )

            self.assertEqual(response2.finish_reason, "stop")
            text = tasks.read_text(encoding="utf-8")
            self.assertEqual(text.count("- [ ] t-"), 2, "기존 1 + 신규 1")
            self.assertIn("수명주기 검증 task", text)

            # --- Phase 3: read-back — list_current_tasks로 다시 읽어 근거 답변 ---
            client3, _ = make_client(
                root,
                [
                    ChatResponse(content="", tool_calls=[list_call]),
                    ChatResponse(content="현재 진행 중 task는 2개입니다."),
                ],
            )
            response3 = client3.chat_with_tools(
                list(USER_READBACK), metadata={"source": "lifecycle", "phase": 3}
            )

            self.assertEqual(response3.finish_reason, "stop")
            self.assertEqual(response3.content, "현재 진행 중 task는 2개입니다.")

            # --- trace 검사: phase 1에 list 결과 + gate 차단, phase 2에 생성, phase 3에 read-back ---
            results = read_trace_tool_results(root, "lifecycle")
            self.assertEqual(sorted(results), [1, 2, 3])
            phase1_names = [item["call"]["name"] for item in results[1]]
            self.assertEqual(phase1_names, ["list_current_tasks", "create_task"])
            self.assertTrue(results[1][0]["result"]["ok"])
            self.assertEqual(results[1][0]["result"]["data"]["count"], 1)
            self.assertTrue(
                results[1][1]["result"]["requires_confirmation"],
                "create_task는 gate에서 차단되어야 한다",
            )
            self.assertEqual(results[1][1]["result"]["data"], None, "차단 시 실행 없음")
            self.assertEqual(
                [item["call"]["name"] for item in results[2]], ["create_task"]
            )
            self.assertTrue(results[2][0]["result"]["ok"])
            self.assertTrue(results[2][0]["result"]["data"]["created"])
            self.assertEqual(
                [item["call"]["name"] for item in results[3]], ["list_current_tasks"]
            )
            self.assertEqual(results[3][0]["result"]["data"]["count"], 2)

    def test_rejected_proposal_never_mutates_and_readback_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._seed(root)
            tasks = root / "memory" / "tasks.md"
            before = tasks.read_bytes()

            proposed = ToolCall(
                name="create_task",
                arguments={
                    "project_id": "smoke-test",
                    "title": "승인 안 된 task",
                    "reason": "거부 확인",
                },
            )
            list_call = ToolCall(
                name="list_current_tasks",
                arguments={"project_id": "smoke-test"},
            )

            # 승인하지 않으면 아무 일도 일어나지 않는다 — 이후 read-only 조회는 그대로 동작
            client, _ = make_client(
                root,
                [
                    ChatResponse(content="", tool_calls=[proposed]),
                    ChatResponse(content="", tool_calls=[list_call]),
                    ChatResponse(content="기존 task 1개만 진행 중입니다."),
                ],
            )
            first = client.chat_with_tools(
                list(USER_PLAN), metadata={"source": "lifecycle_reject", "phase": 1}
            )
            self.assertEqual(first.finish_reason, "awaiting_confirmation")
            self.assertEqual(tasks.read_bytes(), before)

            second = client.chat_with_tools(
                list(USER_READBACK), metadata={"source": "lifecycle_reject", "phase": 2}
            )
            self.assertEqual(second.finish_reason, "stop")
            self.assertEqual(second.content, "기존 task 1개만 진행 중입니다.")
            self.assertEqual(tasks.read_bytes(), before, "거부 후에도 변이 없음")


class ScriptedFakeAdapter:
    """chat_with_tools 루프 검증용 scripted fake (RuntimeAdapter Protocol)."""

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = list(script)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        response = self._script.pop(0) if self._script else ChatResponse(content="[fake-final]")
        response.trace_id = request.trace_id
        return response


if __name__ == "__main__":
    unittest.main()
