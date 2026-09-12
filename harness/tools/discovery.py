from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.tools.file_store import FileStore
# Import lazily inside methods to avoid circular import with workspace_roots.py
from harness.tools.memory_context import MemoryContextReader
from harness.tools.page_store import PageStore, split_frontmatter
from harness.tools.project_workspaces import (
    ProjectWorkspaces,
    default_project_workspaces_file,
)
from harness.tools.resource_links import ProjectResources, default_links_file
from harness.tools.task_store import TaskStore
from harness.tools.workspace import WorkspaceManager, default_registry_file

"""Context Discovery — canonical read-only discovery layer (PART B/C).

§3 데이터 소유 원칙: 이 모듈은 새 저장소를 만들지 않는다. Project/Task는
MemoryContextReader·TaskStore, Page는 PageStore, File은 FileStore의 결과를
읽어 결정적으로 매칭하는 read adapter일 뿐이다. 결과는 항상 canonical
identity(id·path)와 matched content를 분리해 반환한다(PART G).

랭킹(PART B-2, 벡터/DB 없음):
  1. exact project id
  2. exact normalized title
  3. case-insensitive normalized title
  4. title substring
  5. id substring (부분 기억 id)
  6. task 제목/이유 substring → matched_on: task (내 기억으로 프로젝트 재발견)
"""

_MAX_SEARCH_RESULTS = 25
_DEFAULT_SEARCH_LIMIT = 10


def _normalize(text: str) -> str:
    """소문자화 + 공백 정규화 (한글 등 비ASCII는 그대로 둔다)."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _normalize_loose(text: str) -> str:
    """순위용 완화 정규화 — 공백/구두점/밑줄/하이픈 제거 후 소문자."""
    return re.sub(r"[\s\-_.:,()\[\]]+", "", _normalize(text))


class Discovery:
    """Project/Task/Page/File 도메인의 결정적 read-only 검색 계층."""

    def __init__(
        self,
        memory_dir: str | Path | None,
        task_file: str | Path | None = None,
        pages_dir: str | Path | None = None,
        file_roots: str | dict[str, str] | None = None,
    ) -> None:
        self.memory_dir = Path(memory_dir) if memory_dir else None
        self.task_file = Path(task_file) if task_file else None
        self.pages_dir = (
            Path(pages_dir)
            if pages_dir
            else (self.memory_dir / "pages" if self.memory_dir else None)
        )
        # FileStore는 먼저 legacy만으로 초기화한 뒤, memory_dir가 있으면
        # registry + legacy를 병합한 effective roots로 교체한다 (지연 병합).
        from harness.tools.file_store import parse_roots as _parse  # noqa: PLC0415

        if isinstance(file_roots, dict) and file_roots:
            _legacy = dict(file_roots)
        else:
            _legacy = _parse(file_roots)  # type: ignore[assignment]
        self.files = FileStore(_legacy)
        # Registry 병합 — memory_dir가 있으면 persistent registry 우선
        if self.memory_dir is not None:
            try:
                from harness.tools.workspace_roots import resolve_effective_roots  # noqa: PLC0415

                effective = resolve_effective_roots(self.memory_dir, file_roots)
                self.files = FileStore(effective)
            except Exception:
                pass
        # Workspace identity (§7·§11) — FileStore 위의 FileRef 계층. 레지스트리는
        # 파생 상태(<memory_dir>/file_refs.json); 스캔으로 재구성 가능.
        self.workspace = (
            WorkspaceManager(self.files, default_registry_file(self.memory_dir))
            if self.files.roots and self.memory_dir
            else None
        )
        # ResourceLink 읽기 계층(§12·§13) — search_context가 검색 일치와
        # persisted 링크를 구분해 provenance를 실을 수 있게 한다(PART L).
        self.resources = (
            ProjectResources(
                default_links_file(self.memory_dir),
                self.memory_dir,
                workspace=self.workspace,
            )
            if self.workspace is not None
            else None
        )
        # Project Primary Workspace (§4) — project_id → 논리 root_id 관계.
        # 레지스트리는 <memory_dir>/project_workspaces.json. roots가 없어도
        # 관계 읽기는 가능해야 한다(다른 디바이스에서 설정한 관계 → available:false).
        self.project_workspaces = (
            ProjectWorkspaces(
                default_project_workspaces_file(self.memory_dir),
                self.memory_dir,
                roots=dict(self.files.roots),
            )
            if self.memory_dir is not None
            else None
        )

    # ---------- 원천 로더 ----------

    def _reader(self) -> MemoryContextReader:
        if self.memory_dir is None:
            raise ValueError("memory_dir 미설정 — discovery 원천이 없습니다")
        return MemoryContextReader(self.memory_dir)

    def _tasks(self) -> list[dict[str, Any]]:
        """모든 프로젝트의 task를 평탄화 (project_id 포함). read-only."""
        if self.memory_dir is None:
            return []
        task_file = self.task_file_path()
        try:
            store = TaskStore(task_file, memory_dir=self.memory_dir)
        except (ValueError, OSError):
            return []
        tasks: list[dict[str, Any]] = []
        for project in self._reader().list_projects():
            try:
                for task in store.list_tasks(project["id"]):
                    tasks.append({**task, "project_id": project["id"]})
            except (ValueError, OSError):
                continue
        return tasks

    def task_file_path(self) -> Path:
        if self.memory_dir is None:
            raise ValueError("memory_dir 미설정")
        return self.task_file or (self.memory_dir / "tasks.md")

    def list_projects_detailed(self) -> list[dict[str, Any]]:
        """PART B-1 — 모든 프로젝트 + canonical task 수.

        반환: [{id, title, task_count, done_count}] — 실제 존재하는 메타데이터만
        담는다. 요약/상태 같은 필드는 projects.md에 없으므로 만들지 않는다.
        """
        projects = self._reader().list_projects()
        task_file = self.task_file_path()
        try:
            store = TaskStore(task_file, memory_dir=self.memory_dir)
        except (ValueError, OSError):
            store = None
        detailed: list[dict[str, Any]] = []
        for project in projects:
            entry: dict[str, Any] = {
                "id": project["id"],
                "title": project["title"],
                "task_count": 0,
                "done_count": 0,
            }
            if store is not None:
                try:
                    tasks = store.list_tasks(project["id"])
                    entry["task_count"] = len(tasks)
                    entry["done_count"] = sum(1 for task in tasks if task["done"])
                except (ValueError, OSError):
                    pass
            # PART E — primary workspace 메타데이터. 없으면 필드 자체를 만들지
            # 않는다(fabricate 금지). available은 현재 디바이스 기준 판정.
            if self.project_workspaces is not None:
                pw = self.project_workspaces.get_project_primary_workspace(
                    project["id"]
                )
                if pw is not None:
                    entry["primary_workspace"] = {
                        "root_id": pw["root_id"],
                        "display_name": pw["display_name"],
                        "available": pw["available"],
                    }
                    if pw.get("reason"):
                        entry["primary_workspace"]["reason"] = pw["reason"]
            detailed.append(entry)
        return detailed

    # ---------- 프로젝트 검색 ----------

    def search_projects(self, query: str, limit: int = 10) -> dict[str, Any]:
        """PART B — 이름/id/task 내용으로 프로젝트 검색 (결정적 랭킹)."""
        needle = (query or "").strip()
        if not needle:
            raise ValueError("검색어가 비어 있습니다")
        projects = self._reader().list_projects()
        tasks = self._tasks()
        loose = _normalize_loose(needle)
        results: list[dict[str, Any]] = []

        for project in projects:
            project_id = project["id"]
            title = project["title"] or ""
            matched_on = None
            match_detail = ""
            rank = 99
            if project_id == needle:
                matched_on, rank, match_detail = "id", 0, project_id
            elif _normalize_loose(title) == loose and title:
                matched_on, rank, match_detail = "title", 1, title
            elif _normalize(title) == _normalize(needle) and title:
                matched_on, rank, match_detail = "title", 2, title
            elif _normalize(needle) in _normalize(title):
                matched_on, rank, match_detail = "title", 3, title
            elif _normalize(needle) in _normalize(project_id):
                matched_on, rank, match_detail = "id", 4, project_id
            else:
                # task 내용으로 프로젝트 재발견 (PART B-3)
                best_task = None
                for task in tasks:
                    if task["project_id"] != project_id:
                        continue
                    haystack = _normalize(
                        f"{task.get('title', '')} {task.get('reason', '')}"
                    )
                    if _normalize(needle) in haystack:
                        if best_task is None:
                            best_task = task
                if best_task is not None:
                    matched_on = "task"
                    rank = 5
                    match_detail = best_task["title"]

            # PART E — workspace 표시명/경로를 2차 근거로만 사용(명시적 라벨).
            # Project identity는 여전히 id다 — 경로가 identity가 되지 않는다(§7).
            if matched_on is None and self.project_workspaces is not None:
                pw = self.project_workspaces.get_project_primary_workspace(project_id)
                if pw is not None:
                    ws_haystack = _normalize(
                        f"{pw.get('display_name', '')} {pw.get('device_path', '')}"
                    )
                    if _normalize(needle) in ws_haystack:
                        matched_on = "primary_workspace"
                        rank = 6
                        match_detail = pw.get("display_name") or pw.get("device_path", "")
            if matched_on is None:
                continue
            entry: dict[str, Any] = {
                "id": project_id,
                "type": "project",
                "title": title or project_id,
                "matched_on": matched_on,
                "matched_text": match_detail,
                "rank": rank,
            }
            # Include primary_workspace so Qwen can extract root_id for scoped file search
            if self.project_workspaces is not None:
                pw_info = self.project_workspaces.get_project_primary_workspace(project_id)
                if pw_info is not None:
                    entry["primary_workspace"] = {
                        "root_id": pw_info["root_id"],
                        "display_name": pw_info["display_name"],
                        "available": pw_info["available"],
                    }
            results.append(entry)

        results.sort(key=lambda item: (item["rank"], item["id"]))
        max_results = max(1, min(int(limit or _DEFAULT_SEARCH_LIMIT), _MAX_SEARCH_RESULTS))
        return {
            "query": needle,
            "count": len(results[:max_results]),
            "total_matches": len(results),
            "results": results[:max_results],
        }

    # ---------- 통합 검색 ----------

    def search_context(self, query: str, limit: int = 10) -> dict[str, Any]:
        """PART C — Project/Task/Page/File 통합 결정적 검색.

        각 결과는 자기 도메인의 canonical identity + matched_on을 유지한다.
        Page·File은 Project와의 관계가 canonical metadata에 없으므로
        소속을 추론하지 않는다 — 관계가 있을 때만 project_id를 실린다.
        """
        needle = (query or "").strip()
        if not needle:
            raise ValueError("검색어가 비어 있습니다")
        norm = _normalize(needle)
        max_results = max(1, min(int(limit or _DEFAULT_SEARCH_LIMIT), _MAX_SEARCH_RESULTS))

        projects = self._reader().list_projects()
        project_titles = {project["id"]: project["title"] for project in projects}
        tasks = self._tasks()
        results: list[dict[str, Any]] = []

        # Tasks — id/title/reason/소속 프로젝트
        for task in tasks:
            haystack = _normalize(
                f"{task.get('id', '')} {task.get('title', '')} "
                f"{task.get('reason', '')}"
            )
            if norm not in haystack:
                continue
            matched_on = "title" if norm in _normalize(task.get("title", "")) else "reason"
            if norm in _normalize(task.get("id", "")):
                matched_on = "id"
            results.append({
                "type": "task",
                "id": task["id"],
                "title": task.get("title", ""),
                "project_id": task["project_id"],
                "project_title": project_titles.get(task["project_id"], ""),
                "matched_on": matched_on,
            })

        # Projects — id/title (task 매치는 task 항목이 이미 담당)
        for project in projects:
            haystack = _normalize(f"{project['id']} {project.get('title', '')}")
            if norm in haystack:
                matched_on = "id" if norm in _normalize(project["id"]) else "title"
                results.append({
                    "type": "project",
                    "id": project["id"],
                    "title": project.get("title") or project["id"],
                    "matched_on": matched_on,
                })

        # Pages — id/title/path/body (project 소속 추론 금지 — standalone 허용)
        page_results: list[dict[str, Any]] = []
        if self.pages_dir is not None and self.pages_dir.is_dir():
            page_root = self.pages_dir
            store = PageStore(page_root)
            for path in sorted(page_root.rglob("*.md")):
                relative = path.relative_to(page_root)
                rel_posix = relative.as_posix()[: -len(".md")]
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                haystack = _normalize(f"{rel_posix} {text}")
                if norm not in haystack:
                    continue
                # PageStore와 동일한 규칙으로 제목 결정 (frontmatter > 첫 H1 > stem)
                meta, body = split_frontmatter(text)
                stem = rel_posix.rsplit("/", 1)[-1]
                title = meta.get("title") or stem
                for line in body.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        if not meta.get("title"):
                            title = stripped[2:].strip()
                        break
                    if stripped:
                        break
                if norm in _normalize(stem):
                    matched_on = "title"
                elif title and norm in _normalize(title):
                    matched_on = "title"
                else:
                    matched_on = "content"
                page_results.append({
                    "type": "page",
                    "id": None,  # PageStore 규칙 재계산 대신 snapshot에서 보강
                    "path": rel_posix,
                    "title": title,
                    "matched_on": matched_on,
                })
        if page_results:
            # snapshot의 결정적 id(derived 해시)를 path로 보강
            snapshot = store.snapshot()
            ids_by_path = {}

            def _collect(nodes: list[dict[str, Any]]) -> None:
                for node in nodes:
                    ids_by_path[node["path"]] = node["id"]
                    _collect(node.get("children", []))

            _collect(snapshot["pages"])
            for item in page_results:
                item["id"] = ids_by_path.get(item["path"]) or item["path"]
            results.extend(page_results)

        # Files — 이름/경로/본문 (승인 루트가 있을 때만; File != Page).
        # 결과에 stable FileRef identity(id) + locator(root_id·path)를 분리해 실는다.
        if self.files.roots:
            try:
                if self.workspace is not None:
                    self.workspace.ensure_scanned()
                file_matches = self.files.search(needle, limit=max_results)
                for item in file_matches["results"]:
                    entry = {
                        "type": "file",
                        "root": item["root"],
                        "root_id": item["root"],
                        "path": item["path"],
                        "name": item["name"],
                        "matched_on": item["matched_on"],
                        "snippet": item.get("snippet"),
                    }
                    if self.workspace is not None:
                        self.workspace.enrich_entry(entry, item["root"])
                    if entry.get("id") and self.resources is not None:
                        # 검색 일치 ≠ persisted 관계(§12). persisted 링크가 있을 때만
                        # provenance를 단다 — 유사성으로 소유를 주장하지 않는다(PART L).
                        self.resources.registry.reload()
                        links = self.resources.registry.links_for_file(entry["id"])
                        if links:
                            project_links = [
                                {
                                    "project_id": link.from_id,
                                    "relation": link.relation,
                                    "link_id": link.id,
                                    "match_evidence": "persisted",  # 추론 아님
                                }
                                for link in links
                                if link.from_type == "project"
                            ]
                            task_links = [
                                {
                                    "task_id": link.from_id,
                                    "relation": link.relation,
                                    "link_id": link.id,
                                    "match_evidence": "persisted",  # 추론 아님
                                }
                                for link in links
                                if link.from_type == "task"
                            ]
                            if project_links:
                                entry["linked_projects"] = project_links
                            if task_links:
                                entry["linked_tasks"] = task_links
                    results.append(entry)
            except ValueError:
                pass  # 루트 없음/검색어 문제 — file 도메인만 건너뜀

        return {
            "query": needle,
            "count": len(results[:max_results]),
            "total_matches": len(results),
            "results": results[:max_results],
        }
