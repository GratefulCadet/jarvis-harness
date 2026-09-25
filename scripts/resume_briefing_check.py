"""M1+M2 live acceptance — "복귀 → 이어서 시작" 흐름 (실 Ollama).

scratch 전용. 사용자의 자연 발화 하나("졸업논문 계속하자")만으로
  프로젝트 확정 → resume_briefing tool → 브리핑 data → 최종 답변
흐름이 실제 모델을 통해 도는지 검증하고, M2 자료 연결 정확도를 이어서 검증한다.

검증:
1. 발화 하나로 모델이 resume_briefing을 선택한다 (tool 강제 없음).
2. 결정적 조립 결과(project·tasks·resources·last_activity·next_action)가
   bridge events로 왜곡 없이 도착한다.
3. 어떤 canonical 파일도 변경되지 않는다 (read-only).
4. M2 — 연결 전에는 workspace 파일이 브리핑에 뜨지 않는다 (전체 노출 없음).
5. M2 — 명시적 link_task_file 후 "그 작업의 관련 파일"로 귀속해 실린다.
6. M2 — unlink 후 다시 사라진다.

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


def send(payload: dict) -> dict:
    return handle_message(client, session, payload)


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

# 4) M2 — 연결 전: workspace에 paper.txt가 있어도 브리핑에는 안 뜬다
pre = client.tools.execute("resume_briefing", {"query": "졸업논문"})
assert pre.ok, pre.error
assert pre.data["resources"] == [], pre.data["resources"]
print("[4] M2 — 미연결 파일은 브리핑에 뜨지 않음 (workspace 전체 노출 없음)")

# 5) M2 — 명시적 사용자 행동(link_task_file) 후 task 귀속 자료로 실린다
snap = send({"type": "files_snapshot", "id": 10})
assert snap["status"] == "ok", snap
file_entries = [e for s in snap["sections"] for e in s["entries"]]
fid = next(e["id"] for e in file_entries if e["name"] == "paper.txt")
link_res = send({
    "type": "link_task_file", "id": 11,
    "task_id": "t-open1", "file_id": fid, "relation": "result",
})
assert link_res["status"] == "ok" and link_res["created"], link_res

post = client.tools.execute("resume_briefing", {"query": "졸업논문"})
assert post.ok, post.error
data2 = post.data
assert len(data2["resources"]) == 1, data2["resources"]
entry = data2["resources"][0]
assert entry["source"] == "task" and entry["task_id"] == "t-open1", entry
assert entry["file"]["path"].endswith("paper.txt"), entry
assert any(
    e["file"]["path"].endswith("paper.txt")
    for e in data2["next_action"]["resources"]
), data2["next_action"]
open_by_id = {t["id"]: t for t in data2["tasks"]["open"]}
assert any(
    f["path"].endswith("paper.txt") for f in open_by_id["t-open1"].get("files", [])
), open_by_id
print("[5] M2 — 링크 후 't-open1의 관련 파일'로 귀속 — 복귀 브리핑에 반영")

# 6) M2 — unlink 후 다시 사라진다
del_res = send({"type": "unlink_task_file", "id": 12, "link_id": link_res["link"]["id"]})
assert del_res["status"] == "ok", del_res
post2 = client.tools.execute("resume_briefing", {"query": "졸업논문"})
assert post2.ok, post2.error
assert post2.data["resources"] == [], post2.data["resources"]
print("[6] M2 — unlink 후 브리핑에서 제거 확인")

print("\n>>> M1+M2 LIVE ACCEPTANCE PASS")
