from __future__ import annotations

"""Gate loop (Slice 3): gate FAIL 시 feedback을 Freebuff에 1회만 되돌리는 루프.

흐름:
  gate 실행 → PASS: 상태 초기화, exit 0
          → FAIL + 재시도 남음: data/gate_feedback.md 작성 (한국어 피드백), exit 1
          → FAIL + 재시도 소진: data/gate_blocked.md 작성 (블로커), exit 2

"한 번 다시" 원칙: max_attempts(기본 1)만큼만 재시도한다. 재시도는 에이전트가
data/gate_feedback.md를 읽고 수정한 뒤 이 스크립트를 다시 실행하는 것으로 이뤄진다.
PASS가 되면 attempt가 0으로 리셋되어 다음 작업은 새 상태로 시작한다.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from scripts.gate import (
    CheckResult,
    build_feedback,
    check_all,
    head_sha,
    load_config,
    print_results,
    verdict,
    write_json,
)

STATE_FILE = "data/gate_state.json"
FEEDBACK_FILE = "data/gate_feedback.md"
BLOCKED_FILE = "data/gate_blocked.md"
VERDICT_FILE = "data/gate_verdict.json"


def _path(name: str, cwd: Path | None) -> Path:
    return (cwd / name) if cwd else Path(name)


def load_state(cwd: Path | None = None) -> dict[str, Any]:
    path = _path(STATE_FILE, cwd)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except (json.JSONDecodeError, OSError):
            pass
    return {"attempt": 0, "status": "new"}


def save_state(state: dict[str, Any], cwd: Path | None = None) -> None:
    path = _path(STATE_FILE, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_loop(
    cfg: dict[str, Any],
    cwd: Path | None = None,
    results_provider: Callable[[dict[str, Any], Path | None], list[CheckResult]] | None = None,
) -> int:
    """gate 실행 + 재시도 상태 전이. exit: 0=PASS, 1=FAIL(재시도 가능), 2=BLOCKED(재시도 소진)."""
    provider = results_provider or check_all
    results = provider(cfg, cwd)
    v = verdict(results)
    print_results(results)

    write_json(
        _path(VERDICT_FILE, cwd),
        {"verdict": v, "head": head_sha(cwd), "checks": [r.to_dict() for r in results]},
    )

    state = load_state(cwd)
    max_attempts = max(1, int(cfg.get("max_attempts", 1)))

    if v == "pass":
        save_state({"attempt": 0, "status": "passed", "head": head_sha(cwd)}, cwd)
        print(f"\n>>> GATE PASS — {head_sha(cwd)} 검증 통과. 상태 초기화됨.")
        return 0

    attempt = int(state.get("attempt", 0)) + 1
    if attempt <= max_attempts:
        feedback = build_feedback(results, attempt, max_attempts)
        feedback_path = _path(FEEDBACK_FILE, cwd)
        feedback_path.parent.mkdir(parents=True, exist_ok=True)
        feedback_path.write_text(feedback, encoding="utf-8")
        save_state({"attempt": attempt, "status": "retry", "head": head_sha(cwd)}, cwd)
        print(f"\n>>> GATE FAIL — 재시도 {attempt}/{max_attempts}.")
        print(f"    피드백: {feedback_path}")
        print("    data/gate_feedback.md를 읽고 수정한 뒤 python -m scripts.gate_loop 를 다시 실행하라.")
        return 1

    blocked = (
        f"# Gate BLOCKED\n\n"
        f"재시도 {max_attempts}회 모두 실패했습니다. 수동 검토가 필요합니다.\n\n"
        + build_feedback(results, attempt, max_attempts)
    )
    blocked_path = _path(BLOCKED_FILE, cwd)
    blocked_path.parent.mkdir(parents=True, exist_ok=True)
    blocked_path.write_text(blocked, encoding="utf-8")
    save_state({"attempt": attempt, "status": "blocked", "head": head_sha(cwd)}, cwd)
    print(f"\n>>> GATE BLOCKED — 재시도 {max_attempts}회 소진. 블로커 기록: {blocked_path}")
    return 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harness Slice 3 gate loop — FAIL 시 피드백 1회 재시도.",
    )
    parser.add_argument("--config", type=str, default=None, help="gate config YAML 경로")
    parser.add_argument("--cwd", type=str, default=None, help="검증 대상 저장소 경로")
    parser.add_argument("--reset", action="store_true", help="재시도 상태 초기화 후 실행")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    cwd = Path(args.cwd).resolve() if args.cwd else None
    if args.reset:
        save_state({"attempt": 0, "status": "reset"}, cwd)
    return run_loop(cfg, cwd)


if __name__ == "__main__":
    raise SystemExit(main())