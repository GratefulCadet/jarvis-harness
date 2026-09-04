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


def _task_id(project_id: str, title: str, reason: str) -> str:
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

    def __init__(self, task_file: str | Path, memory_dir: str | Path) -> None:
        self.task_file = Path(task_file)
        self.memory_dir = Path(memory_dir)
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

    def create(
        self,
        project_id: str,
        title: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """task 생성. 동일 제안 재실행 시 기존 task를 반환하고 쓰지 않는다(멱등)."""
        project = self._check_project_id(project_id)
        title = self._check_text(title, "title", _MAX_TITLE, required=True)
        reason = self._check_text(reason, "reason", _MAX_REASON, required=False)
        task_id = _task_id(project, title, reason)
        entry = _format_entry(task_id, title, reason)

        if self.task_file.exists():
            text = self.task_file.read_text(encoding="utf-8")
            existing = self._find_entry(text, project, task_id)
            if existing is not None:
                return {
                    "id": task_id,
                    "project_id": project,
                    "title": existing["title"],
                    "reason": existing["reason"],
                    "created": False,
                }
            new_text = self._append_entry(text, project, entry)
        else:
            new_text = (
                f"# Tasks\n\n## {project}\n\n{entry}\n"
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
            task_id, _, title_part = rest.partition(": ")
            title, _, reason = title_part.rpartition(" — ")
            entries.append({
                "id": task_id.strip(),
                "project_id": current,
                "title": (title if reason else title_part).strip(),
                "reason": reason.strip() if reason else "",
                "done": done,
            })
        return entries

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