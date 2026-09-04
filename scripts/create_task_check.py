from __future__ import annotations

import argparse
import os
import shutil
import time
from pathlib import Path

from harness.client import HarnessClient
from harness.config import PROJECT_ROOT, HarnessConfig
from harness.tools.schemas import create_task as create_task_schema_fn

"""Slice 4 check — 실 Ollama에서 create_task confirm 흐름 전체 검증 (§8.3-2·§8.4).

흐름: 사용자 요청 → 모델이 create_task 제안 → Permission Gate 차단
(awaiting_confirmation, 파일 변이 0) → confirmed_calls로 승인 → 정확히 그 call만
실행(memory/tasks.md에 기록) → tool result 기반 모델 최종 응답.

안전: 실제 앱 memory를 건드리지 않도록 **격리된 scratch memory**(data/smoke_memory,
gitignore 대상)를 HARNESS_MEMORY_DIR로 지정해 사용한다. 프로젝트 id도 smoke-test.

exit code:
  0 — 전 구간 완료 (차단 0변이 → 승인 1회 실행 → 최종 응답)
  3 — 모델이 create_task를 제안하지 않음 (tool 템플릿 문제 가능성)
  1 — 오류
"""

SMOKE_PROJECT = "smoke-test"
SMOKE_TITLE = "harness create_task 스모크 검증"
SMOKE_REASON = "confirm 흐름(차단→승인→1회 실행) 검증용 격리 task"

SCRATCH = PROJECT_ROOT / "data" / "smoke_memory"

DEFAULT_REQUEST = (
    f"지금 {SMOKE_PROJECT} 프로젝트에 새 task 하나를 생성해줘. "
    f"제목은 '{SMOKE_TITLE}', 이유는 '{SMOKE_REASON}'야. "
    "반드시 create_task 도구를 호출해서 생성해줘."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harness Slice 4 check — 실 Ollama create_task confirm 흐름 (격리 대상)."
    )
    parser.add_argument("--config", type=str, default=None, help="config YAML 경로")
    parser.add_argument("--model", type=str, default=None, help="모델 덮어쓰기")
    parser.add_argument("--text", type=str, default=DEFAULT_REQUEST, help="모델에게 보낼 요청")
    parser.add_argument("--num-predict", type=int, default=800, help="출력 토큰 상한")
    parser.add_argument(
        "--keep-scratch",
        action="store_true",
        help="검증 후 scratch memory를 지우지 않음",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    scratch = SCRATCH.resolve()
    if not str(scratch).startswith(str(PROJECT_ROOT.resolve())):
        raise SystemExit(f"scratch 경로가 워크스페이스 밖입니다: {scratch}")
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    (scratch / "projects.md").write_text(
        "# Projects\n\n## Active\n\n"
        f"- {SMOKE_PROJECT}: harness create_task 스모크 검증 프로젝트\n",
        encoding="utf-8",
    )

    os.environ["HARNESS_MEMORY_DIR"] = str(scratch)
    config = HarnessConfig.load(args.config)
    # 안전 가드: 설정 실패로 실제 앱 memory에 쓰는 사고를 원천 차단 (§8.4)
    if Path(config.memory_dir).resolve() != scratch:
        raise SystemExit(
            f"[안전 가드] memory_dir이 scratch가 아닙니다: {config.memory_dir} — "
            "환경변수 우선순위 문제. 중단합니다."
        )
    if args.model:
        config.model = args.model
    config.num_predict = args.num_predict

    client = HarnessClient(config)
    tool_schemas = [create_task_schema_fn()]
    print(f"[runtime={config.runtime} model={config.model}]")
    print(f"[격리 memory_dir={config.memory_dir}] (실제 앱 memory와 분리)")
    print(f"tool 표면: {[schema.name for schema in tool_schemas]}")

    messages = [{"role": "user", "content": args.text}]
    tasks_path = scratch / "tasks.md"

    # --- Phase 1: 모델 제안 → gate 차단, 변이 0 ---
    started = time.perf_counter()
    response = client.chat_with_tools(
        messages,
        tools=tool_schemas,
        metadata={"source": "create_task_check", "phase": 1},
    )
    phase1_s = time.perf_counter() - started

    if response.finish_reason != "awaiting_confirmation":
        print(f"\n[실패] finish_reason={response.finish_reason} (e2e {phase1_s:.1f}s)")
        print(f"모델 응답: {response.content[:300]}")
        if response.finish_reason == "stop":
            print("\n[검증 미달] 모델이 create_task를 제안하지 않았습니다.")
            return 3
        return 1

    print(f"\n=== Phase 1 (e2e {phase1_s:.1f}s): awaiting_confirmation ===")
    print(f"제안된 tool call ({len(response.tool_calls)}개):")
    for call in response.tool_calls:
        print(f"  {call.name}{call.arguments}")
    mutated = tasks_path.exists()
    print(f"변이 확인: tasks.md 존재 = {mutated} (기대: False — confirm 전 0변이)")
    if mutated:
        print("[실패] confirm 전에 상태가 변경되었습니다 — §8.4 위반")
        return 1

    # --- Phase 2: 사용자 승인 → 정확히 그 call만 실행 ---
    started = time.perf_counter()
    final = client.chat_with_tools(
        messages,
        tools=tool_schemas,
        confirmed_calls=list(response.tool_calls),
        metadata={"source": "create_task_check", "phase": 2},
    )
    phase2_s = time.perf_counter() - started

    if final.finish_reason != "stop":
        print(f"\n[실패] 승인 후 finish_reason={final.finish_reason}")
        return 1

    print(f"\n=== Phase 2 (e2e {phase2_s:.1f}s): 승인 → 실행 → 최종 응답 ===")
    print(f"최종 응답:\n{final.content}\n")

    if not tasks_path.exists():
        print("[실패] 승인했는데 tasks.md가 생성되지 않았습니다")
        return 1
    text = tasks_path.read_text(encoding="utf-8")
    created_lines = [line for line in text.splitlines() if line.startswith("- [ ]")]
    print(f"tasks.md 내용 ({len(created_lines)}개 task):")
    for line in created_lines:
        print(f"  {line}")
    if len(created_lines) != 1 or SMOKE_TITLE not in text:
        print("[실패] 기대한 1개 task가 정확히 기록되지 않았습니다")
        return 1

    print("\n[완료] 차단(0변이) → 승인(정확한 call 1회 실행) → 최종 응답 전 구간 확인.")
    if not args.keep_scratch:
        shutil.rmtree(scratch)
        print(f"(scratch 삭제: {scratch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())