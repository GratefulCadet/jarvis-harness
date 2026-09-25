"""M5 live acceptance — 실제 bridge 작업이 복귀 브리핑의 다음 행동으로 이어지는가.

M1–M4가 스탠바이로 남긴 문제: task 생성/수정/완료와 link는 모델 tool loop를
거치지 않아 trace에 남지 않았고, 그래서 resume_briefing의 touched_task_ids가
런타임에 항상 비어 있었다. M5는 이 작업들을 trace에 기록한다.

이 스크립트는 그 주장을 실제 경로로 검증한다:
    1. 격리 scratch state에 프로젝트/task를 준비한다 (실사용자 데이터 무변경).
    2. 실제 bridge 핸들러를 JSONL 메시지로 호출한다 (모델 없이 deterministic 경로).
    3. 같은 trace 디렉터리에서 resume_briefing을 실행한다.
    4. 사용자가 마지막으로 다룬 task가 목록 첫 번째가 아님에도 이어지는지 본다.

사용: python -m scripts.m5_session_signal_check
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.client import HarnessClient
from harness.config import HarnessConfig
from harness.tools import build_default_registry
from harness.tools.file_store import FileStore
from harness.tools.workspace import WorkspaceManager, default_registry_file
from scripts.harness_bridge import BridgeSession, handle_message

PASS = "✅"
FAIL = "❌"


def _build_state(root: Path) -> dict[str, Path]:
    memory = root / "mem"
    memory.mkdir(parents=True)
    (memory / "projects.md").write_text(
        "# Projects\n\n## Active\n\n"
        "- graduation-thesis: 졸업논문 프로젝트\n",
        encoding="utf-8",
    )
    (memory / "tasks.md").write_text(
        "# Tasks\n\n## graduation-thesis\n\n"
        "- [ ] t-first: 실험 결과 정리 — 목록 첫 번째지만 사용자가 안 만짐\n"
        "- [ ] t-focus: 논문 초안 개요 작성 — 사용자가 마지막으로 다룬 작업\n",
        encoding="utf-8",
    )
    workspace = root / "workspace"
    (workspace / "graduation").mkdir(parents=True)
    (workspace / "graduation" / "paper.txt").write_text(
        "thesis draft\n", encoding="utf-8"
    )
    traces = root / "traces"
    traces.mkdir()
    return {"mem": memory, "workspace": workspace, "traces": traces}


def _client(state: dict[str, Path]) -> HarnessClient:
    return HarnessClient(
        HarnessConfig(
            runtime="mock",
            memory_dir=state["mem"],
            task_file=state["mem"] / "tasks.md",
            trace_dir=state["traces"],
            file_roots={"workspace": str(state["workspace"])},
            trace_enabled=True,
        )
    )


def _registry(state: dict[str, Path]):
    return build_default_registry(
        memory_dir=state["mem"],
        task_file=state["mem"] / "tasks.md",
        file_roots={"workspace": str(state["workspace"])},
        trace_dir=state["traces"],
    )


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{PASS if ok else FAIL} {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="m5-check-")
    state = _build_state(Path(tmp))
    client = _client(state)
    session = BridgeSession()
    ok = True
    try:
        # ── 0) 신호가 없을 때의 기준선 ───────────────────────────────
        baseline = _registry(state).execute(
            "resume_briefing", {"query": "졸업논문"}
        )
        ok &= _check(
            "기준선: 세션 신호 없으면 첫 번째 open task",
            baseline.ok and baseline.data["next_action"]["task_id"] == "t-first",
            f"next_action={baseline.data['next_action']['task_id']}",
        )

        # ── 1) 실제 bridge 작업: t-focus 제목 수정 ────────────────────
        response = handle_message(
            client,
            session,
            {
                "type": "update_task",
                "id": "r1",
                "project_id": "graduation-thesis",
                "task_id": "t-focus",
                "reason": "3장 구조 확정 후 초안 시작",
            },
        )
        ok &= _check(
            "update_task 성공", response.get("status") == "ok", str(response.get("error") or "")
        )

        traces = sorted(state["traces"].glob("*.json"))
        ok &= _check("update_task이 trace를 남김", len(traces) == 1, f"{len(traces)}건")

        if traces:
            entry = json.loads(traces[0].read_text(encoding="utf-8"))
            meta = entry["request"]["metadata"]
            args = entry["tool_results"][0]["call"]["arguments"]
            ok &= _check(
                "trace에 project_id 기록", meta.get("project_id") == "graduation-thesis"
            )
            ok &= _check("trace에 task_id 기록", args.get("task_id") == "t-focus")
            ok &= _check("시스템 작업으로 표시", bool(meta.get("activity")))

        # ── 2) 복귀: 다룬 task가 이어져야 한다 ────────────────────────
        briefing = _registry(state).execute("resume_briefing", {"query": "졸업논문"})
        ok &= _check("resume_briefing 성공", briefing.ok, str(briefing.error or ""))
        if briefing.ok:
            nxt = briefing.data["next_action"]
            ok &= _check(
                "마지막으로 다룬 task가 다음 행동",
                nxt["task_id"] == "t-focus",
                f"next_action={nxt['task_id']} basis={nxt['basis']}",
            )
            ok &= _check(
                "근거가 세션 연속성을 인용",
                nxt["basis"] == "최근 세션에서 진행 중이던 작업",
                nxt["basis"],
            )
            ok &= _check(
                "최근 활동에 시스템 작업 표시",
                briefing.data["last_activity"]
                and briefing.data["last_activity"][0].get("kind") == "system",
            )

        # ── 3) 실제 link: 파일 경로까지 기록되는가 ────────────────────
        workspace = WorkspaceManager(
            FileStore({"workspace": str(state["workspace"])}),
            default_registry_file(state["mem"]),
        )
        workspace.scan_root()
        ref = workspace.registry.by_path("workspace", "graduation/paper.txt")
        link_response = handle_message(
            client,
            session,
            {
                "type": "link_task_file",
                "id": "r2",
                "task_id": "t-focus",
                "file_id": ref.id if ref else "",
                "relation": "reference",
            },
        )
        ok &= _check(
            "link_task_file 성공",
            link_response.get("status") == "ok",
            str(link_response.get("error") or ""),
        )

        # 같은 초에 만들어진 trace가 여럿일 수 있으므로 activity로 식별한다.
        link_entry = next(
            json.loads(path.read_text(encoding="utf-8"))
            for path in state["traces"].glob("*.json")
            if json.loads(path.read_text(encoding="utf-8"))["request"]["metadata"].get(
                "activity"
            )
            == "link_task_file"
        )
        link_args = link_entry["tool_results"][0]["call"]["arguments"]
        ok &= _check(
            "link trace에 파일 경로 기록",
            link_args.get("path") == "graduation/paper.txt",
            str(link_args.get("path")),
        )
        ok &= _check(
            "link trace의 project_id 역조회 성공",
            link_entry["request"]["metadata"].get("project_id")
            == "graduation-thesis",
        )

        after = _registry(state).execute("resume_briefing", {"query": "졸업논문"})
        if after.ok:
            names = [
                r["file"]["name"]
                for r in after.data["next_action"]["resources"]
            ]
            ok &= _check(
                "연결된 자료가 복귀 제안에 실림",
                "paper.txt" in names,
                str(names),
            )

        # ── 4) 완료한 작업은 다시 제안되지 않는다 ─────────────────────
        handle_message(
            client,
            session,
            {
                "type": "update_task",
                "id": "r3",
                "project_id": "graduation-thesis",
                "task_id": "t-first",
                "done": True,
            },
        )
        final = _registry(state).execute("resume_briefing", {"query": "졸업논문"})
        if final.ok:
            ok &= _check(
                "완료된 task는 다음 행동에서 제외",
                final.data["next_action"]["task_id"] == "t-focus",
                f"next_action={final.data['next_action']['task_id']}",
            )
            ok &= _check(
                "완료 카운트 반영", final.data["tasks"]["completed_count"] == 1
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(">>> M5 CHECK PASS" if ok else ">>> M5 CHECK FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
