from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from harness.tools.memory_context import MemoryContextReader

"""Project Primary Workspace V1 (WORKSPACE ARCHITECTURE §4).

결정(계약 §4·PART B–D 반영):
- Project ≠ Folder. 프로젝트는 **0 또는 1개의** primary workspace root를
  **논리 root_id**로 참조한다. 절대 경로는 관계 identity에 절대 들어가지 않는다
  (멀티디바이스: 같은 root_id가 디바이스마다 다른 device_path로 매핑될 수 있다).
- 저장소: <memory_dir>/project_workspaces.json — file_refs.json /
  resource_links.json과 같은 파생 메타데이터 규약. filesystem도, projects.md도
  수정하지 않는다. projects.md의 `- id: title` 파서는 공유 원천이라 건드리지
  않는다(마크다운 확장은 파싱이 깨지기 쉽다 → 작은 JSON 레지스트리 선택).
- 가용성은 **읽을 때** 판정한다: root가 현재 디바이스 config에 없거나
  device_path가 존재하지 않으면 available:false — 관계 메타데이터는 절대
  자동 삭제하지 않는다(PART D).
- Validation: unknown project / unknown root / 경로를 root_id로 전달 → 거부,
  zero mutation. 절대 경로·구분자 포함 문자열은 root_id로 받지 않는다.

ResourceLink와의 경계(§PART H): primary workspace는 "기본 작업 문맥"일 뿐,
워크스페이스 하위 파일이 전부 ResourceLink가 되는 것도 아니다. 기존
ResourceLink는 workspace 변경과 무관하게 유지된다.
"""


class ProjectWorkspaceError(Exception):
    """primary workspace 관계 생성/조회 위반."""


def default_project_workspaces_file(memory_dir: Path | str | None) -> Path:
    """레지스트리 경로 규약 — <memory_dir>/project_workspaces.json."""
    if memory_dir is None:
        raise ProjectWorkspaceError("memory_dir 미설정 — JARVIS memory 경로가 없습니다")
    return Path(memory_dir) / "project_workspaces.json"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProjectWorkspaces:
    """project_id → logical root_id 매핑의 단일 writer (PART C).

    roots는 현재 디바이스의 승인 루트 {root_id: Path} (FileStore.roots와 같은
    형태 — duck typing). set 시점에는 이 디바이스 config에 루트가 있어야 하고,
    get 시점에는 가용성만 판정한다(다른 디바이스에서 설정한 관계도 읽힌다).
    """

    def __init__(
        self,
        registry_file: str | Path,
        memory_dir: str | Path | None,
        roots: Any = None,  # {root_id: Path} — FileStore.roots 호환
    ) -> None:
        self.registry_file = Path(registry_file)
        self.memory_dir = Path(memory_dir) if memory_dir else None
        self.roots: dict[str, Any] = dict(roots) if roots else {}

    # ---------- 영속화 ----------

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}  # 손상된 레지스트리 → 빈 상태 (crash 금지, filesystem canonical)
        projects = raw.get("projects")
        return dict(projects) if isinstance(projects, dict) else {}

    def reload(self) -> None:
        """여러 Discovery 인스턴스가 레지스트리를 공유하므로 매 연산 전 재적재."""
        self._entries = self._load()

    def save(self) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "projects": self._entries}
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.registry_file.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_name, self.registry_file)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ---------- 검증 (PART C) ----------

    def _project_exists(self, project_id: str) -> bool:
        if self.memory_dir is None or not self.memory_dir.is_dir():
            raise ProjectWorkspaceError("memory_dir 미설정 — JARVIS memory 경로가 없습니다")
        try:
            projects = MemoryContextReader(self.memory_dir).list_projects()
        except FileNotFoundError as exc:
            raise ProjectWorkspaceError(str(exc)) from exc
        return any(p["id"] == project_id for p in projects)

    def _validate_root_id(self, root_id: str) -> None:
        """root_id는 논리 identity — 경로 형태는 하드 거부한다(PART C·§7)."""
        if not root_id or not isinstance(root_id, str):
            raise ProjectWorkspaceError("root_id가 비어 있습니다")
        if "/" in root_id or "\\" in root_id or root_id.startswith("."):
            raise ProjectWorkspaceError(
                f"root_id는 논리 root identity여야 합니다. 경로는 받지 않습니다: {root_id!r}"
            )
        # Windows 드라이브/절대 경로 흔적도 거부 ("C:", "C:\\...", "%HOME%...")
        if len(root_id) >= 2 and root_id[1] == ":":
            raise ProjectWorkspaceError(
                f"root_id는 논리 root identity여야 합니다. 경로는 받지 않습니다: {root_id!r}"
            )

    def _root_configured(self, root_id: str) -> bool:
        return root_id in self.roots

    # ---------- 쓰기 (PART C) ----------

    def set_project_primary_workspace(
        self,
        project_id: str,
        root_id: str,
    ) -> dict[str, Any]:
        if not project_id or not isinstance(project_id, str):
            raise ProjectWorkspaceError("project_id가 비어 있습니다")
        self._validate_root_id(root_id)
        if not self._project_exists(project_id.strip()):
            raise ProjectWorkspaceError(f"존재하지 않는 Project입니다: {project_id.strip()}")
        if not self._root_configured(root_id):
            known = ", ".join(sorted(self.roots)) or "(없음)"
            raise ProjectWorkspaceError(
                f"이 디바이스에 승인되지 않은 root입니다: {root_id!r} — 승인된 루트: {known}"
            )

        pid = project_id.strip()
        self.reload()
        previous = self._entries.get(pid)
        self._entries[pid] = {"root_id": root_id, "set_at": _now_iso()}
        self.save()
        return {
            "project_id": pid,
            "root_id": root_id,
            "updated": previous is not None,
            "workspace": self.get_project_primary_workspace(pid),
        }

    def clear_project_primary_workspace(self, project_id: str) -> dict[str, Any]:
        """관계 메타데이터만 제거. 사용자 파일/폴더는 절대 건드리지 않는다."""
        if not project_id or not isinstance(project_id, str):
            raise ProjectWorkspaceError("project_id가 비어 있습니다")
        pid = project_id.strip()
        self.reload()
        removed = self._entries.pop(pid, None)
        if removed is None:
            return {"project_id": pid, "removed": False}
        self.save()
        return {"project_id": pid, "removed": True, "workspace": removed}

    # ---------- 읽기 (PART D·E) ----------

    def get_project_primary_workspace(self, project_id: str) -> dict[str, Any] | None:
        """관계 + 현재 디바이스 가용성. 관계가 없으면 None.

        available 판정:
        - root가 현재 디바이스 config에 없음  → available:false, reason:"root_not_configured"
        - device_path가 존재하지 않음        → available:false, reason:"path_unavailable"
        관계 메타데이터는 어느 경우에도 삭제하지 않는다(PART D).
        """
        if not project_id or not isinstance(project_id, str):
            raise ProjectWorkspaceError("project_id가 비어 있습니다")
        pid = project_id.strip()
        if self.memory_dir is None or not self.memory_dir.is_dir():
            raise ProjectWorkspaceError("memory_dir 미설정 — JARVIS memory 경로가 없습니다")
        self.reload()
        entry = self._entries.get(pid)
        if entry is None:
            return None

        root_id = entry.get("root_id", "")
        out: dict[str, Any] = {
            "root_id": root_id,
            "display_name": root_id,  # 현재 config transport에는 별칭이 없다 — 과장 금지
            "available": False,
            "set_at": entry.get("set_at"),
        }
        root_path = self.roots.get(root_id)
        if root_path is None:
            out["reason"] = "root_not_configured"
            return out
        out["device_path"] = str(root_path)
        if not Path(root_path).is_dir():
            out["reason"] = "path_unavailable"
            return out
        out["available"] = True
        return out
