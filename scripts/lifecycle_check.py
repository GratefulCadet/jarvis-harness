from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from harness.client import HarnessClient
from harness.config import PROJECT_ROOT, HarnessConfig
from harness.models import ToolCall
from harness.tools.schemas import (
    create_task as create_task_schema_fn,
    list_current_tasks as list_current_tasks_schema_fn,
)

"""Slice 5 lifecycle check — 실 Ollama 전체 수명주기 스모크 (격리 대상).

USER REQUEST
→ Qwen이 기존 task를 list_current_tasks로 읽음 (선택)
→ Qwen이 create_task를 제안
→ Permission Gate: awaiting_confirmation, 변이 0
→ 승인(confirmed_calls) → 제안된 call만 정확히 1회 실행
→ 새 task 존재
→ Qwen이 list_current_tasks로 다시 읽고 근거 있는 최종 응답
→ TRACE 경로 표시

이 스크립트가 곧 Task 4의 개발자 관찰면(transcript)이다.
모든 상태 변경은 격리 scratch memory(data/smoke_memory_life)에서만 일어난다.

exit code:
  0 — 전 구간 통과
  3 — Phase 1에서 모델이 create_task를 제안하지 않음
  1 — 오류 / 검증 미달
"""

SMOKE_PROJECT = "smoke-life"
SEED_TITLE = "기존 task"
NEW_TITLE = "수명주기 검증 task"
NEW_REASON = "lifecycle smoke"
SCRATCH = PROJECT_ROOT / "data" / "smoke_memory_life"

PHASE1_REQUEST = (
    f"지금 {SMOKE_PROJECT} 프로젝트에서 task 작업을 진행하려고 해. "
    f"1) 먼저 list_current_tasks 도구로 현재 진행 중인 task 목록을 확인하고, "
    f"2) 그다음 create_task 도구로 새 task 하나를 생성해줘. "
    f"새 task 제목은 '{NEW_TITLE}', 이유는 '{NEW_REASON}'로 해줘. "
    "두 도구를 모두 실제로 호출해야 해."
)

PHASE3_REQUEST = (
    f"방금 새 task를 추가했어. 지금 {SMOKE_PROJECT} 프로젝트에서 현재 진행 중인 "
    "task 목록을 list_current_tasks 도구로 다시 조회해서, 추가된 task가 포함된 "
    "전체 목록을 정리해서 알려줘."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harness Slice 5 lifecycle check — 실 Ollama 전체 수명주기 (격리 대상)."
    )
    parser.add_argument("--config", type=str, default=None, help="config YAML 경로")
    parser.add_argument("--model", type=str, default=None, help="모델 덮어쓰기")
    parser.add_argument("--num-predict", type=int, default=1400, help="출력 토큰 상한")
    parser.add_argument(
        "--keep-scratch",
        action="store_true",
        help="검증 후 scratch memory를 지우지 않음",
    )
    return parser.parse_args()


def _open_entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("- [ ] t-")]


def _traces_for(config: HarnessConfig, source: str) -> dict[int, Path]:
    found: dict[int, Path] = {}
    for path in config.trace_dir.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        metadata = entry.get("request", {}).get("metadata", {})
        if metadata.get("source") == source:
            found[int(metadata.get("phase", 0))] = path
    return found


def _tool_result_names(trace_path: Path) -> list[str]:
    entry = json.loads(trace_path.read_text(encoding="utf-8"))
    return [item["call"]["name"] for item in entry.get("tool_results", [])]


def _result(trace_path: Path, index: int) -> dict:
    entry = json.loads(trace_path.read_text(encoding="utf-8"))
    return entry.get("tool_results", [])[index]["result"]


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
        f"- {SMOKE_PROJECT}: 수명주기 스모크 검증 프로젝트\n",
        encoding="utf-8",
    )
    tasks_path = scratch / "tasks.md"
    import hashlib

    seed_id = f"t-{hashlib.sha1(f'{SMOKE_PROJECT}|{SEED_TITLE}|seed'.encode('utf-8')).hexdigest()[:12]}"
    tasks_path.write_text(
        "# Tasks\n\n"
        f"## {SMOKE_PROJECT}\n\n"
        f"- [ ] {seed_id}: {SEED_TITLE} — seed\n",
        encoding="utf-8",
    )
    before = tasks_path.read_bytes()

    os.environ["HARNESS_MEMORY_DIR"] = str(scratch)
    config = HarnessConfig.load(args.config)
    if Path(config.memory_dir).resolve() != scratch:
        raise SystemExit(
            f"[안전 가드] memory_dir이 scratch가 아닙니다: {config.memory_dir} — 중단합니다."
        )
    if args.model:
        config.model = args.model
    config.num_predict = args.num_predict

    client = HarnessClient(config)
    tool_schemas = [list_current_tasks_schema_fn(), create_task_schema_fn()]
    print(f"[runtime={config.runtime} model={config.model}]")
    print(f"[격리 memory_dir={config.memory_dir}] (실제 앱 memory와 분리)")
    print(f"tool 표면: {[schema.name for schema in tool_schemas]}")

    # ================= Phase 1: 읽기 → 제안 → gate 차단 (변이 0) =================
    print("\n" + "=" * 60)
    print("PHASE 1 — Qwen이 기존 task를 읽고 create_task를 제안")
    started = time.perf_counter()
    response1 = client.chat_with_tools(
        [{"role": "user", "content": PHASE1_REQUEST}],
        tools=tool_schemas,
        metadata={"source": "lifecycle_check", "phase": 1},
    )
    phase1_s = time.perf_counter() - started
    trace1 = _traces_for(config, "lifecycle_check").get(1)
    calls1 = _tool_result_names(trace1) if trace1 else []
    print(f"(e2e {phase1_s:.1f}s) QWEN TOOL CALL 순서: {calls1 or '(없음 — trace 미기록)'}")
    for call in response1.tool_calls:
        print(f"  제안: {call.name}{call.arguments}")

    if response1.finish_reason != "awaiting_confirmation":
        print(f"\n[실패] finish_reason={response1.finish_reason}")
        if response1.finish_reason == "stop":
            print("[검증 미달] 모델이 create_task를 제안하지 않았습니다.")
            return 3
        return 1
    proposed_calls = list(response1.tool_calls)
    if not any(call.name == "create_task" for call in proposed_calls):
        print("[검증 미달] awaiting_confirmation인데 create_task 제안이 없습니다.")
        return 3

    mutated = tasks_path.read_bytes() != before
    open_count = len(_open_entries(tasks_path))
    print(f"PERMISSION: awaiting_confirmation — 변이 0 확인 (open task {open_count}개 유지, "
          f"tasks.md {'불변 ✓' if not mutated else '변경됨 → 실패'})")
    if mutated:
        print("[실패] 승인 전 상태 변경 — §8.4 위반")
        return 1

    # ================= Phase 2: 승인 → 정확히 1회 실행 =================
    print("\n" + "=" * 60)
    print("PHASE 2 — APPROVAL (confirmed_calls) → 정확히 제안된 call만 실행")
    started = time.perf_counter()
    final2 = client.chat_with_tools(
        [{"role": "user", "content": PHASE1_REQUEST}],
        tools=tool_schemas,
        confirmed_calls=proposed_calls,
        metadata={"source": "lifecycle_check", "phase": 2},
    )
    phase2_s = time.perf_counter() - started
    trace2 = _traces_for(config, "lifecycle_check").get(2)
    print(f"(e2e {phase2_s:.1f}s) QWEN FINAL RESPONSE:\n{final2.content}\n")

    if final2.finish_reason not in ("stop", "awaiting_confirmation"):
        print(f"[실패] 승인 후 finish_reason={final2.finish_reason}")
        return 1
    if final2.finish_reason == "awaiting_confirmation":
        # 승인된 call은 이미 실행됐고, 이후 모델의 새 write 제안은 재차단 — 설계 동작
        # (§8.3-2: 1회 승인 = 1회 실행). 파일 증거로 실행 1회를 확인한다.
        print("  (승인된 call은 실행됨 — 모델의 추가 write 제안은 gate가 재차단, 설계 동작)")
    open_after = _open_entries(tasks_path)
    if len(open_after) != 2:
        print(f"[실패] 기대 open task 2개, 실제 {len(open_after)}개 — 정확히 1회 실행 확인 실패")
        return 1
    new_line = next(line for line in open_after if seed_id not in line)
    new_title = new_line.split(": ", 1)[1].split(" — ", 1)[0]
    print(f"TOOL RESULT: created — 새 task 1개 (open task {len(open_after)}개)")
    print(f"  {new_line}")
    # trace: create_task 성공 실행(ok + created)은 정확히 1번이어야 한다
    if trace2:
        executed = [
            item for item in json.loads(trace2.read_text(encoding="utf-8")).get("tool_results", [])
            if item["call"]["name"] == "create_task" and item["result"].get("ok")
        ]
        if len(executed) != 1 or not executed[0]["result"]["data"]["created"]:
            print(f"[실패] phase 2에서 create_task 성공 실행이 정확히 1회가 아님: {executed}")
            return 1

    # ================= Phase 3: read-back — list_current_tasks =================
    print("\n" + "=" * 60)
    print("PHASE 3 — Qwen이 list_current_tasks로 task를 다시 읽고 답변")
    started = time.perf_counter()
    final3 = client.chat_with_tools(
        [{"role": "user", "content": PHASE3_REQUEST}],
        tools=tool_schemas,
        metadata={"source": "lifecycle_check", "phase": 3},
    )
    phase3_s = time.perf_counter() - started
    trace3 = _traces_for(config, "lifecycle_check").get(3)
    print(f"(e2e {phase3_s:.1f}s) QWEN FINAL RESPONSE:\n{final3.content}\n")

    if final3.finish_reason != "stop":
        print(f"[실패] phase 3 finish_reason={final3.finish_reason}")
        return 1
    if not trace3:
        print("[실패] phase 3 trace가 없습니다")
        return 1
    if _tool_result_names(trace3) != ["list_current_tasks"]:
        print(f"[실패] phase 3에서 list_current_tasks 호출이 없습니다: {_tool_result_names(trace3)}")
        return 3
    result3 = _result(trace3, 0)
    if not result3.get("ok") or result3.get("data", {}).get("count") != 2:
        print(f"[실패] read-back 결과가 open task 2개가 아님: {result3}")
        return 1
    # grounding: 최종 응답이 저장된 새 task 제목에 근거하는지
    if new_title not in final3.content and new_line.split()[1] not in final3.content:
        print(f"[실패] 최종 응답에 read-back한 새 task 정보가 없습니다 (grounding 실패)")
        print(f"  저장된 제목: {new_title!r}")
        return 1

    # ================= 요약 =================
    print("\n" + "=" * 60)
    print("LIFECYCLE SUMMARY")
    print(f"  USER REQUEST → QWEN TOOL CALL(list_current_tasks 등) → 제안 create_task")
    print(f"  → PERMISSION: awaiting_confirmation (변이 0) → APPROVAL")
    print(f"  → TOOL RESULT: created (open task 1→2) → QWEN FINAL RESPONSE")
    print(f"  → read-back: list_current_tasks count=2 → QWEN FINAL RESPONSE (grounded)")
    for phase in sorted(trace for trace in _traces_for(config, "lifecycle_check")):
        print(f"  TRACE: {_traces_for(config, 'lifecycle_check')[phase]}")
    if not args.keep_scratch:
        shutil.rmtree(scratch)
        print(f"(scratch 삭제: {scratch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
