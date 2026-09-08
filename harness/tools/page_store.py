from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

"""Knowledge Markdown page store — read-only (Knowledge slice).

JARVIS knowledge 페이지를 재귀 중첩 구조로 읽어 들이는 store다. 쓰기는 없다 —
이 모듈은 Snapshot만 만들고, 쓰기 경로는 향후 Permission Gate가 붙은 tool로
별도 결정한다(JARVIS_IMPLEMENTATION_CONTEXT §6·§9).

계층 메커니즘 (이번 milestone 선택): frontmatter parent 메타데이터.
- pages/ 디렉터리 아래 모든 *.md를 재귀 스캔한다 (파일시스템 계층은 사용하지
  않는다 — 폴더 이동이 곧 구조 변경이 되어 move/rename에 취약하고, standalone
  페이지 표현이 어색하다).
- frontmatter의 `parent`가 부모를 가리킨다. 값은 다른 페이지의 `id` 또는
  pages 루트 기준 상대 경로(graduation/experiments) 둘 다 허용한다 — 사람이
  읽고 쓰기 쉬우면서(id 고정) 파일 위치로도 연결할 수 있다.
- parent가 없거나 끊긴(dangling) 페이지는 최상위 standalone 페이지가 된다.
  "모든 Page가 어떤 Project에 속해야 한다"는 규칙을 강제하지 않는다(§6).

안정성:
- 페이지 id는 frontmatter `id`(있으면 검증 후 사용) 없으면 파일 상대경로의
  결정적 해시(`p-<sha1 12자>`). 같은 파일은 reload마다 같은 id를 가진다.
- 순환 참조(A→B→A)나 끊긴 부모 때문에 루트에서 도달 못 하는 페이지는
  최상위로 올려 보여서 데이터가 조용히 사라지지 않게 한다.
- 중복/잘못된 id는 conflicts로 보고한다(첫 파일이 id를 유지).

파일 예시:

    ---
    id: knowledge
    title: Knowledge
    ---
    # Knowledge
"""

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """파일 상단의 `---` fenced frontmatter를 (meta, body)로 분리.

    닫는 fence가 없으면 frontmatter가 아닌 것으로 본다(전체를 body로).
    값은 단순 `key: value` 한 줄만 지원한다(리스트·중첩 미지원 — milestone 범위).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1 :])
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key:
            meta[key] = value.strip()
    return {}, text


def derived_page_id(relative_posix: str) -> str:
    """파일 상대경로 → 결정적 페이지 id (frontmatter id가 없을 때)."""
    digest = hashlib.sha1(relative_posix.encode("utf-8")).hexdigest()
    return f"p-{digest[:12]}"


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped:
            break
    return fallback


class PageStore:
    """pages/ 디렉터리의 Markdown 페이지들을 재귀 트리로 읽는 read-only store."""

    def __init__(self, pages_dir: str | Path) -> None:
        self.pages_dir = Path(pages_dir)

    # ---------- 스캔 ----------

    def _scan_files(self) -> list[dict[str, Any]]:
        """pages/**/*.md를 스캔해 플랫 페이지 레코드를 만든다.

        레코드: {id, title, path (posix, 확장자 제외), parent_raw, meta_ok}
        """
        records: list[dict[str, Any]] = []
        if not self.pages_dir.is_dir():
            return records

        for path in sorted(self.pages_dir.rglob("*.md")):
            relative = path.relative_to(self.pages_dir)
            relative_posix = relative.as_posix()[: -len(".md")]
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue  # 읽을 수 없는 파일이 전체 스냅샷을 막지 않는다

            meta, body = split_frontmatter(text)
            # 확장자를 뗀 상대경로(예: sub/lpips-results)를 폴백 제목으로 쓴다
            stem = relative_posix.rsplit("/", 1)[-1]
            title = meta.get("title") or _title_from_body(body, stem)

            explicit_id = (meta.get("id") or "").strip()
            page_id = derived_page_id(relative_posix)
            if explicit_id:
                if _SAFE_ID_RE.match(explicit_id):
                    page_id = explicit_id
                # 잘못된 형식의 id는 무시하고 파생 id를 쓴다(conflicts로 보고)

            records.append({
                "id": page_id,
                "title": title,
                "path": relative_posix,
                "parent_raw": (meta.get("parent") or "").strip(),
                "explicit_id": bool(explicit_id and _SAFE_ID_RE.match(explicit_id)),
            })
        return records

    # ---------- 스냅샷 ----------

    def snapshot(self) -> dict[str, Any]:
        """재귀 페이지 트리 스냅샷.

        반환 shape:
        {
          "pages": [루트 페이지 노드...],   # 노드: {id, type, title, path,
          "count": 전체 페이지 수,          #      parent_id, children, (detached)}
          "conflicts": [문자열 설명...],
        }
        """
        records = self._scan_files()
        conflicts: list[str] = []

        seen_ids: dict[str, str] = {}
        for record in records:
            first_path = seen_ids.get(record["id"])
            if first_path is None:
                seen_ids[record["id"]] = record["path"]
                continue
            if record["explicit_id"]:
                conflicts.append(
                    f"중복 id {record['id']!r}: {first_path}와 "
                    f"{record['path']} — {record['path']}는 파생 id를 사용"
                )
                record["id"] = derived_page_id(record["path"])
                record["explicit_id"] = False
                seen_ids.setdefault(record["id"], record["path"])
            else:
                # 파생 id 충돌은 사실상 불가능(경로 해시) — 안전망으로만 보고
                conflicts.append(
                    f"파생 id 충돌 {record['id']!r}: {first_path}와 {record['path']}"
                )

        by_id = {record["id"]: record for record in records}
        by_path = {record["path"]: record for record in records}

        # 부모 해석: id 우선, 그다음 상대경로. 못 찾으면 None(최상위).
        parent_of: dict[str, str | None] = {}
        for record in records:
            raw = record["parent_raw"]
            parent: dict[str, Any] | None = None
            if raw:
                parent = by_id.get(raw) or by_path.get(raw)
            parent_of[record["id"]] = parent["id"] if parent else None

        # 자식 모아 만들기 (경로 순 정렬로 결정적 순서)
        children_map: dict[str | None, list[dict[str, Any]]] = {}
        for record in records:
            children_map.setdefault(parent_of[record["id"]], []).append(record)
        for bucket in children_map.values():
            bucket.sort(key=lambda item: item["path"])

        def build_node(record: dict[str, Any], shallow: bool = False) -> dict[str, Any]:
            return {
                "id": record["id"],
                "type": "page",
                "title": record["title"],
                "path": record["path"],
                "parent_id": parent_of[record["id"]],
                "children": [] if shallow else [
                    build_node(child)
                    for child in children_map.get(record["id"], [])
                ],
            }

        roots = [build_node(record) for record in children_map.get(None, [])]

        # 순환 참조 등으로 루트에서 도달 못 한 페이지는 최상위로 올린다
        reachable: set[str] = set()

        def collect(node: dict[str, Any]) -> None:
            if node["id"] in reachable:
                return
            reachable.add(node["id"])
            for child in node["children"]:
                collect(child)

        for root in roots:
            collect(root)

        for record in records:
            if record["id"] not in reachable:
                # 순환 참조면 build_node가 무한 재귀하므로 자식 없이 얕게 만든다
                node = build_node(record, shallow=True)
                node["detached"] = True
                roots.append(node)
                conflicts.append(
                    f"순환/고립 페이지 {record['path']!r} — 최상위로 노출"
                )

        roots.sort(key=lambda node: node["path"])
        return {
            "pages": roots,
            "count": len(records),
            "conflicts": conflicts,
        }
