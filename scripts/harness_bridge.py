from __future__ import annotations

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
- 기본 memory_dir는 격리 scratch(data/electron_scratch). 실 사용자 memory로
  가려면 명시적으로 JARVIS_BRIDGE_MEMORY_DIR 환경변수를 지정해야 한다.
- confirm은 제안된 call을 그대로 confirmed_calls로 넘긴다 — 렌더러가 인자를
  바꿀 수 없다 (exact-call confirm, §8.3-2).
- reject는 모델 호출 없이 0변이로 끝낸다.
- bridge는 Freebuff가 아니라 Electron(JARVIS)이 spawn하는 자식 프로세스다.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from harness.client import HarnessClient
from harness.config import PROJECT_ROOT, HarnessConfig
from harness.tools.discovery import Discovery
from harness.tools.file_store import DEFAULT_MAX_DEPTH
from harness.tools.memory_context import MemoryContextReader
from harness.tools.page_store import PageStore
from harness.tools.resource_links import (
    ProjectResources,
    ResourceLinkError,
    default_links_file,
    relation_label,
)
from harness.tools.task_store import TaskStore, TaskValidationError
from harness.tools.workspace import WorkspaceManager, default_registry_file

DEFAULT_PROJECT = "jarvis-app"
DEFAULT_SCRATCH = PROJECT_ROOT / "data" / "electron_scratch"

ReadLine = Callable[[], str]
WriteLine = Callable[[str], None]


def resolve_memory_dir(configured: Path | None) -> tuple[Path, bool]:
    """memory_dir 결정. 기본은 격리 scratch(data/electron_scratch).

    JARVIS_BRIDGE_MEMORY_DIR이 설정되면 그것을 우선한다 (실 memory opt-in).
    반환: (memory_dir, scratch 여부)
    """
    env_dir = os.environ.get("JARVIS_BRIDGE_MEMORY_DIR")
    if env_dir:
        return Path(env_dir), False
    if configured is not None:
        # 명시적으로 전달된 config(테스트 등)는 그대로 사용
        return configured, False
    return DEFAULT_SCRATCH, True


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
                } for task in tasks],
                # PROJECTS의 semantic projection — persisted ResourceLink만
                # 보인다. 검색 유사성으로 자동 링크를 만들지 않는다(PART H·L).
                *_project_resources_children(client, project_id),
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
    """SYSTEM MAP Complete/Reopen → canonical TaskStore write (deterministic, no LLM).

    Direct explicit user action — no Permission Gate round-trip.
    Reuses TaskStore.set_done as single writer; does not duplicate parser logic.
    Flips `- [ ]` ↔ `- [x]` and returns the updated entry. model 호출 없음.
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
    # allow both `task_id` and legacy `taskId`/`id` keys for robustness
    task_id = payload.get("task_id") or payload.get("taskId") or payload.get("id")
    done = payload.get("done")

    # `done` must be boolean — bridge validates before TaskStore to give clear error
    if not isinstance(done, bool):
        return {
            "type": "response",
            "id": request_id,
            "status": "error",
            "error": "done은 boolean이어야 합니다",
        }

    task_file = client.config.task_file or (Path(memory_dir) / "tasks.md")
    store = TaskStore(task_file, memory_dir=memory_dir)
    try:
        result = store.set_done(project_id, task_id, done)
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


def _project_resources_children(
    client: HarnessClient,
    project_id: str,
) -> list[dict[str, Any]]:
    """tree_snapshot용 — 프로젝트의 Resource 링크를 relation 그룹 노드로 변환.

    ResourceLink는 FileRef identity만 저장하므로 현재 locator는 읽을 때
    resolve한다 — rename/move reconciliation 결과가 링크 재작성 없이 반영된다(G).
    """
    resources = _resources(client)
    if resources is None:
        return []
    try:
        result = resources.list_project_resources(project_id)
    except ResourceLinkError:
        return []
    if not result["resources"]:
        return []

    by_relation: dict[str, list[dict[str, Any]]] = {}
    for res in result["resources"]:
        by_relation.setdefault(res["relation"], []).append(res)

    children: list[dict[str, Any]] = []
    for relation in sorted(by_relation):
        children.append({
            "id": f"resources:{project_id}:{relation}",
            "type": "resource_group",
            "title": relation_label(relation),
            "status": "active",
            "children": [
                {
                    # id는 stable FileRef identity — path가 아니다(PART H).
                    "id": f"resfile:{res['file']['id']}",
                    "type": "project_resource",
                    "title": res["file"].get("name") or res["file"]["id"],
                    "status": res["file"].get("status", "ok"),
                    "file": res["file"],
                    "link_id": res["link_id"],
                }
                for res in sorted(
                    by_relation[relation], key=lambda r: r["file"].get("name", "")
                )
            ],
        })
    return children


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

        if message_type == "link_project_file":
            # deterministic canonical write — 명시적 사용자 구조 행동(PART F). 모델 호출 없음.
            return _link_project_file(client, request_id, msg)

        if message_type == "list_project_resources":
            # read-only — 프로젝트 리소스 + 현재 locator resolve. 모델 호출 없음.
            return _list_project_resources(client, request_id, msg)

        if message_type == "unlink_project_file":
            # deterministic metadata cleanup — JARVIS 메타데이터만 제거(K-13).
            return _unlink(client, request_id, msg)

        if message_type == "shutdown":
            raise SystemExit(0)

        if message_type in ("chat", "confirm"):
            scratch = str(client.config.memory_dir).replace("\\", "/").startswith(
                str(PROJECT_ROOT).replace("\\", "/") + "/data/"
            )
            trace_info_kwargs: dict[str, Any] = {}

            if message_type == "chat":
                text = msg.get("text")
                if not isinstance(text, str) or not text.strip():
                    return {
                        "type": "response", "id": request_id,
                        "status": "error", "error": "chat 메시지의 text가 비어 있습니다",
                    }
                session.last_text = text
                project_id = msg.get("project_id") or DEFAULT_PROJECT
                messages = [{"role": "user", "content": text}]
                extra: dict[str, Any] = {"project_id": project_id}
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
                confirmed = [tool_call]
                extra = {"confirmed_tool": tool_call.get("name")}

            response = client.chat_with_tools(
                messages,
                confirmed_calls=confirmed,
                metadata={"source": "electron_bridge", **extra},
            )
            trace = _trace_info(client.config, response.trace_id)

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