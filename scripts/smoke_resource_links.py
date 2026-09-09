"""Bridge smoke — Resource Link V1 실제 handle_message 경로 (PART K-12).

scratch 전용. link → tree_snapshot projection → duplicate idempotent →
list_project_resources → unlink 를 실제 bridge dispatch로 검증한다.
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
    "# Projects\n\n## Active\n\n- graduation-thesis: 졸업논문\n", encoding="utf-8"
)
(mem / "tasks.md").write_text(
    "# Tasks\n\n## graduation-thesis\n\n- [ ] t-fix11111111: setup — baseline\n",
    encoding="utf-8",
)
ws = tmp / "research"
(ws / "results").mkdir(parents=True)
(ws / "results" / "lpips.csv").write_text("step,lpips\n1,0.42\n", encoding="utf-8")

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


# 1) FileRef 확보 — 파일 목록에서 identity를 읽는다
snap = send({"type": "files_snapshot", "id": 1})
assert snap["status"] == "ok", snap
entries = [e for s in snap["sections"] for e in s["entries"]]
fid = next(e["id"] for e in entries if e["name"] == "lpips.csv")
print(f"[1] files_snapshot ok — lpips.csv → {fid}")

# 2) link_project_file (reference)
r1 = send({"type": "link_project_file", "id": 2, "project_id": "graduation-thesis",
           "file_id": fid, "relation": "reference"})
assert r1["status"] == "ok" and r1["created"], r1
link_id = r1["link"]["id"]
assert link_id.startswith("rl-"), r1
print(f"[2] link created — {link_id}")

# 3) duplicate → idempotent
r2 = send({"type": "link_project_file", "id": 3, "project_id": "graduation-thesis",
           "file_id": fid, "relation": "reference"})
assert r2["status"] == "ok" and not r2["created"] and r2["link"]["id"] == link_id, r2
print("[3] duplicate idempotent ok")

# 4) tree_snapshot projection — project children에 resource_group + project_resource
tree = send({"type": "tree_snapshot", "id": 4})
assert tree["status"] == "ok", tree
proj = next(n for n in tree["tree"] if n["type"] == "project")
groups = [c for c in proj["children"] if c.get("type") == "resource_group"]
assert groups, tree
file_nodes = [n for g in groups for n in g.get("children", [])]
assert any(n["file"]["id"] == fid for n in file_nodes), tree
print(f"[4] tree_snapshot projection ok — groups={[g['title'] for g in groups]}, "
      f"files={[n['file']['name'] for n in file_nodes]}")

# 5) list_project_resources
lst = send({"type": "list_project_resources", "id": 5, "project_id": "graduation-thesis"})
assert lst["status"] == "ok" and len(lst["resources"]) == 1, lst
assert lst["resources"][0]["file"]["relative_path"] == "results/lpips.csv", lst
print("[5] list_project_resources ok")

# 6) rename → 링크 재작성 없이 새 locator 반영
(ws / "results" / "lpips.csv").rename(ws / "results" / "lpips-final.csv")
lst2 = send({"type": "list_project_resources", "id": 6, "project_id": "graduation-thesis"})
assert lst2["status"] == "ok", lst2
resolved = lst2["resources"][0]["file"]
assert resolved["id"] == fid, resolved
assert resolved["relative_path"] == "results/lpips-final.csv", resolved
print("[6] rename reflected via FileRef resolve ok")

# 7) 검증 오류 — 경로를 file_id로 → 하드 거부
bad = send({"type": "link_project_file", "id": 7, "project_id": "graduation-thesis",
            "file_id": "results/lpips.csv"})
assert bad["status"] == "error" and "f-*" in bad["error"], bad
print("[7] path-as-id rejected ok")

# 8) unlink — metadata만
u = send({"type": "unlink_project_file", "id": 8, "link_id": link_id})
assert u["status"] == "ok" and u["removed"], u
lst3 = send({"type": "list_project_resources", "id": 9, "project_id": "graduation-thesis"})
assert lst3["resources"] == [], lst3
print("[8] unlink metadata-only ok")

print("\nBRIDGE SMOKE: ALL PASS")
