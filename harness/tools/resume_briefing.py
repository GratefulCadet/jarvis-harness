from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from harness.tools.discovery import Discovery
from harness.tools.memory_context import MemoryContextReader
from harness.tools.registry import Tool
from harness.tools.schemas import resume_briefing as resume_briefing_schema
from harness.tools.task_store import TaskStore

"""Resume Briefing (M1 — "복귀 → 이어서 시작").

사용자가 중단했던 작업을 이어가려 할 때("졸업논문 계속하자") 프로젝트 확정·
진행 중 task·연결된 파일·마지막 활동·다음 행동 제안을 한 번에 조립하는
read-only tool.

North Star (JARVIS_V4_PRODUCT_DIRECTION.md §1·§15):
    사용자가 실제 프로젝트를 중단했다가 돌아와도 JARVIS가 작업 상태와 관련
    자료를 복원하고 바로 다음 행동으로 이어갈 수 있게 한다.

결정적 조립 원칙:
- 모든 필드는 기존 canonical 원천(projects.md · tasks.md · resource_links.json ·
  file_refs.json · trace)에서만 읽는다. 추측·필드 생성 금지.
- next_action는 파생·일시적 표현뿐이다 — Goal/Next Action 영속 스키마를 만들지
  않는다(V4 §6). basis에 산출 근거를 명시한다.
- 이 tool은 read 분류다 — Permission Gate 대상이 아니며 어떤 파일이나 상태도
  변경하지 않는다. 모델은 반환 내용을 자연어로 요약해 사용자에게 제시한다.
"""


_MAX_RESOURCES = 10
_MAX_TASK_RESOURCE_LOOKUPS = 5
_MAX_ACTIVITY = 3
_MAX_COMPLETED = 5
_SUMMARY_EXCERPT_CHARS = 240


def _excerpt(text: str | None, limit: int = _SUMMARY_EXCERPT_CHARS) -> str:
    """trace 응답 요약 발췌 — 공백 정규화 후 명시적 절단(완전한 문장 아님을 표시)."""
    flat = re.sub(r"\s+", " ", (text or "")).strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def _recent_activity(
    trace_dir: Path | None, project_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """trace에서 이 프로젝트의 최근 활동 및 세션 신호(touched tasks, files) 추출.

    반환: (recent_activity_entries, session_signals)
    session_signals:
      - touched_task_ids: 최근 활동에서 조작/참조된 task_id (순서대로, 최신 우선)
      - touched_file_paths: 최근 활동에서 참조된 파일 경로/이름 (순서대로, 최신 우선)
      - last_user_intent: 가장 최근 사용자 발화 텍스트
    """
    if trace_dir is None or not trace_dir.is_dir():
        return [], {}
    entries: list[dict[str, Any]] = []
    touched_task_ids: list[str] = []
    touched_files: list[str] = []
    last_user_intent: str | None = None

    for path in trace_dir.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        req = entry.get("request") or {}
        metadata = req.get("metadata") or {}
        if metadata.get("project_id") != project_id:
            continue

        ts = entry.get("ts") or ""
        tools_used: list[str] = []
        entry_task_ids: list[str] = []
        entry_files: list[str] = []

        # active_file in metadata
        active_file = metadata.get("active_file")
        if isinstance(active_file, dict):
            p = active_file.get("path") or active_file.get("name")
            if p and p not in entry_files:
                entry_files.append(str(p))

        for item in entry.get("tool_results") or []:
            call = item.get("call") or {}
            name = call.get("name")
            if isinstance(name, str) and name and name not in tools_used:
                tools_used.append(name)
            args = call.get("arguments") or {}
            # task_id detection
            tid = args.get("task_id")
            if isinstance(tid, str) and tid and tid not in entry_task_ids:
                entry_task_ids.append(tid)
            # file path / locator detection
            for f_key in ("path", "relative_path", "filename"):
                fval = args.get(f_key)
                if isinstance(fval, str) and fval and fval not in entry_files:
                    entry_files.append(fval)

        # user message text
        user_msg = ""
        for m in req.get("messages") or []:
            if m.get("role") == "user":
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    # 시스템 지시성 프롬프트 ("The internal reads above...") 제외
                    if not content.startswith("The internal reads above"):
                        user_msg = content.strip()

        response = entry.get("response") or {}
        entries.append({
            "ts": ts,
            "turns": entry.get("turns"),
            "tools_used": tools_used,
            "summary": _excerpt(response.get("content")),
            "_task_ids": entry_task_ids,
            "_files": entry_files,
            "_user_msg": user_msg,
        })

    entries.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)

    if entries:
        for e in entries:
            for tid in e["_task_ids"]:
                if tid not in touched_task_ids:
                    touched_task_ids.append(tid)
            for f in e["_files"]:
                if f not in touched_files:
                    touched_files.append(f)
            if last_user_intent is None and e.get("_user_msg"):
                last_user_intent = e["_user_msg"]

    activity_out = [
        {
            "ts": e.get("ts"),
            "turns": e.get("turns"),
            "tools_used": e.get("tools_used"),
            "summary": e.get("summary"),
        }
        for e in entries[:_MAX_ACTIVITY]
    ]

    session_signals = {
        "touched_task_ids": touched_task_ids,
        "touched_files": touched_files,
        "last_user_intent": last_user_intent,
    }
    return activity_out, session_signals


def _select_next_action(
    open_tasks: list[dict[str, Any]],
    session_signals: dict[str, Any],
    task_entries: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """세션 히스토리와 task 관계를 고려하여 가장 적합한 다음 행동(next_action)을 결정.

    우선순위:
    1. 최근 세션에서 직접 다루었던(touched) 미완료 task (작업 연속성)
    2. 최근 세션에서 다룬 파일과 연결된 미완료 task (자료 기반 연속성)
    3. 기본 폴백: 목록 순서 첫 번째 미완료 task
    """
    if not open_tasks:
        return None

    open_by_id = {t["id"]: t for t in open_tasks}
    touched_task_ids = session_signals.get("touched_task_ids") or []
    touched_files = session_signals.get("touched_files") or []

    # 1. 최근 세션에서 다룬 task_id 매칭
    for tid in touched_task_ids:
        if tid in open_by_id:
            matched_task = open_by_id[tid]
            return {
                "task_id": matched_task["id"],
                "title": matched_task.get("title", ""),
                "reason": matched_task.get("reason", ""),
                "basis": "최근 세션에서 진행 중이던 작업",
                "resources": task_entries.get(matched_task["id"], []),
            }

    # 2. 최근 세션에서 다룬 파일과 연결된 task 매칭
    if touched_files:
        for task in open_tasks:
            entries = task_entries.get(task["id"], [])
            for entry in entries:
                file_info = entry.get("file") or {}
                fname = file_info.get("name") or ""
                fpath = file_info.get("path") or ""
                for tf in touched_files:
                    if tf and (tf == fname or tf == fpath or tf.endswith(fpath) or fpath.endswith(tf)):
                        return {
                            "task_id": task["id"],
                            "title": task.get("title", ""),
                            "reason": task.get("reason", ""),
                            "basis": f"최근 세션 작업 파일({fname or fpath})과 연결된 작업",
                            "resources": entries,
                        }

    # 3. 폴백: 첫 번째 미완료 task
    task = open_tasks[0]
    return {
        "task_id": task["id"],
        "title": task.get("title", ""),
        "reason": task.get("reason", ""),
        "basis": "미완료 task 중 목록 순서 첫 번째",
        "resources": task_entries.get(task["id"], []),
    }


def _linked_entry(
    source: str,
    task_id: str | None,
    item: dict[str, Any],
) -> dict[str, Any]:
    """list_project_resources / list_task_resources 항목 → 브리핑 표기."""
    file = item.get("file") or {}
    return {
        "source": source,
        "task_id": task_id,
        "relation": item.get("relation"),
        "file": {
            "id": file.get("id"),
            "root_id": file.get("root_id"),
            "path": file.get("relative_path"),
            "name": file.get("name"),
            "status": file.get("status", "unknown"),
        },
    }


def build_resume_briefing_tool(
    memory_dir: str | Path | None,
    task_file: str | Path | None = None,
    pages_dir: str | Path | None = None,
    file_roots: str | dict[str, str] | None = None,
    trace_dir: str | Path | None = None,
) -> Tool:
    schema = resume_briefing_schema()

    if memory_dir is None:
        return Tool(
            schema=schema,
            kind="read",
            handler=None,
            unavailable_hint=(
                "memory_dir 미설정. configs/harness.yaml의 tools.memory_dir 또는 "
                "HARNESS_MEMORY_DIR 환경변수로 JARVIS memory 경로를 지정하세요"
            ),
        )

    resolved_memory = Path(memory_dir)
    resolved_trace_dir = Path(trace_dir) if trace_dir else None
    discovery = Discovery(
        resolved_memory,
        task_file=task_file,
        pages_dir=pages_dir,
        file_roots=file_roots,
    )

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        project_id_arg = str(arguments.get("project_id") or "").strip()
        if not query and not project_id_arg:
            raise ValueError("query 또는 project_id 중 하나는 필요합니다")

        reader = MemoryContextReader(resolved_memory)
        projects = reader.list_projects()

        # ---------- 1) 프로젝트 확정 (명시적 id가 항상 추정 검색을 이긴다) ----------
        project: dict[str, Any] | None = None
        resolved_by = ""
        candidates: list[dict[str, Any]] = []
        if project_id_arg:
            for candidate in projects:
                if candidate["id"] == project_id_arg:
                    project = {
                        "id": candidate["id"],
                        "title": candidate.get("title") or candidate["id"],
                    }
                    resolved_by = "project_id"
                    break
            if project is None:
                return {
                    "status": "unresolved",
                    "reason": f"등록된 프로젝트가 아닙니다: {project_id_arg}",
                    "projects": [
                        {"id": p["id"], "title": p.get("title") or p["id"]}
                        for p in projects
                    ],
                }
        else:
            found = discovery.search_projects(query, limit=5)
            matches = found.get("results") or []
            if not matches:
                return {
                    "status": "unresolved",
                    "reason": f"'{query}'와 일치하는 프로젝트가 없습니다",
                    "projects": [
                        {"id": p["id"], "title": p.get("title") or p["id"]}
                        for p in projects
                    ],
                }
            top = matches[0]
            project = {"id": top["id"], "title": top.get("title") or top["id"]}
            resolved_by = f"query:{top.get('matched_on')}"
            candidates = [
                {
                    "id": m["id"],
                    "title": m.get("title") or m["id"],
                    "matched_on": m.get("matched_on"),
                }
                for m in matches[1:4]
            ]

        # ---------- 2) task 상태 (canonical TaskStore 읽기) ----------
        store = TaskStore(
            discovery.task_file_path(), memory_dir=resolved_memory
        )
        tasks = store.list_tasks(project["id"])
        open_tasks = [t for t in tasks if not t.get("done")]
        completed_tasks = [t for t in tasks if t.get("done")]

        # ---------- 3) 관련 자료 (persisted ResourceLink → 현재 locator resolve) ----------
        # M2 — 자료 연결 정확도: workspace 전체가 아니라 명시적으로 연결된 파일만.
        # task별 귀속을 함께 계산해 "그 작업에 실제 관련된 파일"을 구분한다.
        resources: list[dict[str, Any]] = []
        task_entries: dict[str, list[dict[str, Any]]] = {}
        resources_api = discovery.resources
        if resources_api is not None:
            try:
                project_items = resources_api.list_project_resources(
                    project["id"]
                )["resources"]
            except Exception:
                project_items = []
            for item in project_items:
                resources.append(_linked_entry("project", None, item))
            for task in open_tasks[:_MAX_TASK_RESOURCE_LOOKUPS]:
                try:
                    task_items = resources_api.list_task_resources(
                        task["id"]
                    )["resources"]
                except Exception:
                    task_items = []
                collected: list[dict[str, Any]] = []
                for item in task_items:
                    entry = _linked_entry("task", task["id"], item)
                    resources.append(entry)
                    collected.append(entry)
                task_entries[task["id"]] = collected
        resources = resources[:_MAX_RESOURCES]

        # ---------- 4) 마지막 활동 (이 프로젝트 trace만) ----------
        last_activity, session_signals = _recent_activity(
            resolved_trace_dir, project["id"]
        )

        # ---------- 5) 다음 행동 (파생·일시적 — 영속 엔티티 아님, V4 §6) ----------
        # M4 — 세션 히스토리(최근 다룬 task 및 작업 파일)를 활용하여 가장 연결성 높은 작업 추천
        next_action = _select_next_action(
            open_tasks, session_signals, task_entries
        )

        # ---------- 6) notes — 빈 영역은 지어내지 않고 명시 ----------
        notes: list[str] = []
        if candidates:
            notes.append("프로젝트는 검색 일치 기준으로 확정했습니다")
        if not resources:
            notes.append("이 프로젝트에 연결된 파일이 없습니다")
        if not last_activity:
            notes.append("기록된 최근 활동이 없습니다")
        if not open_tasks:
            notes.append("미완료 task가 없습니다")

        primary_workspace = None
        if discovery.project_workspaces is not None:
            primary_workspace = (
                discovery.project_workspaces
                .get_project_primary_workspace(project["id"])
            )

        project_out: dict[str, Any] = {
            **project,
            "resolved_by": resolved_by,
        }
        if primary_workspace is not None:
            project_out["primary_workspace"] = {
                "root_id": primary_workspace.get("root_id"),
                "display_name": primary_workspace.get("display_name"),
                "available": primary_workspace.get("available"),
            }
        if candidates:
            project_out["candidates"] = candidates

        return {
            "status": "ok",
            "project": project_out,
            "tasks": {
                "open": [
                    {
                        "id": t["id"],
                        "title": t.get("title", ""),
                        "reason": t.get("reason", ""),
                        # M2 — 조회한 task에 한해 실제 연결된 파일만 귀속 표기.
                        # 조회 범위 밖 task에는 files 필드를 만들지 않는다(추측 금지).
                        **(
                            {
                                "files": [
                                    entry["file"]
                                    for entry in task_entries.get(t["id"], [])
                                ],
                            }
                            if t["id"] in task_entries
                            else {}
                        ),
                    }
                    for t in open_tasks
                ],
                "completed": [
                    {"id": t["id"], "title": t.get("title", "")}
                    for t in completed_tasks[:_MAX_COMPLETED]
                ],
                "open_count": len(open_tasks),
                "completed_count": len(completed_tasks),
                "total_count": len(tasks),
            },
            "resources": resources,
            "last_activity": last_activity,
            "next_action": next_action,
            "notes": notes,
        }

    return Tool(schema=schema, kind="read", handler=handler)
