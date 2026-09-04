from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

"""local-jarvis memory/*.md 데이터 원천 리더 (Slice 1 get_project_context 백엔드).

참조 앱의 `memory/`(projects.md·profile.md·business_context.md·rules.md·
writing_style.md·chat_handoffs/*.md)는 JARVIS의 문맥 저장소다. 이 리더는
read-only로 파일을 읽어 tool 결과 dict로 변환한다. — 실행·쓰기는 앱 책임(§8.2).
"""


class UnknownProjectError(ValueError):
    """projects.md의 Active 목록에 없는 project_id."""

    def __init__(self, project_id: str, available: list[str]) -> None:
        self.project_id = project_id
        self.available = available
        super().__init__(
            f"알 수 없는 프로젝트: {project_id!r}. "
            f"Active 프로젝트: {available or '(없음)'}"
        )


@dataclass
class ProjectEntry:
    project_id: str
    title: str


def _parse_active_projects(text: str) -> list[ProjectEntry]:
    """`## Active` 섹션의 `- <id>[: <title>]` 줄을 파싱. (미지원: 다른 헤딩 구조)"""
    entries: list[ProjectEntry] = []
    in_active = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## ") or line.startswith("# "):
            in_active = line == "## Active" or line.startswith("## Active")
            continue
        if not in_active or not line.startswith("- "):
            continue
        rest = line[2:].strip()
        if not rest or ":" not in rest:
            # `- local-jarvis` — id만 있는 형태도 허용
            if rest:
                entries.append(ProjectEntry(project_id=rest, title=""))
            continue
        project_id, _, title = rest.partition(":")
        entries.append(ProjectEntry(project_id=project_id.strip(), title=title.strip()))
    return entries


class MemoryContextReader:
    def __init__(self, memory_dir: str | Path) -> None:
        self.memory_dir = Path(memory_dir)

    @property
    def exists(self) -> bool:
        return self.memory_dir.is_dir()

    def projects_md(self) -> Path:
        return self.memory_dir / "projects.md"

    def list_projects(self) -> list[dict[str, str]]:
        """Active 프로젝트 목록 [{"id", "title"}]."""
        if not self.exists:
            raise FileNotFoundError(
                f"memory 디렉터리가 없습니다: {self.memory_dir}"
            )
        if not self.projects_md().exists():
            raise FileNotFoundError(
                f"projects.md가 없습니다: {self.projects_md()} — "
                "JARVIS memory 디렉터리 경로가 맞는지 확인하세요"
            )
        return [
            {"id": entry.project_id, "title": entry.title}
            for entry in _parse_active_projects(
                self.projects_md().read_text(encoding="utf-8")
            )
        ]

    def read_project(self, project_id: str) -> dict[str, Any]:
        """project_id의 문맥 = 관련 memory 파일 모음 dict.

        반환 shape (tool 결과 data):
        {"project_id", "title", "files": [{"name", "kind", "content", "chars"}]}
        """
        projects = self.list_projects()
        match = next((p for p in projects if p["id"] == project_id), None)
        if match is None:
            raise UnknownProjectError(project_id, [p["id"] for p in projects])

        files: list[dict[str, Any]] = []
        for path in sorted(self.memory_dir.glob("*.md")):
            files.append(self._read_file(path, kind="core"))
        handoffs_dir = self.memory_dir / "chat_handoffs"
        if handoffs_dir.is_dir():
            for path in sorted(handoffs_dir.glob("*.md")):
                files.append(self._read_file(path, kind="handoff"))

        return {
            "project_id": match["id"],
            "title": match["title"],
            "files": files,
        }

    @staticmethod
    def _read_file(path: Path, kind: str) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8")
        return {
            "name": path.name,
            "kind": kind,
            "chars": len(content),
            "content": content,
        }
