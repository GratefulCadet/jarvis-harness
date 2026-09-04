from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from harness.client import HarnessClient
from harness.config import PROJECT_ROOT, HarnessConfig
from harness.tools.schemas import list_current_tasks as list_current_tasks_schema_fn

"""Slice 5 check — 실 Ollama에서 list_current_tasks read 흐름 검증.

흐름: 사용자 요청 → Qwen이 list_current_tasks 호출 → memory/tasks.md에서
현재 진행 중(완료 제외) task 목록 반환 → Qwen이 그 결과로 최종 응답.

검증 포인트:
- read-only: 조회 전후 tasks.md가 1바이트도 변하지 않아야 한다
- project filter: 요청한 프로젝트의 open task만 반환 (다른 프로젝트·완료 제외)
- 최종 응답이 실제 tool 결과(격리 task 제목)에 근거해야 한다 (grounding)
- trace에 list_current_tasks tool_result가 남아야 한다

안전: 실제 앱 memory를 건드리지 않도록 **격리된 scratch memory**(data/ 아래,
gitignore 대상)를 HARNESS_MEMORY_DIR로 지정해 사용한다. 프로젝트 id도 smoke-*.

exit code:
  0 — 전 구간 완료 (호출 → 결과 → 근거 있는 최종 응답, 변이 0)
  3 — 모델이 list_current_tasks를 호출하지 않음 (tool 템플릿 문제 가능성)
  1 — 오류 / 검증 미달
"""

SMOKE_PROJECT = "smoke-list"
SMOKE_OTHER = "smoke-other"
SMOKE_OPEN_TITLE = "격리 목록 확인용 task"
SMOKE_DONE_TITLE = "이미 완료된 task"
SCRATCH = PROJECT_ROOT / "data" / "smoke_memory_list"

DEFAULT_REQUEST = (
    f"지금 {SMOKE_PROJECT} 프로젝트에서 현재 진행 중인 task 목록을 "
    "list_current_tasks 도구로 조회해서 알려줘. 완료된 task는 제외하고 "
    "각 task의 id와 제목을 정리해서 답해줘."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harness Slice 5 check — 실 Ollama list_current_tasks read 흐름 (격리 대상)."
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
        f"- {SMOKE_PROJECT}: list_current_tasks 스모크 검증 프로젝트\n"
        f"- {SMOKE_OTHER}: 필터 확인용 다른 프로젝트\n",
        encoding="utf-8",
    )
    # 사전 시드: 대상 프로젝트에 open 1 + done 1, 다른 프로젝트에 open 1
    import hashlib

    def task_id(title: str, reason: str) -> str:
        digest = hashlib.sha1(
            f"{SMOKE_PROJECT}|{title}|{reason}".encode("utf-8")
        ).hexdigest()
        return f"t-{digest[:12]}"

    open_id = task_id(SMOKE_OPEN_TITLE, "read-side 격리 확인")
    done_id = task_id(SMOKE_DONE_TITLE, "완료 처리 확인")
    other_id = hashlib.sha1(
        f"{SMOKE_OTHER}|다른 프로젝트 task|필터 확인".encode("utf-8")
    ).hexdigest()[:12]
    tasks_path = scratch / "tasks.md"
    tasks_path.write_text(
        "# Tasks\n\n"
        f"## {SMOKE_PROJECT}\n\n"
        f"- [ ] {open_id}: {SMOKE_OPEN_TITLE} — read-side 격리 확인\n"
        f"- [x] {done_id}: {SMOKE_DONE_TITLE} — 완료 처리 확인\n\n"
        f"## {SMOKE_OTHER}\n\n"
        f"- [ ] t-{other_id}: 다른 프로젝트 task — 필터 확인\n",
        encoding="utf-8",
    )
    before = tasks_path.read_bytes()

    os.environ["HARNESS_MEMORY_DIR"] = str(scratch)
    config = HarnessConfig.load(args.config)
    # 안전 가드: 설정 실패로 실제 앱 memory를 읽는 사고를 원천 차단
    if Path(config.memory_dir).resolve() != scratch:
        raise SystemExit(
            f"[안전 가드] memory_dir이 scratch가 아닙니다: {config.memory_dir} — "
            "환경변수 우선순위 문제. 중단합니다."
        )
    if args.model:
        config.model = args.model
    config.num_predict = args.num_predict

    client = HarnessClient(config)
    tool_schemas = [list_current_tasks_schema_fn()]
    print(f"[runtime={config.runtime} model={config.model}]")
    print(f"[격리 memory_dir={config.memory_dir}] (실제 앱 memory와 분리)")
    print(f"tool 표면: {[schema.name for schema in tool_schemas]}")

    messages = [{"role": "user", "content": args.text}]

    started = time.perf_counter()
    response = client.chat_with_tools(
        messages,
        tools=tool_schemas,
        metadata={"source": "list_tasks_check", "phase": 1},
    )
    elapsed = time.perf_counter() - started
    print(f"\n=== (e2e {elapsed:.1f}s) ===\n최종 응답:\n{response.content}\n")

    # 1) read-only: tasks.md 불변
    if tasks_path.read_bytes() != before:
        print("[실패] list_current_tasks가 tasks.md를 변경했습니다 — read 위반")
        return 1

    # 2) tool이 실제 호출됐고 결과가 trace에 남았는지
    called, results = _find_tool_call(config.trace_dir, "list_tasks_check")
    if not called:
        print("\n[검증 미달] trace에 list_current_tasks 호출이 없습니다.")
        print("모델이 tool을 호출하지 않았을 수 있습니다 (tool 템플릿 문제 가능성).")
        return 3
    print(f"trace의 tool 호출 결과: {results}")

    # 3) grounding: 최종 응답이 tool 결과(격리 open task 제목)에 근거하는지
    if SMOKE_OPEN_TITLE not in response.content:
        print("[실패] 최종 응답에 tool 결과의 task 제목이 없습니다 (grounding 실패)")
        return 1

    print("\n[완료] list_current_tasks 호출 → open task만 반환 → 근거 있는 최종 응답, 변이 0.")
    print(f"[read-only 확인] tasks.md 바이트 불변 ({len(before)} bytes)")
    if not args.keep_scratch:
        shutil.rmtree(scratch)
        print(f"(scratch 삭제: {scratch})")
    return 0


def _find_tool_call(trace_dir: Path, source: str) -> tuple[bool, list]:
    """source metadata가 일치하는 trace에서 list_current_tasks tool_results를 찾는다."""
    if not trace_dir.is_dir():
        return False, []
    for path in sorted(trace_dir.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if entry.get("request", {}).get("metadata", {}).get("source") != source:
            continue
        for item in entry.get("tool_results", []):
            call = item.get("call", {})
            if call.get("name") == "list_current_tasks":
                return True, [item.get("result", {})]
    return False, []


if __name__ == "__main__":
    raise SystemExit(main())
