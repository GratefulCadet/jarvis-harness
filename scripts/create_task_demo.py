from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from harness.client import HarnessClient
from harness.config import PROJECT_ROOT, HarnessConfig
from harness.tools.schemas import create_task as create_task_schema_fn

"""Slice 4 interactive demo — 실제 모델로 create_task confirm 흐름을 눈으로 확인.

    python -m scripts.create_task_demo

전체 흐름 (사용자 직접 관찰용):
    USER REQUEST
    ↓
    QWEN TOOL CALL  (모델이 create_task 제안 — 이름·인자 표시)
    ↓
    AWAITING CONFIRMATION  (finish_reason, 파일 변이 0 — 확인 메시지 표시)
    ↓
    Approve? [y/N]:   ← 여기서 멈춰 사용자 승인을 기다린다
    ↓
    TASK CREATED  (격리 scratch memory/tasks.md 내용 출력)
    ↓
    QWEN FINAL RESPONSE  (모델이 생성 완료를 인지한 최종 답변)
    ↓
    TRACE PATH  (data/traces/*.json — 전체 증거 기록)

기본적으로 **격리 scratch**(data/smoke_memory, gitignore 대상)를 사용해 실제 앱
memory를 건드리지 않는다. --keep으로 검증 후에도 scratch를 남길 수 있다.

exit code:
    0 — 흐름 완료 (y 승인: task 생성됨 / n 거부: 변이 없음)
    3 — 모델이 create_task를 제안하지 않음 (tool 템플릿 문제 가능성)
    1 — 오류
"""

SMOKE_PROJECT = "smoke-test"
SMOKE_TITLE = "데모로 추가한 테스트 할 일"
SMOKE_REASON = "create_task confirm 흐름 시연용 격리 task"

SCRATCH = PROJECT_ROOT / "data" / "smoke_memory"

DEFAULT_REQUEST = (
    f"지금 {SMOKE_PROJECT} 프로젝트(프로젝트 id: {SMOKE_PROJECT})에 테스트용 할 일을 "
    f"하나 추가해줘. 제목은 '{SMOKE_TITLE}', 이유는 '{SMOKE_REASON}'야. "
    "반드시 create_task 도구를 호출해서 생성해줘."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Slice 4 interactive demo — Qwen 제안 → 승인 대기 → 1회 실행 → 최종 응답."
    )
    parser.add_argument("--config", type=str, default=None, help="config YAML 경로")
    parser.add_argument("--model", type=str, default=None, help="모델 덮어쓰기")
    parser.add_argument("--text", type=str, default=DEFAULT_REQUEST, help="모델에게 보낼 요청")
    parser.add_argument("--num-predict", type=int, default=800, help="출력 토큰 상한")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="검증 후 scratch memory를 지우지 않음 (직접 확인용)",
    )
    return parser.parse_args()


def find_trace(config: HarnessConfig, trace_id: str | None) -> Path | None:
    if not trace_id or not config.trace_enabled:
        return None
    candidates = sorted(config.trace_dir.glob(f"*{trace_id}.json"))
    return candidates[-1] if candidates else None


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
        f"- {SMOKE_PROJECT}: harness create_task 데모용 격리 프로젝트\n",
        encoding="utf-8",
    )

    os.environ["HARNESS_MEMORY_DIR"] = str(scratch)
    config = HarnessConfig.load(args.config)
    if Path(config.memory_dir).resolve() != scratch:
        raise SystemExit(f"[안전 가드] memory_dir이 scratch가 아닙니다: {config.memory_dir}")
    if args.model:
        config.model = args.model
    config.num_predict = args.num_predict

    client = HarnessClient(config)
    tool_schemas = [create_task_schema_fn()]
    tasks_path = scratch / "tasks.md"

    print("=" * 62)
    print("JARVIS harness 데모 — create_task confirm 흐름 (실제 로컬 모델)")
    print(f"model: {config.model} · runtime: {config.runtime}")
    print(f"격리 task 저장소: {tasks_path}  (실제 앱 memory와 분리)")
    print("=" * 62)

    messages = [{"role": "user", "content": args.text}]

    # --- 1) USER REQUEST → QWEN TOOL CALL → AWAITING CONFIRMATION ---
    print("\n[1] USER REQUEST")
    print(f"    {args.text}")
    started = time.perf_counter()
    response = client.chat_with_tools(
        messages,
        tools=tool_schemas,
        metadata={"source": "create_task_demo", "phase": 1},
    )
    phase1_s = time.perf_counter() - started

    print("\n[2] QWEN TOOL CALL")
    if response.finish_reason != "awaiting_confirmation":
        print(f"    (finish_reason={response.finish_reason})")
        print(f"    모델 응답: {response.content[:200]}")
        if response.finish_reason == "stop":
            print("\n[검증 미달] 모델이 create_task를 제안하지 않았습니다.")
            return 3
        return 1
    for call in response.tool_calls:
        print(f"    {call.name}{json.dumps(call.arguments, ensure_ascii=False)}")
    print(f"    (e2e {phase1_s:.1f}s)")

    print("\n[3] AWAITING CONFIRMATION (finish_reason=awaiting_confirmation)")
    print("    Permission Gate가 실행을 막았습니다.")
    print(f"    저장 위치: {tasks_path}")
    mutated = tasks_path.exists()
    print(f"    변이 확인: tasks.md 존재 = {mutated}  ← 아직 아무것도 바뀌지 않았습니다")
    if mutated:
        print("[실패] confirm 전에 상태가 변경되었습니다 — §8.4 위반")
        return 1

    # --- 4) 승인 대기 ---
    answer = input("\n[4] 승인하시겠습니까? [y/N]: ").strip().lower()
    if answer != "y":
        print("\n    승인하지 않아 실행하지 않았습니다. 변이 없음 (tasks.md 없음).")
        if not args.keep:
            shutil.rmtree(scratch)
        return 0

    # --- 5) 승인 → 정확히 그 call만 실행 → QWEN FINAL RESPONSE ---
    print("\n[5] 승인됨 — 제안된 call만 정확히 실행합니다.")
    started = time.perf_counter()
    final = client.chat_with_tools(
        messages,
        tools=tool_schemas,
        confirmed_calls=list(response.tool_calls),
        metadata={"source": "create_task_demo", "phase": 2},
    )
    phase2_s = time.perf_counter() - started

    print("\n[6] TASK CREATED")
    if not tasks_path.exists():
        print("    [실패] 승인했는데 tasks.md가 생성되지 않았습니다")
        return 1
    text = tasks_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(("- [", "## ", "# ")):
            print(f"    {line}")

    print("\n[7] QWEN FINAL RESPONSE")
    print(f"    (e2e {phase2_s:.1f}s, finish_reason={final.finish_reason})")
    print(f"    {final.content}")

    # --- 8) TRACE ---
    trace_path = find_trace(config, final.trace_id)
    print("\n[8] TRACE")
    if trace_path:
        entry = json.loads(trace_path.read_text(encoding="utf-8"))
        print(f"    {trace_path}")
        print(f"    turns={entry.get('turns')} tool_results={len(entry.get('tool_results') or [])}")
        print(f"    response.finish_reason={entry['response']['finish_reason']}")
        print(f"    loop.confirmed_calls={json.dumps(entry.get('loop', {}).get('confirmed_calls'), ensure_ascii=False)}")
    else:
        print("    (trace 비활성 또는 파일을 찾지 못함)")

    print("\n[완료] 차단(0변이) → 승인(정확한 call 1회 실행) → 최종 응답 전 구간 확인.")
    if not args.keep:
        shutil.rmtree(scratch)
        print(f"(scratch 삭제: {scratch} — 내용을 직접 보려면 --keep 사용)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())