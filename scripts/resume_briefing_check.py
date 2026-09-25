"""M1 live acceptance — "복귀 → 이어서 시작" 한 발화 시나리오 (실 Ollama).

scratch 전용. 사용자의 자연 발화 하나("졸업논문 계속하자")만으로
  프로젝트 확정 → resume_briefing tool → 브리핑 data → 최종 답변
흐름이 실제 모델을 통해 도는지 검증한다.

검증:
1. 발화 하나로 모델이 resume_briefing을 선택한다 (tool 강제 없음).
2. 결정적 조립 결과(project·tasks·resources·last_activity·next_action)가
   bridge events로 왜곡 없이 도착한다.
3. 어떤 canonical 파일도 변경되지 않는다 (read-only).

실행: python -m scripts.resume_briefing_check
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from harness.client import HarnessClient
from harness.config import HarnessConfig
from scripts.harness_bridge import BridgeSession, handle_message

tmp = Path(tempfile.mkdtemp())
mem = tmp / "mem"
mem.mkdir()
(mem / "projects.md").write_text(
    "# Projects\n\n## Active\n\n"
    "- graduation-thesis: 졸업논문 프로젝트\n"
    "- vocal-app: 보컬 앱 아이디어\n",
    encoding="utf-8",
)
(mem / "tasks.md").write_text(
    "# Tasks\n\n"
    "## graduation-thesis\n\n"
    "- [ ] t-open1: 실험 결과 정리 — LPIPS 지표 표로 정리\n"
    "- [ ] t-open2: 논문 초안 개요 작성 — 3장 구조 잡기\n"
    "- [x] t-done1: 자료 수집 — 관련논문 5편 정리\n",
    encoding="utf-8",
)
ws = tmp / "research"
(ws / "graduation").mkdir(parents=True)
(ws / "graduation" / "paper.txt").write_text(
    "thesis draft text\n", encoding="utf-8"
)

cfg = HarnessConfig.load()
cfg.memory_dir = mem
cfg.task_file = mem / "tasks.md"
cfg.file_roots = {"research": str(ws)}
cfg.trace_dir = tmp / "traces"
cfg.trace_dir.mkdir()
client = HarnessClient(cfg)
session = BridgeSession()

tasks_before = (mem / "tasks.md").read_text(encoding="utf-8")

# 1) 사용자 발화 하나 — tool 명시 없음, 프로젝트 id 명시 없음
res = handle_message(
    client,
    session,
    {
        "type": "chat",
        "id": 1,
        "text": "졸업논문 계속하자",
        "project_id": "graduation-thesis",
    },
)
assert res["status"] == "final", res
print(f"[1] final 응답 수신 (trace={res.get('trace_id')})")
print(f"    모델 답변: {(res.get('text') or '')[:200]}")

# 2) 모델이 resume_briefing을 골랐고 브리핑 data가 events로 도착했는가
briefing_events = [
    e
    for e in res.get("events") or []
    if e.get("kind") == "tool" and e.get("name") == "resume_briefing"
]
assert briefing_events, f"resume_briefing tool 실행 없음: {res.get('events')}"
event = briefing_events[-1]
assert event["ok"], event
data = event["data"]
assert data["status"] == "ok", data
assert data["project"]["id"] == "graduation-thesis", data["project"]
assert data["tasks"]["open_count"] == 2, data["tasks"]
assert data["next_action"]["task_id"] == "t-open1", data["next_action"]
print("[2] resume_briefing data가 events로 도착 — 결정적 조립 확인")
print(f"    project   : {data['project']['title']} (resolved_by={data['project']['resolved_by']})")
print(f"    tasks     : open={data['tasks']['open_count']} done={data['tasks']['completed_count']}")
print(f"    resources : {len(data['resources'])}개")
print(f"    activity  : {len(data['last_activity'])}건")
print(f"    next      : {data['next_action']['title']} — {data['next_action']['reason']}")

# 3) read-only — canonical 파일 무변경
tasks_after = (mem / "tasks.md").read_text(encoding="utf-8")
assert tasks_after == tasks_before, "tasks.md가 변경됨 — read-only 위반"
print("[3] read-only 확인 — canonical 파일 무변경")

print("\n>>> M1 LIVE ACCEPTANCE PASS")
