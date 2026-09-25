"""Electron ↔ Python Harness 브리지 (Task 1 — JARVIS Electron 통합).

프로토콜: stdin/stdout에 JSON 객체 1줄씩 (JSONL). 응답마다 요청의 `id`를
그대로 돌려준다.

요청:
  {"type": "ping"}
  {"type": "chat", "text": "...", "project_id": "..."}
  {"type": "confirm", "tool_call": <이전에 제안된 call 그대로>}
  {"type": "reject", "tool_call": <이전에 제안된 call 그대로>}
  {"type": "shutdown"}

응답:
  {"type": "response", "id": ..., "status": "final", "text": "...",
   "trace_id": "...", "trace_path": "...", "events": [...], "scratch": true}
  {"type": "response", "id": ..., "status": "awaiting_confirmation",
   "tool_call": {...}, "trace_id": ..., "trace_path": ..., "events": [...], "scratch": ...}
  {"type": "response", "id": ..., "status": "rejected", "text": "..."}
  {"type": "response", "id": ..., "status": "error", "error": "..."}

안전:
- 기본 memory_dir는 표준 canonical 사용자 영구 디렉터리다
  (Windows %APPDATA%/jarvis-app/memory, POSIX ~/.config/jarvis-app/memory).
  격리 scratch(data/electron_scratch)는 더 이상 기본값이 아니다 —
  테스트·스모크에서만 JARVIS_USE_SCRATCH=1로 강제한다.
- JARVIS_STATE_DIR / JARVIS_BRIDGE_MEMORY_DIR가 명시적 재정의다.
- 최초 진입 시 scratch에 상태가 있으면 copy-if-absent로 마이그레이션한다
  (projects·tasks·roots·links·file_refs·pages). 기존 사용자 파일은 덮어쓰지 않는다.
- confirm은 제안된 call을 그대로 confirmed_calls로 넘긴다 — 렌더러가 인자를
  바꿀 수 없다 (exact-call confirm, §8.3-2).
- reject는 모델 호출 없이 0변이로 끝낸다.
- bridge는 Freebuff가 아니라 Electron(JARVIS)이 spawn하는 자식 프로세스다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from harness.client import HarnessClient
from harness.config import PROJECT_ROOT, HarnessConfig
from harness.trace import TraceRecorder
from harness.tools.discovery import Discovery
from harness.tools.discovery_tools import build_list_projects_tool
from harness.tools.file_store import DEFAULT_MAX_DEPTH
from harness.tools.memory_context import MemoryContextReader
from harness.tools.page_store import PageStore
from harness.tools.resource_links import (
    ProjectResources,
    ResourceLinkError,
    default_links_file,
    relation_label,
)
from harness.tools.project_workspaces import (
    ProjectWorkspaceError,
    ProjectWorkspaces,
)
from harness.tools.task_store import TaskStore, TaskValidationError
from harness.tools.workspace import WorkspaceManager, default_registry_file
from harness.tools.workspace_roots import (
    WorkspaceRootError,
    WorkspaceRoots,
    default_workspace_roots_file,
    resolve_effective_roots,
)

DEFAULT_PROJECT = "jarvis-app"
DEFAULT_SCRATCH = PROJECT_ROOT / "data" / "electron_scratch"

ReadLine = Callable[[], str]
WriteLine = Callable[[str], None]


def get_canonical_user_state_dir() -> Path:
    """운영체제별 표준 영구 사용자 memory 디렉터리 경로 반환.
    - Windows: %APPDATA%/jarvis-app/memory
    - Linux/macOS: ~/.config/jarvis-app/memory 또는 XDG_CONFIG_HOME
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "jarvis-app" / "memory"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "jarvis-app" / "memory"


# canonical JARVIS state 파일 — <memory_dir> 아래 고정 관례 경로.
# 각 항목은 아래 모듈의 default_* 헬퍼가 같은 경로를 사용한다.
#   projects.md / tasks.md      — memory_context / task_store
#   workspace_roots.json         — workspace_roots
#   project_workspaces.json      — project_workspaces
#   resource_links.json          — resource_links (Task↔File 연결, M2)
#   file_refs.json               — workspace (FileRef identity)
#   page_identity.json           — page_store (pages/의 형제)
STATE_FILES: tuple[str, ...] = (
    "projects.md",
    "tasks.md",
    "workspace_roots.json",
    "project_workspaces.json",
    "resource_links.json",
    "file_refs.json",
    "page_identity.json",
)

# canonical 디렉터리 — pages/ (Knowledge Markdown). 디렉터리도 file 단위로
# copy-if-absent 하므로 사용자가 일부 페이지를 이미 갖고 있어도 나머지는 온다.
STATE_DIRS: tuple[str, ...] = ("pages",)


def migrate_scratch_to_user_state(
    scratch_dir: Path, user_state_dir: Path
) -> list[str]:
    """scratch의 초기 상태를 사용자 state 디렉터리로 안전하게 최초 마이그레이션한다.

    copy-if-absent: 이미 존재하는 사용자 파일은 절대 덮어쓰지 않는다. 연결
    (resource_links)·파일 identity(file_refs)·페이지(pages)까지 포함하므로,
    스크래치에서 만든 사용자의 작업 맥락이 영구 상태로 유실되지 않는다.

    반환: 상대 경로 리스트 (파일은 이름, 디렉터리는 "pages/파일명" 형태)
    """
    if not scratch_dir.is_dir():
        return []
    migrated: list[str] = []
    user_state_dir.mkdir(parents=True, exist_ok=True)

    for name in STATE_FILES:
        src = scratch_dir / name
        dst = user_state_dir / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            migrated.append(name)

    for name in STATE_DIRS:
        src_root = scratch_dir / name
        if not src_root.is_dir():
            continue
        for src in sorted(src_root.rglob("*")):
            if not src.is_file():
                continue
            relative = src.relative_to(scratch_dir)
            dst = user_state_dir / relative
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            migrated.append(relative.as_posix())

    return migrated


def resolve_memory_dir(configured: Path | None) -> tuple[Path, bool]:
    """memory_dir 결정.
    우선순위:
    1. JARVIS_BRIDGE_MEMORY_DIR 또는 JARVIS_STATE_DIR 환경변수 (명시적 재정의)
    2. configured (코드 또는 단위 테스트에서 직접 전달한 경우)
    3. JARVIS_USE_SCRATCH=1 환경변수 (테스트/스모크/격리 scratch 강제)
    4. 기본값: 표준 canonical 사용자 영구 디렉터리 (%APPDATA%/jarvis-app/memory 등).
       최초 생성 시 scratch의 초기 템플릿/작업 상태를 안전하게 마이그레이션한다.
    반환: (memory_dir, scratch 여부)
    """
    env_dir = os.environ.get("JARVIS_STATE_DIR") or os.environ.get("JARVIS_BRIDGE_MEMORY_DIR")
    if env_dir:
        return Path(env_dir), False

    if configured is not None:
        # 명시적으로 전달된 config(테스트 등)는 그대로 사용
        return configured, False

    use_scratch = os.environ.get("JARVIS_USE_SCRATCH", "").lower() in ("1", "true", "yes")
    if use_scratch:
        return DEFAULT_SCRATCH, True

    user_state = get_canonical_user_state_dir()
    # 최초 진입 시 scratch 상태가 있으면 무손실 복사(존재하지 않는 파일만)
    if DEFAULT_SCRATCH.is_dir():
        migrate_scratch_to_user_state(DEFAULT_SCRATCH, user_state)

    return user_state, False


def ensure_scratch_seed(memory_dir: Path) -> None:
    """scratch memory에 projects.md가 없으면 기본 Active 프로젝트를 시드한다."""
    projects = memory_dir / "projects.md"
    if not projects.exists():
        memory_dir.mkdir(parents=True, exist_ok=True)
        projects.write_text(
            "# Projects\n\n## Active\n\n"
            "- jarvis-app: Electron JARVIS (PiP + Command Center)\n"
            "- local-jarvis: 로컬 우선 개인 AI 비서\n",
            encoding="utf-8",
        )


def build_config() -> HarnessConfig:
    config = HarnessConfig.load()  # runtime/model/trace는 configs/harness.yaml
    memory_dir, _ = resolve_memory_dir(None)
    config.memory_dir = memory_dir
    config.task_file = memory_dir / "tasks.md"
    return apply_bridge_defaults(config)


def apply_bridge_defaults(config: HarnessConfig) -> HarnessConfig:
    """bridge 대화 기본값 — Qwen3-8B는 tool call + 한국어 답변을 240토큰 안에
    못 끝내는 경우가 있어 충분한 출력 예산을 준다 (환경변수로 조정 가능)."""
    config.num_predict = int(
        os.environ.get("JARVIS_BRIDGE_NUM_PREDICT", "1200")
    )
    return config


def build_client(config: HarnessConfig | None = None) -> HarnessClient:
    return HarnessClient(config or build_config())


def _serialize_call(call: Any) -> dict[str, Any]:
    return {
        "name": call.name,
        "arguments": dict(call.arguments or {}),
        "id": call.id,
    }


def _trace_info(config: HarnessConfig, trace_id: str) -> dict[str, Any]:
    if not trace_id or not config.trace_dir.is_dir():
        return {"trace_id": trace_id, "trace_path": None, "events": []}
    for path in config.trace_dir.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if entry.get("trace_id") != trace_id:
            continue
        events = []
        for item in entry.get("tool_results", []):
            call = item.get("call", {})
            result = item.get("result", {})
            events.append({
                "kind": "tool",
                "name": call.get("name"),
                "arguments": call.get("arguments"),
                "ok": result.get("ok"),
                "requires_confirmation": result.get("requires_confirmation", False),
                "data": result.get("data"),
                "error": result.get("error"),
            })
        return {
            "trace_id": trace_id,
            "trace_path": str(path),
            "events": events,
        }
    return {"trace_id": trace_id, "trace_path": None, "events": []}


def _is_scratch_dir(path: Any) -> bool:
    """data/ 아래 격리 scratch인지 판정 (응답의 scratch 플래그 공용)."""
    return str(path).replace("\\", "/").startswith(
        str(PROJECT_ROOT).replace("\\", "/") + "/data/"
    )


def _tree_snapshot(
    client: HarnessClient,
    request_id: Any,
) -> dict[str, Any]:
    """실제 JARVIS state → 구조화된 트리 스냅샷 (read-only).

    같은 원천(MemoryContextReader.list_projects + TaskStore.list_tasks)을
    쓰므로 list_current_tasks / create_task와 일관된다.
    """
    memory_dir = client.config.memory_dir
    if memory_dir is None or not Path(memory_dir).is_dir():
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": "memory_dir 미설정 — JARVIS memory 경로가 없습니다",
        }

    task_file = client.config.task_file or (Path(memory_dir) / "tasks.md")
    reader = MemoryContextReader(memory_dir)
    store = TaskStore(task_file, memory_dir=memory_dir)

    tree: list[dict[str, Any]] = []
    for project in reader.list_projects():
        project_id = project["id"]
        try:
            tasks = store.list_tasks(project_id)
        except Exception:
            # 한 프로젝트의 파싱 오류가 전체 스냅샷을 막지 않도록 안전하게 빈 목록
            tasks = []
        tree.append({
            "id": project_id,
            "type": "project",
            "title": project["title"] or project_id,
            "status": "active",
            "children": [
                *[{
                    "id": task["id"],
                    "type": "task",
                    "title": task["title"],
                    "reason": task.get("reason", ""),
                    "status": "done" if task.get("done") else "open",
                    "parent_id": project_id,
                    "children": _task_resources_children(client, task["id"]),
                } for task in tasks],
                # PROJECTS의 semantic projection — persisted ResourceLink만
                # 보인다. 검색 유사성으로 자동 링크를 만들지 않는다(PART H·L).
                *_project_resources_children(client, project_id),
                # PART F — primary workspace semantic 노드 (관계가 있을 때만).
                # Workspace는 root_id 참조일 뿐 중복 Project가 아니다.
                *_project_workspace_children(client, project_id),
            ],
        })

    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "tree": tree,
        "scratch": _is_scratch_dir(memory_dir),
    }


def _update_task(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """SYSTEM MAP task mutation → canonical TaskStore write (deterministic, no LLM).

    Supports:
    - done/open toggle (done: bool)
    - title/reason edit (title?: str, reason?: str)
    - both simultaneously

    Direct explicit user action — no Permission Gate round-trip.
    """
    memory_dir = client.config.memory_dir
    if memory_dir is None or not Path(memory_dir).is_dir():
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": "memory_dir 미설정 — JARVIS memory 경로가 없습니다",
        }

    project_id = payload.get("project_id")
    task_id = payload.get("task_id") or payload.get("taskId") or payload.get("id")
    done = payload.get("done")
    title = payload.get("title")
    reason = payload.get("reason")

    task_file = client.config.task_file or (Path(memory_dir) / "tasks.md")
    store = TaskStore(task_file, memory_dir=memory_dir)

    try:
        # done toggle
        if isinstance(done, bool):
            result = store.set_done(project_id, task_id, done)
        # title/reason edit
        elif title is not None or reason is not None:
            result = store.update_task(project_id, task_id, title=title, reason=reason)
        else:
            return {
                "type": "response",
                "id": request_id,
                "status": "error",
                "error": "done, title, reason 중 하나 이상 필요합니다",
            }
    except (TaskValidationError, ValueError) as exc:
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # M5 — 세션 연속성 신호 (관찰 전용).
    _record_activity(
        client,
        project_id=project_id,
        operation="update_task",
        arguments={
            "project_id": project_id,
            "task_id": task_id,
            "done": bool(done),
        },
        summary=(
            f"작업 완료 처리: {result.get('title') or task_id}"
            if done
            else f"작업 다시 열기: {result.get('title') or task_id}"
        ),
        active_file=_active_file_context(payload),
    )

    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "task": result,
        "scratch": _is_scratch_dir(memory_dir),
    }


def _delete_task(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """SYSTEM MAP Task Delete → canonical TaskStore deletion (deterministic, no LLM).

    Destructive — semantically distinct from update_task, so it uses a dedicated
    bridge message. Direct explicit user action (one UI confirmation) — no
    Permission Gate round-trip. TaskStore is the single writer.
    """
    memory_dir = client.config.memory_dir
    if memory_dir is None or not Path(memory_dir).is_dir():
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": "memory_dir 미설정 — JARVIS memory 경로가 없습니다",
        }

    project_id = payload.get("project_id")
    task_id = payload.get("task_id") or payload.get("taskId") or payload.get("id")

    task_file = client.config.task_file or (Path(memory_dir) / "tasks.md")
    store = TaskStore(task_file, memory_dir=memory_dir)

    try:
        result = store.delete_task(project_id, task_id)
    except (TaskValidationError, ValueError) as exc:
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # M5 — 세션 연속성 신호 (관찰 전용).
    _record_activity(
        client,
        project_id=project_id,
        operation="delete_task",
        arguments={"project_id": project_id, "task_id": task_id},
        summary=f"작업 삭제: {result.get('title') or task_id}",
        active_file=_active_file_context(payload),
    )

    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "task": result,
        "scratch": _is_scratch_dir(memory_dir),
    }


def _create_task(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """SYSTEM MAP Add Task → canonical TaskStore write (deterministic, no LLM).

    Direct explicit user action (Tree UI click) — no Permission Gate round-trip.
    TaskStore is the single writer; this handler is a thin deterministic bridge
    without duplicating TaskStore logic. model 호출 없음.
    """
    memory_dir = client.config.memory_dir
    if memory_dir is None or not Path(memory_dir).is_dir():
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": "memory_dir 미설정 — JARVIS memory 경로가 없습니다",
        }

    project_id = payload.get("project_id")
    title = payload.get("title")
    # reason은 선택 — 없으면 TaskStore가 빈 문자열로 정규화한다
    reason = payload.get("reason")

    task_file = client.config.task_file or (Path(memory_dir) / "tasks.md")
    store = TaskStore(task_file, memory_dir=memory_dir)
    try:
        result = store.create(project_id, title, reason)
    except (TaskValidationError, ValueError) as exc:
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": str(exc),
        }
    except Exception as exc:  # 방어적 — 어떤 실패든 JSONL로 반환
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # M5 — 생성된 task_id까지 신호에 남겨야 복귀 시 방금 만든 작업이 이어진다.
    _record_activity(
        client,
        project_id=project_id,
        operation="create_task",
        arguments={
            "project_id": project_id,
            "task_id": result.get("id"),
            "title": result.get("title"),
        },
        summary=f"새 작업 추가: {result.get('title') or result.get('id')}",
        active_file=_active_file_context(payload),
    )

    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "task": result,
        "scratch": _is_scratch_dir(memory_dir),
    }


def _discovery(client: HarnessClient) -> Discovery:
    """bridge용 Discovery adapter — harness_bridge 설정(격리 scratch 포함)을 쓴다."""
    return Discovery(
        client.config.memory_dir,
        task_file=client.config.task_file,
        pages_dir=client.config.pages_dir,
        file_roots=client.config.file_roots,
    )


def _workspace_roots_manager(client: HarnessClient) -> WorkspaceRoots | None:
    if client.config.memory_dir is None:
        return None
    try:
        return WorkspaceRoots(
            default_workspace_roots_file(Path(client.config.memory_dir)),
            memory_dir=Path(client.config.memory_dir),
        )
    except WorkspaceRootError:
        return None


def _workspace(client: HarnessClient) -> WorkspaceManager | None:
    """bridge용 identity 계층 — 레지스트리는 <memory_dir>/file_refs.json (PART D)."""
    if client.config.memory_dir is None:
        return None
    discovery = _discovery(client)
    if not discovery.files.roots:
        return None
    return WorkspaceManager(
        discovery.files, default_registry_file(Path(client.config.memory_dir))
    )


def _resources(client: HarnessClient) -> ProjectResources | None:
    """bridge용 ResourceLink 계층 — 레지스트리는 <memory_dir>/resource_links.json.

    WorkspaceManager가 없으면(승인 루트 없음) FileRef 검증이 불가능하므로 None.
    """
    if client.config.memory_dir is None:
        return None
    workspace = _workspace(client)
    if workspace is None:
        return None
    return ProjectResources(
        default_links_file(Path(client.config.memory_dir)),
        client.config.memory_dir,
        workspace=workspace,
    )


# ---------- WorkspaceRoots (persistent, picker UX) ----------

def _err(request_id: Any, msg: str) -> dict[str, Any]:
    return {"type": "response", "id": request_id, "status": "error", "error": msg}


def _ok(request_id: Any, client: HarnessClient, **kw: Any) -> dict[str, Any]:
    return {"type": "response", "id": request_id, "status": "ok", "scratch": _is_scratch_dir(client.config.memory_dir), **kw}


def _ws_op(
    client: HarnessClient, request_id: Any, op: Callable[[], Any]
) -> dict[str, Any]:
    """공통 try/except 래퍼 — 도메인 에러는 문자열, 나머지는 형식화."""
    try:
        result = op()
    except WorkspaceRootError as exc:
        return _err(request_id, str(exc))
    except ProjectWorkspaceError as exc:
        return _err(request_id, str(exc))
    except Exception as exc:
        return _err(request_id, f"{type(exc).__name__}: {exc}")
    return result


def _list_workspace_roots(
    client: HarnessClient, request_id: Any, _payload: dict[str, Any]
) -> dict[str, Any]:
    roots_mgr = _workspace_roots_manager(client)
    if roots_mgr is None:
        return _err(request_id, "memory_dir 미설정 — workspace_roots를 조회할 수 없습니다")
    return _ws_op(client, request_id, lambda: _ok(request_id, client, roots=roots_mgr.list_roots()))


def _register_workspace_root(
    client: HarnessClient, request_id: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    device_path = payload.get("device_path") or payload.get("path") or payload.get("folder")
    if not isinstance(device_path, str) or not device_path.strip():
        return _err(request_id, "device_path(폴더 경로)가 필요합니다")
    roots_mgr = _workspace_roots_manager(client)
    if roots_mgr is None:
        return _err(request_id, "memory_dir 미설정 — workspace_roots를 등록할 수 없습니다")
    display_name = payload.get("display_name")
    dn = display_name.strip() if isinstance(display_name, str) and display_name.strip() else None
    return _ws_op(client, request_id, lambda: _ok(request_id, client, **roots_mgr.register(device_path.strip(), dn)))


def _update_workspace_root(
    client: HarnessClient, request_id: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    root_id = payload.get("root_id") or payload.get("id")
    if not isinstance(root_id, str) or not root_id.strip():
        return _err(request_id, "root_id가 필요합니다")
    roots_mgr = _workspace_roots_manager(client)
    if roots_mgr is None:
        return _err(request_id, "memory_dir 미설정")
    kwargs: dict[str, Any] = {}
    for key in ("display_name", "device_path"):
        val = payload.get(key) if key == "display_name" else payload.get(key) or payload.get("path")
        if val is not None:
            if not isinstance(val, str):
                return _err(request_id, f"{key}는 문자열이어야 합니다")
            kwargs[key] = val
    if not kwargs:
        return _err(request_id, "변경할 필드가 없습니다 (display_name 또는 device_path)")
    return _ws_op(client, request_id, lambda: _ok(request_id, client, **roots_mgr.update(root_id.strip(), **kwargs)))


def _remove_workspace_root(
    client: HarnessClient, request_id: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    root_id = payload.get("root_id") or payload.get("id")
    if not isinstance(root_id, str) or not root_id.strip():
        return _err(request_id, "root_id가 필요합니다")
    roots_mgr = _workspace_roots_manager(client)
    if roots_mgr is None:
        return _err(request_id, "memory_dir 미설정")
    return _ws_op(client, request_id, lambda: _ok(request_id, client, **roots_mgr.remove(root_id.strip())))


def _connect_project_workspace(
    client: HarnessClient, request_id: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    """One-gesture: register/reuse root + set project primary workspace (PART F)."""
    project_id = payload.get("project_id")
    device_path = payload.get("device_path") or payload.get("path") or payload.get("folder")
    if not isinstance(project_id, str) or not project_id.strip():
        return _err(request_id, "project_id가 필요합니다")
    if not isinstance(device_path, str) or not device_path.strip():
        return _err(request_id, "device_path(폴더 경로)가 필요합니다")
    memory_dir = client.config.memory_dir
    if memory_dir is None or not Path(memory_dir).is_dir():
        return _err(request_id, "memory_dir 미설정")
    roots_mgr = _workspace_roots_manager(client)
    if roots_mgr is None:
        return _err(request_id, "memory_dir 미설정")
    display_name = payload.get("display_name")
    dn = display_name.strip() if isinstance(display_name, str) and display_name.strip() else None

    def _connect() -> dict[str, Any]:
        reg = roots_mgr.register(device_path.strip(), dn)
        workspaces = _project_workspaces(client)
        if workspaces is None:
            raise ProjectWorkspaceError("memory_dir 미설정 — workspace 관계를 설정할 수 없습니다")
        ws = workspaces.set_project_primary_workspace(project_id.strip(), reg["root"]["id"])
        return _ok(request_id, client,
                   root=reg["root"], root_created=reg["created"],
                   project_id=ws["project_id"], root_id=ws["root_id"],
                   updated=ws["updated"], workspace=ws["workspace"])

    return _ws_op(client, request_id, _connect)


def _project_workspaces(client: HarnessClient) -> ProjectWorkspaces | None:
    """bridge용 Project→Workspace 관계 계층 — <memory_dir>/project_workspaces.json.

    Discovery가 이미 같은 인스턴스를 만든다. memory_dir만 있으면 관계 읽기가
    가능해야 하므로(다른 디바이스에서 설정한 관계 → available:false) 루트 유무로
    None을 반환하지 않는다.
    """
    if client.config.memory_dir is None:
        return None
    discovery = _discovery(client)
    assert discovery.project_workspaces is not None
    return discovery.project_workspaces


def _project_workspace_children(
    client: HarnessClient,
    project_id: str,
) -> list[dict[str, Any]]:
    """tree_snapshot용 — Project 아래 semantic Workspace 노드 (PART F).

    WorkspaceRoot를 논리 identity로 참조하는 projection일 뿐 중복 Project가
    아니다. FILES 물리 뷰는 그대로 별도 섹션이다. 관계가 없으면 노드를
    만들지 않는다(fabricate 금지).
    """
    workspaces = _project_workspaces(client)
    if workspaces is None:
        return []
    try:
        pw = workspaces.get_project_primary_workspace(project_id)
    except ProjectWorkspaceError:
        return []
    if pw is None:
        return []

    return [{
        "id": f"workspace:{project_id}",
        "type": "workspace_group",
        "title": "Workspace",
        "status": "active",
        "children": [{
            # project-scoped id — 두 프로젝트가 같은 root를 primary로 공유해도
            # 트리 노드 id는 유일해야 한다(접힘 상태 키 충돌 방지).
            "id": f"wsroot:{project_id}:{pw['root_id']}",
            "type": "project_workspace",
            "title": pw["display_name"],
            "status": "available" if pw["available"] else "unavailable",
            "root_id": pw["root_id"],
            "available": pw["available"],
            **({"reason": pw["reason"]} if pw.get("reason") else {}),
        }],
    }]


def _task_resources_children(
    client: HarnessClient,
    task_id: str,
) -> list[dict[str, Any]]:
    """tree_snapshot용 Task→File semantic projection."""
    resources = _resources(client)
    if resources is None:
        return []
    try:
        result = resources.list_task_resources(task_id)
    except ResourceLinkError:
        return []
    by_relation: dict[str, list[dict[str, Any]]] = {}
    for resource in result["resources"]:
        by_relation.setdefault(resource["relation"], []).append(resource)
    groups: list[dict[str, Any]] = []
    for relation in sorted(by_relation):
        groups.append({
            "id": f"task-resources:{task_id}:{relation}",
            "type": "task_resource_group",
            "title": relation_label(relation),
            "status": "active",
            "children": [
                {
                    "id": f"task-resource:{resource['file']['id']}",
                    "type": "task_resource",
                    "title": resource["file"].get("name") or resource["file"]["id"],
                    "status": resource["file"].get("status", "ok"),
                    "file": resource["file"],
                    "link_id": resource["link_id"],
                }
                for resource in sorted(
                    by_relation[relation],
                    key=lambda item: item["file"].get("name", ""),
                )
            ],
        })
    return groups


def _link_task_file(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    task_id = payload.get("task_id")
    file_id = payload.get("file_id")
    relation = payload.get("relation") or "reference"
    if not isinstance(task_id, str) or not task_id.strip():
        return _err(request_id, "task_id가 필요합니다")
    if not isinstance(file_id, str) or not file_id.strip():
        return _err(request_id, "file_id(FileRef identity)가 필요합니다")
    if not isinstance(relation, str):
        return _err(request_id, "relation은 문자열이어야 합니다")
    resources = _resources(client)
    if resources is None:
        return _err(request_id, "승인된 파일 루트가 없어 링크를 생성할 수 없습니다")
    try:
        result = resources.link_task_file(task_id.strip(), file_id.strip(), relation.strip())
    except ResourceLinkError as exc:
        return _err(request_id, str(exc))
    except Exception as exc:
        return _err(request_id, f"{type(exc).__name__}: {exc}")
    # M5 — 명시적 link도 세션 신호. 파일 위치까지 남겨 복귀 시 그 자료로 이어가게 한다.
    _record_activity(
        client,
        project_id=_project_of_task(client, task_id),
        operation="link_task_file",
        arguments={
            "task_id": task_id.strip(),
            "file_id": file_id.strip(),
            "relation": relation.strip(),
            **_file_signal(client, file_id),
        },
        summary=f"작업에 자료 연결: {task_id.strip()}",
        active_file=_active_file_context(payload),
    )
    return _ok(request_id, client, created=result["created"], link=result["link"])


def _list_task_resources(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return _err(request_id, "task_id가 필요합니다")
    resources = _resources(client)
    if resources is None:
        return _err(request_id, "승인된 파일 루트가 없어 리소스를 조회할 수 없습니다")
    try:
        result = resources.list_task_resources(task_id.strip())
    except ResourceLinkError as exc:
        return _err(request_id, str(exc))
    return _ok(request_id, client, task_id=result["task_id"], resources=result["resources"])


def _unlink_task_file(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    link_id = payload.get("link_id")
    if not isinstance(link_id, str) or not link_id.strip():
        return _err(request_id, "link_id가 필요합니다")
    resources = _resources(client)
    if resources is None:
        return _err(request_id, "승인된 파일 루트가 없어 링크를 제거할 수 없습니다")
    try:
        result = resources.unlink_task_file(link_id.strip())
    except ResourceLinkError as exc:
        return _err(request_id, str(exc))
    # M5 — 해제된 링크의 task/file은 제거된 link 사본에서 되찾는다.
    removed = result.get("link") or {}
    task_id = removed.get("from_id")
    file_id = removed.get("to_id")
    _record_activity(
        client,
        project_id=_project_of_task(client, task_id),
        operation="unlink_task_file",
        arguments={
            "task_id": task_id,
            "file_id": file_id,
            **_file_signal(client, file_id),
        },
        summary=f"작업 자료 연결 해제: {task_id}",
        active_file=_active_file_context(payload),
    )
    return _ok(request_id, client, removed=result["removed"], link=result["link"])


def _project_resources_children(
    client: HarnessClient,
    project_id: str,
) -> list[dict[str, Any]]:
    """tree_snapshot용 — 프로젝트의 Resource 링크를 relation 그룹 노드로 변환."""
    resources = _resources(client)
    if resources is None:
        return []
    try:
        result = resources.list_project_resources(project_id)
    except ResourceLinkError:
        return []
    by_relation: dict[str, list[dict[str, Any]]] = {}
    for resource in result["resources"]:
        by_relation.setdefault(resource["relation"], []).append(resource)
    return [
        {
            "id": f"resources:{project_id}:{relation}",
            "type": "resource_group",
            "title": relation_label(relation),
            "status": "active",
            "children": [
                {
                    "id": f"resfile:{resource['file']['id']}",
                    "type": "project_resource",
                    "title": resource["file"].get("name") or resource["file"]["id"],
                    "status": resource["file"].get("status", "ok"),
                    "file": resource["file"],
                    "link_id": resource["link_id"],
                }
                for resource in sorted(
                    by_relation[relation],
                    key=lambda item: item["file"].get("name", ""),
                )
            ],
        }
        for relation in sorted(by_relation)
    ]




def _discover_projects(
    client: HarnessClient,
    request_id: Any,
) -> dict[str, Any]:
    """PART B-1 — 모든 프로젝트 + canonical task 수. read-only, 모델 호출 없음."""
    memory_dir = client.config.memory_dir
    if memory_dir is None or not Path(memory_dir).is_dir():
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": "memory_dir 미설정 — JARVIS memory 경로가 없습니다",
        }
    try:
        projects = _discovery(client).list_projects_detailed()
    except Exception as exc:
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "projects": projects,
        "scratch": _is_scratch_dir(memory_dir),
    }


def _search_context(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """PART C — Project/Task/Page/File 통합 검색. read-only, 모델 호출 없음."""
    memory_dir = client.config.memory_dir
    if memory_dir is None or not Path(memory_dir).is_dir():
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": "memory_dir 미설정 — JARVIS memory 경로가 없습니다",
        }
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": "query가 비어 있습니다",
        }
    limit = payload.get("limit") or 10
    try:
        result = _discovery(client).search_context(query, limit=limit)
    except Exception as exc:
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        **result,
        "scratch": _is_scratch_dir(memory_dir),
    }


def _link_project_file(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """PART F — 명시적 사용자 행동: Project→File ResourceLink 생성.

    deterministic canonical write — ProjectResources가 단일 writer.
    Permission Gate 우회가 아니라 정책 그대로: 직접 구조적 사용자 행동은
    결정적 쓰기를, AI 제안은 승인 후 이 핸들러를 통과한다. 모델 호출 없음.
    """
    project_id = payload.get("project_id")
    file_id = payload.get("file_id")
    relation = payload.get("relation") or "reference"

    if not isinstance(project_id, str) or not project_id.strip():
        return {"type": "response", "id": request_id, "status": "error",
                "error": "project_id가 필요합니다"}
    if not isinstance(file_id, str) or not file_id.strip():
        return {"type": "response", "id": request_id, "status": "error",
                "error": "file_id(FileRef identity)가 필요합니다"}
    if not isinstance(relation, str):
        return {"type": "response", "id": request_id, "status": "error",
                "error": "relation은 문자열이어야 합니다"}

    resources = _resources(client)
    if resources is None:
        return {"type": "response", "id": request_id, "status": "error",
                "error": "승인된 파일 루트가 없어 링크를 생성할 수 없습니다"}

    try:
        result = resources.link_project_file(
            project_id.strip(), file_id.strip(), relation.strip()
        )
    except ResourceLinkError as exc:
        return {"type": "response", "id": request_id, "status": "error",
                "error": str(exc)}
    except Exception as exc:  # 방어적 — 어떤 실패든 JSONL로 반환
        return {"type": "response", "id": request_id, "status": "error",
                "error": f"{type(exc).__name__}: {exc}"}

    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "created": result["created"],
        "link": result["link"],
        "scratch": _is_scratch_dir(client.config.memory_dir),
    }


def _list_project_resources(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """PART G — 프로젝트 리소스 읽기. locator는 FileRef에서 resolve. 모델 호출 없음."""
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return {"type": "response", "id": request_id, "status": "error",
                "error": "project_id가 필요합니다"}

    resources = _resources(client)
    if resources is None:
        return {"type": "response", "id": request_id, "status": "error",
                "error": "승인된 파일 루트가 없어 리소스를 조회할 수 없습니다"}

    try:
        result = resources.list_project_resources(project_id.strip())
    except ResourceLinkError as exc:
        return {"type": "response", "id": request_id, "status": "error",
                "error": str(exc)}
    except Exception as exc:
        return {"type": "response", "id": request_id, "status": "error",
                "error": f"{type(exc).__name__}: {exc}"}

    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "project_id": result["project_id"],
        "resources": result["resources"],
        "scratch": _is_scratch_dir(client.config.memory_dir),
    }


def _unlink(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """PART K-13 — 링크 메타데이터만 제거. 사용자 파일은 절대 건드리지 않는다."""
    link_id = payload.get("link_id")
    if not isinstance(link_id, str) or not link_id.strip():
        return {"type": "response", "id": request_id, "status": "error",
                "error": "link_id가 필요합니다"}

    resources = _resources(client)
    if resources is None:
        return {"type": "response", "id": request_id, "status": "error",
                "error": "승인된 파일 루트가 없어 링크를 제거할 수 없습니다"}

    try:
        result = resources.unlink(link_id.strip())
    except ResourceLinkError as exc:
        return {"type": "response", "id": request_id, "status": "error",
                "error": str(exc)}
    except Exception as exc:
        return {"type": "response", "id": request_id, "status": "error",
                "error": f"{type(exc).__name__}: {exc}"}

    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "removed": result["removed"],
        "link": result["link"],
        "scratch": _is_scratch_dir(client.config.memory_dir),
    }


# ---------- Project Primary Workspace (§4 — PROJECT PRIMARY WORKSPACE V1) ----------

def _set_project_workspace(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """PART I — Project → primary WorkspaceRoot 설정 (canonical metadata write)."""
    project_id = payload.get("project_id")
    root_id = payload.get("root_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return _err(request_id, "project_id가 필요합니다")
    if not isinstance(root_id, str) or not root_id.strip():
        return _err(request_id, "root_id(논리 root identity)가 필요합니다")
    workspaces = _project_workspaces(client)
    if workspaces is None:
        return _err(request_id, "memory_dir 미설정 — workspace 관계를 설정할 수 없습니다")
    return _ws_op(client, request_id, lambda: _ok(
        request_id, client, **workspaces.set_project_primary_workspace(project_id.strip(), root_id.strip())
    ))


def _get_project_workspace(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """PART D·E — 관계 + 현재 디바이스 가용성 조회."""
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return _err(request_id, "project_id가 필요합니다")
    workspaces = _project_workspaces(client)
    if workspaces is None:
        return _err(request_id, "memory_dir 미설정 — workspace 관계를 조회할 수 없습니다")
    return _ws_op(client, request_id, lambda: _ok(
        request_id, client,
        project_id=project_id.strip(),
        workspace=workspaces.get_project_primary_workspace(project_id.strip()),
    ))


def _clear_project_workspace(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """PART J-8 — 관계 메타데이터만 제거."""
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return _err(request_id, "project_id가 필요합니다")
    workspaces = _project_workspaces(client)
    if workspaces is None:
        return _err(request_id, "memory_dir 미설정 — workspace 관계를 해제할 수 없습니다")
    return _ws_op(client, request_id, lambda: _ok(
        request_id, client, **workspaces.clear_project_primary_workspace(project_id.strip())
    ))


def _files_snapshot(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """PART D/E — 승인된 파일 루트의 경계 있는 트리 스냅샷. read-only, 모델 호출 없음.

    SYSTEM MAP FILES 섹션용. 루트가 없으면 status=ok + roots:[] — renderer가
    '루트 없음' 상태를 표시한다(오류 아님).
    """
    discovery = _discovery(client)
    root = payload.get("root")
    relative = payload.get("path")
    depth = payload.get("depth") or DEFAULT_MAX_DEPTH
    try:
        if root and str(root).strip():
            root_name, _ = discovery.files.resolve_root(root)
            roots = {root_name: discovery.files.roots[root_name]}
        else:
            roots = dict(discovery.files.roots)
    except Exception as exc:
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": str(exc),
        }
    workspace = _workspace(client)
    sections: list[dict[str, Any]] = []
    for name in sorted(roots):
        try:
            tree = discovery.files.list_tree(
                root=name,
                relative=relative if name == (root or name) else None,
                max_depth=depth,
            )
            if workspace is not None:
                # identity 부착 전 최신 스캔 — rename/move가 FILES에 반영된다(PART H)
                workspace.scan_root(name)
                for entry in tree.get("entries", []):
                    if entry.get("type") == "file":
                        workspace.enrich_entry(entry, name)
        except Exception as exc:
            tree = {"root": name, "path": relative or "", "entries": [],
                    "stats": {"dirs": 0, "files": 0, "blocked": 0, "truncated": False},
                    "error": str(exc)}
        sections.append(tree)
    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "roots": sorted(roots),
        "sections": sections,
    }


def _file_read(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    workspace = _workspace(client)
    root = payload.get("root")
    path = payload.get("path")
    if workspace is None:
        return _err(request_id, "승인된 파일 루트가 없습니다")
    if not isinstance(path, str) or not path.strip():
        return _err(request_id, "path가 필요합니다")
    try:
        result = workspace.read_text(root, path.strip())
        result["file_id"] = result.pop("id")
        return _ok(request_id, client, **result)
    except Exception as exc:
        return _err(request_id, str(exc))


def _file_write(
    client: HarnessClient,
    request_id: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    workspace = _workspace(client)
    if workspace is None:
        return _err(request_id, "승인된 파일 루트가 없습니다")
    root_id = payload.get("root_id")
    file_id = payload.get("file_id")
    path = payload.get("path")
    content = payload.get("content")
    revision = payload.get("revision")
    if not all(isinstance(value, str) and value.strip() for value in (root_id, file_id, path)):
        return _err(request_id, "root_id, file_id, path가 필요합니다")
    if not isinstance(content, str):
        return _err(request_id, "content는 문자열이어야 합니다")
    if revision is not None and not isinstance(revision, dict):
        return _err(request_id, "revision은 객체여야 합니다")
    try:
        result = workspace.update_text(
            root_id.strip(), file_id.strip(), path.strip(), content, revision
        )
        result["file_id"] = result.pop("id")
        return _ok(request_id, client, **result)
    except Exception as exc:
        return _err(request_id, str(exc))


def _pages_snapshot(
    client: HarnessClient,
    request_id: Any,
) -> dict[str, Any]:
    """Knowledge Markdown pages → 재귀 트리 스냅샷 (read-only).

    pages_dir이 설정돼 있지 않으면 scratch 기본 위치
    <memory_dir>/pages 를 스캔한다. PageStore가 단일 원천이며,
    이 메시지는 모델 호출 없이 Snapshot만 만든다.
    """
    memory_dir = client.config.memory_dir
    if memory_dir is None or not Path(memory_dir).is_dir():
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": "memory_dir 미설정 — JARVIS memory 경로가 없습니다",
        }

    pages_dir = client.config.pages_dir or (Path(memory_dir) / "pages")
    store = PageStore(pages_dir)
    snapshot = store.snapshot()
    return {
        "type": "response",
        "id": request_id,
        "status": "ok",
        "pages": snapshot["pages"],
        "count": snapshot["count"],
        "conflicts": snapshot["conflicts"],
        "pages_dir": str(pages_dir),
        "scratch": _is_scratch_dir(pages_dir),
    }


class BridgeSession:
    """bridge 상태: 단일 대화의 마지막 user text를 유지해 confirm 맥락을 복원한다."""

    def __init__(self) -> None:
        self.last_text: str | None = None
        self.last_active_file: dict[str, str] | None = None
        # confirm은 project_id를 받지 않는다. 마지막 chat의 프로젝트를 이어받아
        # 복귀 브리핑이 이 프로젝트의 trace로 필터링할 수 있게 한다(M5).
        self.last_project_id: str | None = None
        # Milestone A — 마지막 chat에서 만든 프로젝트 문맥 요약. confirm 경로가
        # 이어받아 모델 컨텍스트에 다시 주입한다(프로젝트 id·목록 재해석 방지).
        self.last_project_context: str | None = None


def _project_context_line(client: HarnessClient, project_id: str) -> str | None:
    """Milestone A — 현재 프로젝트 문맥을 모델용 시스템 메시지 한 개로 요약한다.

    왜 필요한가: bridge는 messages=[user 발화]만 넘긴다. 모델은 유효한 project_id를
    모르고, create_task의 project_id는 registry 검증(Active 목록 대조)을 통과해야
    하므로 모델은 read tool로 목록을 읽어 "추측"해야 한다. 그 사이
    _CONTINUE_AFTER_TOOLS의 "답변으로 전환" 지침이 개입해 write 의도가
    최종 텍스트로 흘러가 버린다. 프로젝트 문맥을 시스템 메시지로 주입하면 모델이
    발화 첫 턴에 올바른 project_id로 create_task를 제안할 수 있다.

    실패해도(None) chat을 막지 않는다 — 모델이 기존 read tool로 스스로 찾는
    기존 경로가 그대로 남는다.
    """
    # fake client(테스트)도 있어 속성 자체를 방어적으로 본다.
    registry = getattr(client, "tools", None)
    tool = registry.get("list_projects") if registry is not None else None
    if tool is None or getattr(tool, "handler", None) is None:
        return None
    try:
        result = tool.handler({})
    except Exception:
        return None
    projects = result.get("projects") if isinstance(result, dict) else None
    if not isinstance(projects, list) or not projects:
        return None
    lines: list[str] = ["Current project context (do not ask the user for these):"]
    current = next(
        (p for p in projects if isinstance(p, dict) and p.get("id") == project_id),
        None,
    )
    if isinstance(current, dict):
        title = current.get("title") or project_id
        open_count = int(current.get("task_count") or 0) - int(
            current.get("done_count") or 0
        )
        lines.append(
            f"- Active project: {project_id} — {title} "
            f"({open_count} open task(s)). This is the user's current project."
        )
    for project in projects:
        if not isinstance(project, dict):
            continue
        pid = project.get("id")
        title = project.get("title") or pid
        if not isinstance(pid, str) or not pid.strip() or pid == project_id:
            continue
        lines.append(f"- Project id {pid} — {title}")
    lines.append(
        "Use these exact ids internally. Never ask the user to confirm, "
        "repeat, or spell out a project."
    )
    return "\n".join(lines)


def _active_file_context(msg: dict[str, Any]) -> dict[str, str] | None:
    """chat/confirm 요청의 active_file 메타데이터를 정화한다.

    렌더러가 보낸 값에서 identity locator 4개(file_id·root_id·path·name)만 남기고
    버린다 — 내용·임의 경로는 신뢰하지 않는다. 모델 컨텍스트는 이 메타데이터만
    받고, 내용은 항상 기존 read 도구가 저장된 파일시스템에서 현재 값으로 읽는다.
    """
    raw = msg.get("active_file")
    if not isinstance(raw, dict):
        return None
    fields: dict[str, str] = {}
    for key in ("file_id", "root_id", "path", "name"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value.strip()
    if not fields.get("root_id") or not fields.get("path"):
        return None
    return fields


# ---------- Session activity trace (M5) ----------

def _project_of_task(client: HarnessClient, task_id: Any) -> str | None:
    """task_id가 속한 project_id를 canonical 상태에서 찾는다 (역조회).

    link/unlink 메시지는 task_id만 받고 project_id를 받지 않는다. 복귀 브리핑은
    프로젝트 단위로 trace를 필터링하므로 여기서 project_id를 알아내야 한다.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    memory_dir = client.config.memory_dir
    if memory_dir is None or not Path(memory_dir).is_dir():
        return None
    task_file = client.config.task_file or (Path(memory_dir) / "tasks.md")
    try:
        store = TaskStore(task_file, memory_dir=memory_dir)
        for project in MemoryContextReader(Path(memory_dir)).list_projects():
            for task in store.list_tasks(project["id"]):
                if task.get("id") == task_id.strip():
                    return project["id"]
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def _file_signal(client: HarnessClient, file_id: Any) -> dict[str, Any]:
    """FileRef id → 복귀 매칭용 파일 신호(path/name).

    resolve_briefing은 파일 '경로'로 세션 신호와 task 링크를 잇는다. FileRef id만
    남기면 매칭할 수 없으므로 canonical locator를 그대로 가져온다. 결정할 수 없으면
    빈 dict — 신호를 지어내지 않는다.
    """
    if not isinstance(file_id, str) or not file_id.strip():
        return {}
    workspace = _workspace(client)
    if workspace is None:
        return {}
    ref = workspace.registry.by_id(file_id.strip())
    if ref is None:
        return {}
    signal: dict[str, Any] = {}
    if ref.relative_path:
        signal["path"] = ref.relative_path
    if ref.name:
        signal["name"] = ref.name
    return signal


def _record_activity(
    client: HarnessClient,
    *,
    project_id: Any,
    operation: str,
    arguments: dict[str, Any],
    summary: str,
    active_file: dict[str, Any] | None = None,
) -> None:
    """deterministic 작업(create/edit/done/delete, link/unlink)을 trace에 기록.

    이 작업들은 모델 tool loop를 거치지 않아 기존 trace에 남지 않았고, 그 결과
    복귀 브리핑의 세션 신호(touched_task_ids)가 런타임에 항상 비어 있었다.
    관찰 전용 부수효과이며, 실패해도 canonical 작업 결과를 막지 않는다.
    """
    if not isinstance(project_id, str) or not project_id.strip():
        return
    if not client.config.trace_enabled:
        return
    try:
        TraceRecorder(client.config.trace_dir).record_activity(
            project_id=project_id.strip(),
            operation=operation,
            arguments=arguments,
            summary=summary,
            active_file=active_file,
        )
    except (OSError, TypeError, ValueError):
        # 기록은 부수효과 — 절대 사용자 작업을 막지 않는다.
        return


# 모델 tool loop으로 task를 만드는 write 도구. M5의 deterministic 기록과 같은
# 목적(세션 신호)이지만 경로가 다르므로 여기서 별도로 다룬다.
_TASK_CREATING_TOOLS = frozenset({"create_task"})


def _record_model_task_writes(
    client: HarnessClient, trace: dict[str, Any], project_id: Any
) -> None:
    """모델이 실제로 만든 task의 생성된 id를 세션 신호로 남긴다.

    M5는 deterministic bridge 작업(create/update/delete, link)을 기록하지만
    모델 tool loop 경로는 다루지 않았다. 그 경로에는 두 가지 문제가 있다.

    1. confirm 응답 metadata에는 project_id가 없다(chat에는 있다). 복귀 브리핑은
       프로젝트 단위로 trace를 걸러내므로 이 trace는 아예 보이지 않았다.
    2. create_task의 인자에는 task_id가 없다 — 모델은 id를 알 수 없고 TaskStore가
       생성한다. 실제 id는 tool result에만 존재한다.

    그래서 "성공적으로 실행된 task 생성"을 응답 events에서 찾아 생성된 id로
    기록한다. 여기서는 결과 데이터를 읽으므로 모델이 모르는 id를 알 수 있다.
    """
    if not isinstance(project_id, str) or not project_id.strip():
        return
    for event in trace.get("events") or []:
        if not isinstance(event, dict):
            continue
        if event.get("name") not in _TASK_CREATING_TOOLS:
            continue
        # 승인 대기에서 막힌 제안과 실패는 상태를 바꾸지 않았다.
        if event.get("requires_confirmation") or event.get("ok") is not True:
            continue
        data = event.get("data") or {}
        task_id = data.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            continue
        _record_activity(
            client,
            project_id=project_id,
            operation="create_task",
            arguments={
                "project_id": project_id,
                "task_id": task_id,
                "title": data.get("title"),
            },
            summary=f"새 작업 추가: {data.get('title') or task_id}",
        )


def handle_message(
    client: HarnessClient,
    session: BridgeSession,
    msg: dict[str, Any],
) -> dict[str, Any]:
    """요청 1개 처리 → 응답 dict. 예외는 모두 status=error로 변환한다."""
    message_type = msg.get("type")
    request_id = msg.get("id")

    try:
        if message_type == "ping":
            return {"type": "response", "id": request_id, "status": "ok"}

        if message_type == "tree_snapshot":
            # read-only — 실제 JARVIS memory(projects.md + tasks.md)를
            # 단일 원천으로 구조화된 트리 스냅샷으로 반환한다. 모델 호출 없음.
            return _tree_snapshot(client, request_id)

        if message_type == "update_task":
            # deterministic canonical write — TaskStore.set_done 단일 writer, 모델 호출 없음.
            return _update_task(client, request_id, msg)

        if message_type == "delete_task":
            # deterministic canonical write — destructive, dedicated message.
            return _delete_task(client, request_id, msg)

        if message_type == "create_task":
            # deterministic canonical write — TaskStore가 단일 writer, 모델 호출 없음.
            return _create_task(client, request_id, msg)

        if message_type == "pages_snapshot":
            # read-only — Knowledge Markdown pages의 재귀 트리. 모델 호출 없음.
            return _pages_snapshot(client, request_id)

        if message_type == "discover_projects":
            # read-only — 전체 프로젝트 나열 (Context Discovery). 모델 호출 없음.
            return _discover_projects(client, request_id)

        if message_type == "search_context":
            # read-only — Project/Task/Page/File 통합 검색. 모델 호출 없음.
            return _search_context(client, request_id, msg)

        if message_type == "files_snapshot":
            # read-only — 승인된 루트의 파일 트리 (SYSTEM MAP FILES). 모델 호출 없음.
            return _files_snapshot(client, request_id, msg)

        if message_type == "file_read":
            return _file_read(client, request_id, msg)

        if message_type == "file_write":
            return _file_write(client, request_id, msg)

        if message_type == "link_project_file":
            # deterministic canonical write — 명시적 사용자 구조 행동(PART F). 모델 호출 없음.
            return _link_project_file(client, request_id, msg)

        if message_type == "link_task_file":
            return _link_task_file(client, request_id, msg)

        if message_type == "list_task_resources":
            return _list_task_resources(client, request_id, msg)

        if message_type == "unlink_task_file":
            return _unlink_task_file(client, request_id, msg)


        if message_type == "list_project_resources":
            # read-only — 프로젝트 리소스 + 현재 locator resolve. 모델 호출 없음.
            return _list_project_resources(client, request_id, msg)

        if message_type == "unlink_project_file":
            return _unlink(client, request_id, msg)

        if message_type == "set_project_workspace":
            return _set_project_workspace(client, request_id, msg)

        if message_type == "get_project_workspace":
            return _get_project_workspace(client, request_id, msg)

        if message_type == "clear_project_workspace":
            return _clear_project_workspace(client, request_id, msg)

        if message_type == "list_workspace_roots":
            return _list_workspace_roots(client, request_id, msg)

        if message_type == "register_workspace_root":
            return _register_workspace_root(client, request_id, msg)

        if message_type == "update_workspace_root":
            return _update_workspace_root(client, request_id, msg)

        if message_type == "remove_workspace_root":
            return _remove_workspace_root(client, request_id, msg)

        if message_type == "connect_project_workspace":
            return _connect_project_workspace(client, request_id, msg)

        if message_type == "shutdown":
            raise SystemExit(0)

        if message_type in ("chat", "confirm"):
            scratch = str(client.config.memory_dir).replace("\\", "/").startswith(
                str(PROJECT_ROOT).replace("\\", "/") + "/data/"
            )
            trace_info_kwargs: dict[str, Any] = {}
            system_messages: list[dict[str, str]] | None = None

            if message_type == "chat":
                text = msg.get("text")
                if not isinstance(text, str) or not text.strip():
                    return {
                        "type": "response", "id": request_id,
                        "status": "error", "error": "chat 메시지의 text가 비어 있습니다",
                    }
                session.last_text = text
                session.last_active_file = _active_file_context(msg)
                project_id = msg.get("project_id") or DEFAULT_PROJECT
                session.last_project_id = project_id
                # Milestone A — 프로젝트 문맥을 시스템 메시지로 주입한다.
                # 모델이 유효한 project_id를 발화 첫 턴에 알게 하는 것이 목적.
                session.last_project_context = _project_context_line(
                    client, project_id
                )
                messages = [{"role": "user", "content": text}]
                system_messages: list[dict[str, str]] | None = None
                if session.last_project_context:
                    system_messages = [{
                        "role": "system",
                        "content": session.last_project_context,
                    }]
                extra: dict[str, Any] = {"project_id": project_id}
                if session.last_active_file:
                    extra["active_file"] = session.last_active_file
                confirmed: list[Any] | None = None
            else:  # confirm
                tool_call = msg.get("tool_call")
                if not isinstance(tool_call, dict) or not tool_call.get("name"):
                    return {
                        "type": "response", "id": request_id,
                        "status": "error",
                        "error": "confirm에는 제안된 tool_call이 필요합니다",
                    }
                text = session.last_text or "승인된 tool call을 실행하고 결과를 알려줘."
                messages = [{"role": "user", "content": text}]
                # confirm은 chat의 프로젝트 문맥을 이어받아 모델 컨텍스트를 동일하게
                # 유지한다 — 최종 담변 생성 시에도 프로젝트 id를 모르는 상태가 아니다.
                system_messages = (
                    [{
                        "role": "system",
                        "content": session.last_project_context,
                    }]
                    if session.last_project_context
                    else None
                )
                confirmed = [tool_call]
                extra = {"confirmed_tool": tool_call.get("name")}
                # M5 — confirm은 렌더러가 project_id를 보내지 않는다. 마지막 chat의
                # 프로젝트를 이어붙이지 않으면 이 trace는 복귀 브리핑에서 보이지 않는다.
                if session.last_project_id:
                    extra["project_id"] = session.last_project_id
                if session.last_active_file:
                    extra["active_file"] = session.last_active_file

            context = (
                {"active_file": extra["active_file"]}
                if "active_file" in extra
                else None
            )

            response = client.chat_with_tools(
                messages,
                confirmed_calls=confirmed,
                context=context,
                system_messages=system_messages,
                metadata={"source": "electron_bridge", **extra},
            )
            trace = _trace_info(client.config, response.trace_id)
            # M5 — 모델이 만든 task의 생성된 id를 세션 신호로 남긴다.
            _record_model_task_writes(
                client, trace, extra.get("project_id") or session.last_project_id
            )

            if response.finish_reason == "awaiting_confirmation":
                blocked = list(response.tool_calls or [])
                if not blocked:
                    return {
                        "type": "response", "id": request_id,
                        "status": "error",
                        "error": "awaiting_confirmation인데 tool_call이 없습니다",
                        **trace,
                        "scratch": scratch,
                    }
                return {
                    "type": "response", "id": request_id,
                    "status": "awaiting_confirmation",
                    "tool_call": _serialize_call(blocked[0]),
                    **trace,
                    "scratch": scratch,
                }

            if response.finish_reason == "stop":
                return {
                    "type": "response", "id": request_id,
                    "status": "final",
                    "text": response.content or "",
                    **trace,
                    "scratch": scratch,
                }

            return {
                "type": "response", "id": request_id,
                "status": "error",
                "error": f"예상하지 못한 finish_reason: {response.finish_reason}",
                **trace,
                "scratch": scratch,
            }

        if message_type == "reject":
            # 0변이 — 모델 호출 없음, Harness에 아무것도 요청하지 않는다.
            return {
                "type": "response", "id": request_id,
                "status": "rejected",
                "text": "거절했습니다. 아무것도 변경되지 않았습니다.",
            }

        return {
            "type": "response", "id": request_id,
            "status": "error",
            "error": f"알 수 없는 message type: {message_type!r}",
        }
    except SystemExit:
        raise
    except Exception as exc:  # 어떤 실패든 JSONL로 반환해 Electron이 ERROR 상태 표시
        return {
            "type": "response", "id": request_id,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_bridge(
    client: HarnessClient,
    read_line: ReadLine,
    write_line: WriteLine,
) -> None:
    """stdin JSONL → 처리 → stdout JSONL 루프."""
    session = BridgeSession()
    for raw in iter(read_line, ""):
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError("요청은 JSON object여야 합니다")
        except (json.JSONDecodeError, ValueError) as exc:
            write_line(json.dumps({
                "type": "response", "id": None,
                "status": "error", "error": f"잘못된 요청: {exc}",
            }, ensure_ascii=False))
            continue
        if msg.get("type") == "shutdown":
            write_line(json.dumps(
                {"type": "response", "id": msg.get("id"), "status": "ok"},
                ensure_ascii=False,
            ))
            break
        response = handle_message(client, session, msg)
        write_line(json.dumps(response, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Electron ↔ Python Harness JSONL bridge (JARVIS runtime child process)."
    )
    parser.add_argument("--config", type=str, default=None, help="harness config YAML 경로")
    args = parser.parse_args(argv)

    if args.config:
        config = HarnessConfig.load(args.config)
        memory_dir, _ = resolve_memory_dir(None)
        config.memory_dir = memory_dir
        config.task_file = memory_dir / "tasks.md"
        config = apply_bridge_defaults(config)
    else:
        config = build_config()

    ensure_scratch_seed(config.memory_dir)
    client = build_client(config)

    # 줄 단위 버퍼링 없이 즉시 flush (Electron main이 라인 단위로 읽음)
    run_bridge(
        client,
        read_line=lambda: sys.stdin.readline(),
        write_line=lambda line: (sys.stdout.write(line + "\n"), sys.stdout.flush()),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())