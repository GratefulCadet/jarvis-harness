"""AgentSession 도메인 — Task × Agent × 지속 conversation의 장기 바인딩.

역할 경계 (마일스톤 계약 §2):
- Task        = 무엇을 해야 하는가 (TaskStore, tasks.md — 여기서 다루지 않는다)
- AgentSession = 특정 목적을 장기간 담당하는 AI conversation/context (이 파일)
- AgentRun    = 그 Session 안에서 실제로 수행한 한 번의 실행 (agent_runs.py)

관계: Project → Task → AgentSession → AgentRun. Task 하나에 여러 Session이
붙을 수 있다(Freebuff 구현 + Codex review 같은 조합). Task markdown은 Session을
모른다 — Session이 task_id를 참조하는 단방향이다(persistence 계약 §4).

설계 원칙:
- persistence는 기존 JSON store 관례 그대로: versioned 파일 1개
  (<memory_dir>/agent_sessions.json), tmp + os.replace 원자 교체, 손상 시
  빈 상태 복구(agent_runs.json과 동일 — crash 금지).
- session_id는 내부 ID(as-<hex8>), external_session_id는 vendor ID
  (Freebuff thread UUID 등). vendor 이름을 스키마에 박지 않는다 — Codex나
  Local Qwen은 external이 null이어도 된다.
- 이 파일은 "기록/조회"만 한다. Freebuff orchestrator HTTP 호출은 없다 —
  그것은 freebuff_client.py의 역할이고, bridge가 둘을 조립한다.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AGENT_SESSIONS_VERSION = 1

# agent_type 레지스트리 — UI 셀렉터 옵션과 동일한 순서(§6). LangFlow는 future.
AGENT_TYPES = ("freebuff", "codex", "local_qwen")

DEFAULT_AGENT_TYPE = "freebuff"  # §8 — V1 기본 agent. 자동 routing은 없다.

SESSION_STATUSES = ("active", "archived", "broken", "awaiting_auth")

DEFAULT_PURPOSE = "Agent orchestration"


class AgentSessionError(ValueError):
    """인자/상태 검증 실패 — bridge가 status=error로 되돌려준다."""


def default_agent_sessions_file(memory_dir: Path | str | None) -> Path:
    if memory_dir is None:
        raise AgentSessionError("memory_dir 미설정 — JARVIS state 경로가 없습니다")
    return Path(memory_dir) / "agent_sessions.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_session_id() -> str:
    return f"as-{uuid.uuid4().hex[:8]}"


def _clean_str(value: Any, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:max_len]


# ---------- AgentSessionStore ----------

_UPDATABLE_FIELDS = frozenset({
    "purpose", "status", "model", "reasoning_effort", "last_run_id",
    "run_count", "last_validated_at", "last_validation",
})


class AgentSessionStore:
    """agent_sessions.json 단일 writer — agent_runs.json과 동일한 파생 메타데이터 규약.

    external conversation(Freebuff thread)의 canonical 저장소는 Freebuff 측
    데스크톱 DB다. 이 레지스트리는 JARVIS가 알아야 할 참조(identity/목적/상태)
    만 저장한다 — Freebuff 내부를 미러링하지 않는다.
    """

    def __init__(self, registry_file: Path | str) -> None:
        self.registry_file = Path(registry_file)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}  # 손상된 레지스트리 → 빈 상태 (crash 금지)
        sessions = raw.get("sessions")
        return dict(sessions) if isinstance(sessions, dict) else {}

    def save(self, sessions: dict[str, dict[str, Any]]) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": AGENT_SESSIONS_VERSION, "sessions": sessions}
        fd, tmp_name = tempfile.mkstemp(dir=str(self.registry_file.parent), suffix=".tmp")
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

    def create_session(
        self,
        *,
        agent_type: str,
        task_id: str | None = None,
        project_id: str | None = None,
        purpose: str | None = None,
        workspace_root_id: str | None = None,
        workspace: str | None = None,
        external_session_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        if agent_type not in AGENT_TYPES:
            raise AgentSessionError(
                f"알 수 없는 agent_type: {agent_type!r}. 지원: {list(AGENT_TYPES)}"
            )
        if agent_type == "freebuff" and not (external_session_id and str(external_session_id).strip()):
            raise AgentSessionError(
                "freebuff 세션은 기존 thread UUID(external_session_id)가 필요합니다 — "
                "V1은 zero-message draft 생성을 지원하지 않는다(마일스톤 §13)"
            )
        if external_session_id is not None:
            external_session_id = _clean_str(external_session_id, 128)
            if not external_session_id:
                raise AgentSessionError("external_session_id 형식이 잘못되었습니다")
        session_id = _new_session_id()
        with self._lock:
            sessions = self._load()
            while session_id in sessions:
                session_id = _new_session_id()
            session: dict[str, Any] = {
                "session_id": session_id,
                "agent_type": agent_type,
                "project_id": _clean_str(project_id, 120) or None,
                "task_id": _clean_str(task_id, 120) or None,
                "purpose": _clean_str(purpose, 200) or DEFAULT_PURPOSE,
                "workspace_root_id": _clean_str(workspace_root_id, 120) or None,
                "workspace": _clean_str(workspace, 500) or None,
                "external_session_id": external_session_id,
                "model": _clean_str(model, 120) or None,
                "reasoning_effort": _clean_str(reasoning_effort, 40) or None,
                "status": "active",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "last_run_id": None,
                "run_count": 0,
                "last_validated_at": None,
                "last_validation": None,
            }
            sessions[session_id] = session
            self.save(sessions)
        return dict(session)

    def get(self, session_id: str) -> dict[str, Any] | None:
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        session = self._load().get(session_id.strip())
        return dict(session) if session else None

    def list_sessions(
        self,
        *,
        agent_type: str | None = None,
        task_id: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sessions = self._load()
        ordered = sorted(
            sessions.values(), key=lambda s: str(s.get("updated_at") or ""), reverse=True
        )
        result: list[dict[str, Any]] = []
        for session in ordered:
            if agent_type is not None and session.get("agent_type") != agent_type:
                continue
            if task_id is not None and session.get("task_id") != task_id:
                continue
            if not include_archived and session.get("status") == "archived":
                continue
            result.append(dict(session))
            if len(result) >= max(1, min(int(limit), 200)):
                break
        return result

    def update(self, sid: str, **fields: Any) -> dict[str, Any]:
        # 첫 매개변수 이름이 session_id가 아니다 — renderer가 fields에
        # session_id를 몰래 넣어도 kwargs 충돌 TypeError 대신 화이트리스트
        # AgentSessionError로 거부되어야 한다.
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise AgentSessionError(f"알 수 없는 session 필드: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in SESSION_STATUSES:
            raise AgentSessionError(
                f"알 수 없는 session status: {fields['status']!r}. 지원: {list(SESSION_STATUSES)}"
            )
        with self._lock:
            sessions = self._load()
            session = sessions.get(sid)
            if session is None:
                raise AgentSessionError(f"알 수 없는 session: {sid}")
            session.update(fields)
            session["updated_at"] = _now_iso()
            self.save(sessions)
            return dict(session)

    def archive(self, session_id: str) -> dict[str, Any]:
        """active → archived. 삭제가 아니라 상태 변경 — history에 남는다(§15)."""
        session = self.get(session_id)
        if session is None:
            raise AgentSessionError(f"알 수 없는 session: {session_id}")
        return self.update(session_id, status="archived")

    def record_run_link(self, session_id: str, run_id: str) -> dict[str, Any]:
        """AgentRun이 이 Session에서 시작되었음을 기록(§5). 카운트는 여기서만 증가."""
        if not isinstance(run_id, str) or not run_id.strip():
            raise AgentSessionError("run_id가 비어 있습니다")
        with self._lock:
            sessions = self._load()
            session = sessions.get(session_id)
            if session is None:
                raise AgentSessionError(f"알 수 없는 session: {session_id}")
            session["last_run_id"] = run_id.strip()
            session["run_count"] = int(session.get("run_count") or 0) + 1
            session["updated_at"] = _now_iso()
            self.save(sessions)
            return dict(session)
