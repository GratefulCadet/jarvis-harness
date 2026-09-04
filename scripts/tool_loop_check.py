from __future__ import annotations

import argparse
import json
import time

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.tools.schemas import get_project_context as gpc_schema_fn

"""Slice 2 check — 실 Ollama에서 chat_with_tools tool-call 루프 검증 (§5).

모델이 get_project_context를 실제로 골라 호출하고(검증·gate·memory 조회),
tool result를 바탕으로 최종 텍스트 답변까지 완결되는지 확인한다.

exit code:
  0 — 루프가 최종 답변으로 정상 완료 (tool 사용 여부와 무관)
  3 — 완료는 했으나 모델이 tool을 한 번도 호출하지 않음 (tool 템플릿 문제 가능성)
  1 — 오류
"""

DEFAULT_QUESTION = (
    "지금 local-jarvis 프로젝트의 작업을 시작하려고 해. 먼저 get_project_context "
    "도구로 프로젝트 문맥을 조회한 뒤, 그 내용을 바탕으로 이 프로젝트의 핵심 목표를 "
    "세 문장 이내로 요약해줘."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harness Slice 2 check — 실 Ollama tool-call 루프 (get_project_context)."
    )
    parser.add_argument("--config", type=str, default=None, help="config YAML 경로")
    parser.add_argument("--model", type=str, default=None, help="모델 덮어쓰기")
    parser.add_argument("--project", type=str, default="local-jarvis", help="조회할 프로젝트 id")
    parser.add_argument(
        "--text",
        type=str,
        default=DEFAULT_QUESTION,
        help="모델에게 보낼 질문 (기본: tool 사용 유도 문장)",
    )
    parser.add_argument("--num-predict", type=int, default=800, help="출력 토큰 상한")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    config = HarnessConfig.load(args.config)
    if args.model:
        config.model = args.model
    config.num_predict = args.num_predict  # 요약 답변 + tool JSON 여유

    client = HarnessClient(config)
    # 검증 초점을 좁히기 위해 tool 표면을 get_project_context 하나로 제한
    tool_schemas = [gpc_schema_fn()]
    print(f"[runtime={config.runtime} model={config.model} memory_dir={config.memory_dir}]")
    print(f"tool 표면: {[schema.name for schema in tool_schemas]}")

    messages = [
        {
            "role": "user",
            "content": (
                f"{args.text}\n\n"
                f"(참고: 조회할 프로젝트 id는 {args.project!r}입니다.)"
            ),
        }
    ]

    started = time.perf_counter()
    response = client.chat_with_tools(
        messages,
        tools=tool_schemas,
        metadata={"source": "tool_loop_check"},
    )
    e2e_s = time.perf_counter() - started

    print(f"\n=== finish_reason: {response.finish_reason} (e2e {e2e_s:.1f}s) ===")
    print(f"최종 응답:\n{response.content}\n")

    if response.finish_reason != "stop":
        print(f"[경고] 루프가 최종 답변 없이 중단됨: {response.finish_reason}")
        return 1

    # trace에서 tool 사용 여부 확인
    trace_path = None
    if response.trace_id and config.trace_enabled:
        candidates = sorted(config.trace_dir.glob(f"*{response.trace_id}.json"))
        if candidates:
            trace_path = candidates[-1]
    tool_used = False
    if trace_path:
        entry = json.loads(trace_path.read_text(encoding="utf-8"))
        tool_used = bool(entry.get("tool_results"))
        print(f"trace: {trace_path} (turns={entry.get('turns')}, tool_results={len(entry.get('tool_results') or [])})")

    if not tool_used:
        print(
            "\n[검증 미달] 모델이 tool을 호출하지 않았습니다. "
            "커스텀 Modelfile 템플릿이 tool 형식을 지원하지 않을 수 있습니다 — "
            "--model qwen3:8b 또는 qwen2.5:7b로 재시도해 보세요."
        )
        return 3
    print("\n[완료] 모델이 tool을 호출하고 tool result 기반 최종 답변을 생성했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
