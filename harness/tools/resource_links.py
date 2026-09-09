"""ResourceLink V1 — Project↔FileRef explicit semantic relationships.

Architecture contract (JARVIS_WORKSPACE_ARCHITECTURE §12·§13) 반영:

- 추론(inference)과 영속(persistence)은 다르다. 검색 유사성은 링크를 만들지
  않는다. 링크는 명시적 사용자 행동 또는 승인된 제안만 영속화한다.
- File identity는 항상 stable FileRef id(`f-*`)다. 경로·파일명·검색 텍스트를
  identity로 받지 않는다(E-9).
- locator(root_id + relative_path)는 읽을 때 FileRef에서 resolve한다 —
  rename/move reconciliation이 성공하면 링크를 건드리지 않고도 최신 위치가
  보인다(G).
- V1 범위: from=project → to=file 만. Task identity가 아직 content-derived라
  Task↔File 링크는 의도적으로 금지(M).

저장소: <memory_dir>/resource_links.json — 파생 JARVIS 메타데이터.
사용자 파일 본문은 복제하지 않는다.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.tools.memory_context import MemoryContextReader

LINKS_VERSION = 1

#: PART C — 제어된 어휘. 링크가 "왜" 연결됐는지를 설명한다. 추가는 신중하게.
RELATIONS: tuple[str, ...] = ("reference", "source", "result", "resource")

#: V1 지원 타입. 저장 스키마는 일반형(from_type/to_type)이라 향후 Task↔File,
#: Page↔File, Conversation↔Project 확장 시 재작성이 필요 없다(B).
SUPPORTED_FROM_TYPES: tuple[str, ...] = ("project",)
SUPPORTED_TO_TYPES: tuple[str, ...] = ("file",)

REF_PREFIX = "f-"  # workspace.FileRef 와 동일한 identity 접두어


class ResourceLinkError(Exception):
    """ResourceLink 계층 오류 — bridge는 message를 그대로 노출한다."""


def new_link_id() -> str:
    """불변 링크 id — 랜덤 uuid. 내용/경로와 무관(§13 일반형)."""
    return f"rl-{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_links_file(memory_dir: Path | str | None) -> Path:
    """레지스트리 위치 관례: <memory_dir>/resource_links.json (PART D)."""
    base = Path(memory_dir) if memory_dir else Path(".")
    return base / "resource_links.json"


def relation_label(relation: str) -> str:
    """SYSTEM MAP relation 하위그룹 라벨."""
    return {
        "reference": "References",
        "source": "Sources",
        "result": "Results",
        "resource": "Resources",
    }.get(relation, relation.title())


@dataclass
class ResourceLink:
    """불변 관계 레코드. id만 영속 identity고 나머지는 관계 의미다."""

    id: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    relation: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_type": self.from_type,
            "from_id": self.from_id,
            "to_type": self.to_type,
            "to_id": self.to_id,
            "relation": self.relation,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ResourceLink":
        return cls(
            id=str(raw["id"]),
            from_type=str(raw["from_type"]),
            from_id=str(raw["from_id"]),
            to_type=str(raw["to_type"]),
            to_id=str(raw["to_id"]),
            relation=str(raw["relation"]),
            created_at=str(raw.get("created_at") or ""),
        )


class ResourceLinkRegistry:
    """resource_links.json 원자적 저장소. FileRegistry와 같은 관례를 따른다."""

    def __init__(self, links_file: str | Path) -> None:
        self.links_file = Path(links_file)
        self.links: dict[str, ResourceLink] = {}
        self._load()

    def _load(self) -> None:
        self.links = {}
        if not self.links_file.exists():
            return  # 읽기 경로에서 파일을 생성하지 않는다 — 링크가 없으면 빈 상태
        try:
            raw = json.loads(self.links_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return  # 손상된 메타데이터는 파생 상태 — 조용히 빈 레지스트리로
        for item in raw.get("links", []):
            try:
                link = ResourceLink.from_dict(item)
            except (KeyError, TypeError):
                continue
            self.links[link.id] = link

    def reload(self) -> None:
        """여러 인스턴스가 같은 파일을 공유하므로 조회/쓰기 전 최신 상태를 읽는다."""
        self._load()

    def save(self) -> None:
        payload = {
            "version": LINKS_VERSION,
            "links": [
                link.to_dict()
                for link in sorted(self.links.values(), key=lambda l: l.id)
            ],
        }
        self.links_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.links_file.parent / f".{self.links_file.name}.tmp-{uuid.uuid4().hex[:8]}"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.links_file)

    # ---------- 조회 ----------

    def all_links(self) -> list[ResourceLink]:
        return sorted(self.links.values(), key=lambda l: l.id)

    def by_id(self, link_id: str) -> ResourceLink | None:
        return self.links.get(link_id)

    def find_link(self, from_type: str, from_id: str, to_type: str, to_id: str, relation: str) -> ResourceLink | None:
        for link in self.links.values():
            if (
                link.from_type == from_type
                and link.from_id == from_id
                and link.to_type == to_type
                and link.to_id == to_id
                and link.relation == relation
            ):
                return link
        return None

    def links_for_project(self, project_id: str) -> list[ResourceLink]:
        return [l for l in self.all_links() if l.from_type == "project" and l.from_id == project_id]

    def links_for_file(self, file_id: str) -> list[ResourceLink]:
        return [l for l in self.all_links() if l.to_type == "file" and l.to_id == file_id]

    def add(self, link: ResourceLink) -> ResourceLink:
        self.links[link.id] = link
        return link

    def remove(self, link_id: str) -> ResourceLink | None:
        return self.links.pop(link_id, None)


class ProjectResources:
    """Project↔File 링크의 validation·생성·조회 facade (PART E·F·G).

    직접 명시적 사용자 행동(deterministic write)과 AI 제안-승인 흐름 모두
    이 단일 writer를 통과한다. 모델 호출은 없다.
    """

    def __init__(
        self,
        links_file: str | Path,
        memory_dir: str | Path | None,
        workspace: Any = None,  # WorkspaceManager | None — registry 접근용
    ) -> None:
        self.links_file = Path(links_file)
        self.memory_dir = Path(memory_dir) if memory_dir else None
        self.registry = ResourceLinkRegistry(links_file)
        self.workspace = workspace

    # ---------- 검증 (PART E) ----------

    def _project_exists(self, project_id: str) -> bool:
        if self.memory_dir is None or not self.memory_dir.is_dir():
            raise ResourceLinkError("memory_dir 미설정 — JARVIS memory 경로가 없습니다")
        reader = MemoryContextReader(self.memory_dir)
        try:
            projects = reader.list_projects()
        except FileNotFoundError as exc:
            raise ResourceLinkError(str(exc)) from exc
        return any(p["id"] == project_id for p in projects)

    def _validate_file_ref(self, file_id: str) -> Any:
        """FileRef identity 검증 — 경로가 들어오면 명시적으로 거부한다(E-9)."""
        if not file_id or not isinstance(file_id, str):
            raise ResourceLinkError("file_id가 비어 있습니다")
        if "/" in file_id or "\\" in file_id or file_id.startswith("."):
            raise ResourceLinkError(
                f"file_id는 FileRef identity(f-*)여야 합니다. 경로는 받지 않습니다: {file_id!r}"
            )
        if not file_id.startswith(REF_PREFIX):
            raise ResourceLinkError(
                f"알 수 없는 FileRef id 형식입니다: {file_id!r} — f-* identity가 필요합니다"
            )
        if self.workspace is None:
            raise ResourceLinkError(
                "승인된 파일 루트가 없어 FileRef를 검증할 수 없습니다 — 루트를 먼저 승인하세요"
            )
        self.workspace.registry.reload()
        ref = self.workspace.registry.by_id(file_id)
        if ref is None:
            raise ResourceLinkError(f"존재하지 않는 FileRef입니다: {file_id}")
        if ref.unresolved:
            raise ResourceLinkError(
                f"FileRef {file_id}는 reconcile이 애매한 상태입니다 — 확인 전에는 링크할 수 없습니다"
            )
        if ref.missing:
            raise ResourceLinkError(
                f"FileRef {file_id}는 마지막 스캔에서 파일을 찾지 못했습니다 — 링크할 수 없습니다"
            )
        return ref

    def _validate_relation(self, relation: str, from_type: str, to_type: str) -> None:
        if relation not in RELATIONS:
            raise ResourceLinkError(
                f"지원하지 않는 relation입니다: {relation!r} — 사용 가능: {', '.join(RELATIONS)}"
            )
        if from_type not in SUPPORTED_FROM_TYPES:
            raise ResourceLinkError(
                f"V1에서는 from_type={from_type!r} 링크를 지원하지 않습니다 — 지원: {', '.join(SUPPORTED_FROM_TYPES)}"
            )
        if to_type not in SUPPORTED_TO_TYPES:
            raise ResourceLinkError(
                f"V1에서는 to_type={to_type!r} 링크를 지원하지 않습니다 — 지원: {', '.join(SUPPORTED_TO_TYPES)}"
            )

    # ---------- 쓰기 (PART F·K-13) ----------

    def link_project_file(
        self,
        project_id: str,
        file_id: str,
        relation: str = "reference",
    ) -> dict[str, Any]:
        """명시적 Project→File 링크 생성. 동일 삼중항은 idempotent(E-중복)."""
        if not project_id or not isinstance(project_id, str):
            raise ResourceLinkError("project_id가 비어 있습니다")
        self._validate_relation(relation, "project", "file")
        if not self._project_exists(project_id):
            raise ResourceLinkError(f"존재하지 않는 Project입니다: {project_id}")
        self._validate_file_ref(file_id)

        self.registry.reload()
        existing = self.registry.find_link("project", project_id, "file", file_id, relation)
        if existing is not None:
            return {"created": False, "link": existing.to_dict()}

        link = ResourceLink(
            id=new_link_id(),
            from_type="project",
            from_id=project_id,
            to_type="file",
            to_id=file_id,
            relation=relation,
            created_at=_now_iso(),
        )
        self.registry.add(link)
        self.registry.save()
        return {"created": True, "link": link.to_dict()}

    def unlink(self, link_id: str) -> dict[str, Any]:
        """JARVIS 메타데이터만 제거(K-13). 사용자 파일은 절대 건드리지 않는다."""
        if not link_id or not isinstance(link_id, str):
            raise ResourceLinkError("link_id가 비어 있습니다")
        self.registry.reload()
        removed = self.registry.remove(link_id)
        if removed is None:
            raise ResourceLinkError(f"존재하지 않는 링크입니다: {link_id}")
        self.registry.save()
        return {"removed": True, "link": removed.to_dict()}

    # ---------- 읽기 (PART G) ----------

    def _resolve_file(self, ref: Any) -> dict[str, Any]:
        """FileRef → 현재 locator. rename/move reconciliation 결과가 그대로 반영된다."""
        out: dict[str, Any] = {
            "id": ref.id,
            "root_id": ref.root_id,
            "relative_path": ref.relative_path,
            "name": ref.name,
            "status": "ok",
        }
        if ref.unresolved:
            out["status"] = "unresolved"
        elif ref.missing:
            out["status"] = "missing"
        return out

    def list_project_resources(self, project_id: str) -> dict[str, Any]:
        """프로젝트 리소스 = persisted links + 읽을 때 resolve한 현재 locator.

        ResourceLink는 FileRef identity만 저장하므로 rename/move 후에도
        링크 재작성 없이 최신 경로가 나온다(G).
        """
        if self.memory_dir is None or not self.memory_dir.is_dir():
            raise ResourceLinkError("memory_dir 미설정 — JARVIS memory 경로가 없습니다")
        if not self._project_exists(project_id):
            raise ResourceLinkError(f"존재하지 않는 Project입니다: {project_id}")

        # locator resolve 전 최신 스캔 — rename/move가 링크 재작성 없이 최신
        # 경로로 반영되려면 레지스트리가 파일시스템과 동기돼 있어야 한다.
        # ensure_scanned는 경로 집합 diff로 저렴하게 변화를 감지한다(§E).
        if self.workspace is not None:
            self.workspace.ensure_scanned()

        self.registry.reload()
        resources: list[dict[str, Any]] = []
        for link in self.registry.links_for_project(project_id):
            entry: dict[str, Any] = {
                "link_id": link.id,
                "relation": link.relation,
                "created_at": link.created_at,
                "file": {"id": link.to_id, "status": "unknown"},
            }
            if self.workspace is not None:
                self.workspace.registry.reload()
                ref = self.workspace.registry.by_id(link.to_id)
                if ref is not None:
                    entry["file"] = self._resolve_file(ref)
            resources.append(entry)

        return {"project_id": project_id, "resources": resources}
