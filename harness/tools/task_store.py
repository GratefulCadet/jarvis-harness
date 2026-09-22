from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any

from harness.tools.memory_context import MemoryContextReader

"""Slice 4 — `create_task` reference 저장소 (JARVIS memory/tasks.md).

조사 결과 `[사실]`: 참조 앱(local-jarvis)에는 task 추상화·task 저장소가 없다.
앱의 state 모델은 `memory/*.md`(projects.md의 `## Active`가 프로젝트 레지스트리)다.
그러므로 병렬 DB를 만들지 않고, 앱의 기존 markdown 관례(projects.md)를 그대로
따라 **memory/tasks.md** 하나에 프로젝트별 섹션으로 기록한다.

파일 형식:

    # Tasks

    ## local-jarvis

    - [ ] t-<id12>: <title> — <reason>

- id는 (project_id, title, reason)의 결정적 해시 → 같은 제안이 재실행(재시도)돼도
  중복 생성되지 않는다(멱등성).
- 쓰기는 같은 디렉터리의 임시 파일 → `os.replace` 원자 교체로 부분 쓰기를 막는다.
- task_file은 반드시 memory_dir 안에 있어야 한다(§8.4 쓰기 위치 제약).
- JARVIS가 자체 task 저장소를 도입하면 동일 schema·gate로 handler를 교체 등록할 수
  있다(§8.2) — 이 모듈은 그때까지의 reference 구현이다.
"""

_SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENTRY_RE = re.compile(r"^- \[([ x])\] (.*)$")
_MAX_TITLE = 200
_MAX_REASON = 500


class TaskValidationError(ValueError):
    """인자 검증 실패 — tool 결과로 LLM에 되돌려 수정·재시도하게 한다(§8.3-3)."""


def _new_task_id() -> str:
    """Generate content-independent immutable task ID.

    UUID4-derived hex ensures uniqueness without depending on title/reason.
    Collision check is performed by the caller against persisted IDs.
    """
    return f"t-{uuid.uuid4().hex[:12]}"


def _normalize_for_dedup(text: str) -> str:
    """Normalize title/reason for duplicate detection (not identity)."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


# Legacy helper — kept for test/migration tooling only. NOT used at runtime.
def _task_id_legacy(project_id: str, title: str, reason: str) -> str:
    digest = hashlib.sha1(
        f"{project_id}|{title}|{reason}".encode("utf-8")
    ).hexdigest()
    return f"t-{digest[:12]}"


def _format_entry(task_id: str, title: str, reason: str) -> str:
    line = f"- [ ] {task_id}: {title}"
    if reason:
        line += f" — {reason}"
    return line


class TaskStore:
    """memory/tasks.md 기반 task 저장소 (reference 구현, §8.2)."""

    def __init__(
        self,
        task_file: str | Path,
        memory_dir: str | Path,
        resource_links_file: str | Path | None = None,
    ) -> None:
        self.task_file = Path(task_file)
        self.memory_dir = Path(memory_dir)
        self.resource_links_file = (
            Path(resource_links_file)
            if resource_links_file is not None
            else self.memory_dir / "resource_links.json"
        )
        self._reader = MemoryContextReader(self.memory_dir)
        self._check_containment()

    def _check_containment(self) -> None:
        """§8.4 — 쓰기는 의도된 JARVIS state 위치(memory/) 안에서만."""
        task_file = self.task_file.resolve()
        base = self.memory_dir.resolve()
        if task_file != base and base not in task_file.parents:
            raise ValueError(
                f"task_file이 memory_dir 밖입니다: {self.task_file} "
                f"(memory_dir: {self.memory_dir}) — §8.4 쓰기 위치 제약"
            )

    # ---------- 검증 ----------

    def _check_project_id(self, project_id: Any) -> str:
        if not isinstance(project_id, str) or not project_id.strip():
            raise TaskValidationError("project_id는 비어 있을 수 없습니다")
        candidate = project_id.strip()
        if not _SAFE_PROJECT_RE.match(candidate) or ".." in candidate:
            raise TaskValidationError(
                f"project_id 형식이 안전하지 않습니다: {project_id!r} "
                "(영문·숫자·._-만 허용)"
            )
        available = [project["id"] for project in self._reader.list_projects()]
        if candidate not in available:
            raise TaskValidationError(
                f"알 수 없는 프로젝트: {candidate!r}. Active 프로젝트: "
                f"{available or '(없음)'}"
            )
        return candidate

    @staticmethod
    def _check_text(value: Any, name: str, max_len: int, *, required: bool) -> str:
        if value is None:
            if required:
                raise TaskValidationError(f"{name}는 비어 있을 수 없습니다")
            return ""
        if not isinstance(value, str):
            raise TaskValidationError(f"{name}는 문자열이어야 합니다 (받은 값: {value!r})")
        text = value.strip()
        if required and not text:
            raise TaskValidationError(f"{name}는 비어 있을 수 없습니다")
        if any(ch in text for ch in ("\n", "\r")):
            raise TaskValidationError(f"{name}에 줄바꿈을 포함할 수 없습니다")
        if len(text) > max_len:
            raise TaskValidationError(
                f"{name}이 너무 깁니다 (최대 {max_len}자, 현재 {len(text)}자)"
            )
        return text

    # ---------- 생성 ----------

    def validate_create(
        self,
        project_id: Any,
        title: Any,
        reason: Any = None,
    ) -> tuple[str, str, str]:
        """Validate create inputs without IDs, duplicate handling, or file writes."""
        project = self._check_project_id(project_id)
        checked_title = self._check_text(title, "title", _MAX_TITLE, required=True)
        checked_reason = self._check_text(reason, "reason", _MAX_REASON, required=False)
        return project, checked_title, checked_reason

    def create(
        self,
        project_id: str,
        title: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """task 생성. 동일 제안 재실행 시 기존 task를 반환하고 쓰지 않는다(멱등).

        ID는 content와 무관한 불변 ID (UUID 기반)로 생성된다.
        중복 감지는 title+reason 정규화 기반으로 ID 생성과 분리된다.
        """
        project, title, reason = self.validate_create(project_id, title, reason)

        # Duplicate detection: same project + same normalized title + same normalized reason
        if self.task_file.exists():
            text = self.task_file.read_text(encoding="utf-8")
            dup = self._find_duplicate(text, project, title, reason)
            if dup is not None:
                return {
                    "id": dup["id"],
                    "project_id": project,
                    "title": dup["title"],
                    "reason": dup["reason"],
                    "created": False,
                }
            # Generate ID with collision check
            existing_ids = {e["id"] for e in self._parse(text)}
            task_id = _new_task_id()
            while task_id in existing_ids:
                task_id = _new_task_id()
            new_text = self._append_entry(text, project, _format_entry(task_id, title, reason))
        else:
            task_id = _new_task_id()
            new_text = (
                f"# Tasks\n\n## {project}\n\n{_format_entry(task_id, title, reason)}\n"
            )
        self._atomic_write(new_text)
        return {
            "id": task_id,
            "project_id": project,
            "title": title,
            "reason": reason,
            "created": True,
        }

    def list_tasks(self, project_id: str) -> list[dict[str, Any]]:
        """지정 프로젝트의 task 목록 (id·title·reason·done)."""
        project = self._check_project_id(project_id)
        if not self.task_file.exists():
            return []
        return [
            entry for entry in self._parse(self.task_file.read_text(encoding="utf-8"))
            if entry["project_id"] == project
        ]

    def set_done(
        self,
        project_id: str,
        task_id: str,
        done: bool,
    ) -> dict[str, Any]:
        """task 완료/미완료 상태 변경 (canonical writer).

        `- [ ]` ↔ `- [x]` 를 뒤집는다. 같은 상태를 다시 요청하면
        쓰지 않고 그대로 반환한다(멱등). TaskStore가 단일 parser/writer이며
        이 메서드는 `_parse` 결과를 신뢰하고 같은 파일 규칙으로 다시 쓴다.
        """
        project = self._check_project_id(project_id)
        if not isinstance(task_id, str) or not task_id.strip():
            raise TaskValidationError("task_id는 비어 있을 수 없습니다")
        tid = task_id.strip()
        if not isinstance(done, bool):
            raise TaskValidationError("done은 boolean이어야 합니다")
        if not self.task_file.exists():
            raise TaskValidationError("tasks.md가 없습니다")
        text = self.task_file.read_text(encoding="utf-8")
        entries = self._parse(text)
        target = next(
            (e for e in entries if e["project_id"] == project and e["id"] == tid),
            None,
        )
        if target is None:
            raise TaskValidationError(
                f"알 수 없는 task: {tid!r} (project: {project!r})"
            )
        if target["done"] == done:
            return {
                "id": tid,
                "project_id": project,
                "title": target["title"],
                "reason": target["reason"],
                "done": done,
                "updated": False,
            }
        # 라인 단위로 뒤집기 — 원본 줄의 leading 공백을 보존한다
        lines = text.split("\n")
        current: str | None = None
        updated = False
        for idx, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("## "):
                current = stripped[3:].strip()
                continue
            if current != project:
                continue
            m = _ENTRY_RE.match(stripped)
            if not m:
                continue
            rest = m.group(2).strip()
            cand_id, sep, _ = rest.partition(": ")
            if not sep:
                continue
            if cand_id.strip() != tid:
                continue
            leading = raw[: len(raw) - len(raw.lstrip(" \t"))] if raw.strip() else ""
            lines[idx] = f"{leading}- [{'x' if done else ' '}] {rest}"
            updated = True
            break
        if not updated:
            raise TaskValidationError(
                f"task 라인을 찾을 수 없습니다: {tid!r}"
            )
        new_text = "\n".join(lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
        self._atomic_write(new_text)
        return {
            "id": tid,
            "project_id": project,
            "title": target["title"],
            "reason": target["reason"],
            "done": done,
            "updated": True,
        }

    def update_task(
        self,
        project_id: str,
        task_id: str,
        title: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Task title/reason 편집 (canonical writer).

        - project must exist, task must exist in that project
        - Task ID never changes
        - partial update: title only, reason only, or both
        - duplicate protection: reject if resulting title+reason matches another task
        - idempotent: same values → updated:false
        """
        project = self._check_project_id(project_id)
        if not isinstance(task_id, str) or not task_id.strip():
            raise TaskValidationError("task_id는 비어 있을 수 없습니다")
        tid = task_id.strip()

        if title is None and reason is None:
            raise TaskValidationError("title 또는 reason 중 하나 이상 제공해야 합니다")

        if not self.task_file.exists():
            raise TaskValidationError("tasks.md가 없습니다")

        text = self.task_file.read_text(encoding="utf-8")
        target = self._find_entry(text, project, tid)
        if target is None:
            raise TaskValidationError(
                f"알 수 없는 task: {tid!r} (project: {project!r})"
            )

        new_title = target["title"] if title is None else self._check_text(title, "title", _MAX_TITLE, required=True)
        new_reason = target["reason"] if reason is None else self._check_text(reason, "reason", _MAX_REASON, required=False)

        # Idempotency: no change → no write
        if new_title == target["title"] and new_reason == target["reason"]:
            return {
                "id": tid,
                "project_id": project,
                "title": target["title"],
                "reason": target["reason"],
                "done": target["done"],
                "updated": False,
            }

        # Duplicate protection: check other tasks in same project
        for entry in self._parse(text):
            if entry["project_id"] != project or entry["id"] == tid:
                continue
            if (_normalize_for_dedup(entry["title"]) == _normalize_for_dedup(new_title)
                    and _normalize_for_dedup(entry["reason"]) == _normalize_for_dedup(new_reason)):
                raise TaskValidationError(
                    f"동일한 title+reason을 가진 task가 이미 존재합니다: {entry['id']!r}"
                )

        # Line-level replacement — preserve done state and leading whitespace
        lines = text.split("\n")
        current: str | None = None
        replaced = False
        for idx, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("## "):
                current = stripped[3:].strip()
                continue
            if current != project:
                continue
            m = _ENTRY_RE.match(stripped)
            if not m:
                continue
            rest = m.group(2).strip()
            cand_id, sep, _ = rest.partition(": ")
            if not sep:
                continue
            if cand_id.strip() != tid:
                continue
            leading = raw[: len(raw) - len(raw.lstrip(" \t"))] if raw.strip() else ""
            lines[idx] = f"{leading}{_format_entry(tid, new_title, new_reason)}"
            replaced = True
            break
        if not replaced:
            raise TaskValidationError(
                f"task 라인을 찾을 수 없습니다: {tid!r}"
            )

        new_text = "\n".join(lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
        self._atomic_write(new_text)
        return {
            "id": tid,
            "project_id": project,
            "title": new_title,
            "reason": new_reason,
            "done": target["done"],
            "updated": True,
        }

    def delete_task(
        self,
        project_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        """Task 삭제 (canonical writer, V1 hard delete — no trash/tombstone).

        - project must exist, task must exist in that project
        - 삭제는 정확히 1개 라인 — 다른 task·형식은 그대로 보존
        - done state와 무관하게 삭제된다 (open/done 동일 취급)
        - 이미 삭제된(없는) task 재요청은 unknown error (멱등 delete 아님)
        """
        project = self._check_project_id(project_id)
        if not isinstance(task_id, str) or not task_id.strip():
            raise TaskValidationError("task_id는 비어 있을 수 없습니다")
        tid = task_id.strip()

        if not self.task_file.exists():
            raise TaskValidationError("tasks.md가 없습니다")

        text = self.task_file.read_text(encoding="utf-8")
        target = self._find_entry(text, project, tid)
        if target is None:
            raise TaskValidationError(
                f"알 수 없는 task: {tid!r} (project: {project!r})"
            )

        if self.resource_links_file.exists():
            from harness.tools.resource_links import ResourceLinkRegistry

            links = ResourceLinkRegistry(self.resource_links_file)
            if links.links_for_task(tid):
                raise TaskValidationError(
                    f"Task에 연결된 리소스가 있습니다: {tid!r}. 삭제 전에 링크를 해제하세요"
                )

        # Remove exactly one line, preserving everything else
        lines = text.split("\n")
        current: str | None = None
        removed = False
        for idx, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("## "):
                current = stripped[3:].strip()
                continue
            if current != project:
                continue
            m = _ENTRY_RE.match(stripped)
            if not m:
                continue
            rest = m.group(2).strip()
            cand_id, sep, _ = rest.partition(": ")
            if not sep:
                continue
            if cand_id.strip() != tid:
                continue
            del lines[idx]
            removed = True
            break
        if not removed:
            raise TaskValidationError(
                f"task 라인을 찾을 수 없습니다: {tid!r}"
            )

        new_text = "\n".join(lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
        self._atomic_write(new_text)
        return {
            "id": tid,
            "project_id": project,
            "title": target["title"],
            "reason": target["reason"],
            "done": target["done"],
            "deleted": True,
        }

    # ---------- 파일 처리 ----------

    def _atomic_write(self, text: str) -> None:
        self.task_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.task_file.parent / (
            f".{self.task_file.name}.tmp-{uuid.uuid4().hex[:8]}"
        )
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.task_file)

    @staticmethod
    def _parse(text: str) -> list[dict[str, Any]]:
        """섹션별 task 엔트리 파싱. 형식에 맞지 않는 줄은 무시한다(안전)."""
        entries: list[dict[str, Any]] = []
        current: str | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("## "):
                current = line[3:].strip()
                continue
            if current is None:
                continue
            match = _ENTRY_RE.match(line)
            if not match:
                continue
            done = match.group(1) == "x"
            rest = match.group(2).strip()
            # 형식: `- [ ] <id>: <title> — <reason>` — reason은 선택.
            # `<id>: ` 부분이 없으면 형식 불일치로 무시한다(안전).
            task_id, sep_id, title_part = rest.partition(": ")
            if not sep_id:
                continue
            title, sep, reason = title_part.rpartition(" — ")
            if not sep:
                title, reason = title_part, ""
            entries.append({
                "id": task_id.strip(),
                "project_id": current,
                "title": title.strip(),
                "reason": reason.strip() if reason else "",
                "done": done,
            })
        return entries

    def find_task(self, task_id: str) -> dict[str, Any] | None:
        """Find a canonical task by immutable ID across all project sections."""
        if not isinstance(task_id, str) or not task_id.strip() or not self.task_file.exists():
            return None
        tid = task_id.strip()
        return next(
            (
                entry
                for entry in self._parse(self.task_file.read_text(encoding="utf-8"))
                if entry["id"] == tid
            ),
            None,
        )

    def _find_entry(
        self, text: str, project_id: str, task_id: str
    ) -> dict[str, Any] | None:
        return next(
            (
                entry for entry in self._parse(text)
                if entry["project_id"] == project_id and entry["id"] == task_id
            ),
            None,
        )

    @staticmethod
    def _find_duplicate(
        text: str, project_id: str, title: str, reason: str
    ) -> dict[str, Any] | None:
        """Search for existing task with same normalized title + reason in project.

        This is creation policy (idempotency), not identity.
        """
        norm_title = _normalize_for_dedup(title)
        norm_reason = _normalize_for_dedup(reason)
        for entry in TaskStore._parse(text):
            if entry["project_id"] != project_id:
                continue
            if (_normalize_for_dedup(entry["title"]) == norm_title
                    and _normalize_for_dedup(entry["reason"]) == norm_reason):
                return entry
        return None

    @staticmethod
    def _append_entry(text: str, project_id: str, entry: str) -> str:
        """기존 내용을 보존하면서 해당 프로젝트 섹션에 엔트리를 추가."""
        lines = text.split("\n")
        start: int | None = None
        end: int | None = None
        for index, line in enumerate(lines):
            if line.startswith("## "):
                if line[3:].strip() == project_id:
                    start = index
                elif start is not None:
                    end = index
                    break

        if start is None:
            base = text.rstrip("\n")
            return f"{base}\n\n## {project_id}\n\n{entry}\n" if base else f"## {project_id}\n\n{entry}\n"

        if end is None:
            end = len(lines)
        insert_pos = end
        while insert_pos > start + 1 and lines[insert_pos - 1].strip() == "":
            insert_pos -= 1
        rebuilt = lines[:insert_pos] + [entry, ""] + lines[end:]
        joined = "\n".join(rebuilt)
        if not joined.endswith("\n"):
            joined += "\n"
        return joined