from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

"""Knowledge Markdown PageStore — read-only content plus derived stable identity.

Markdown remains the canonical Page content.  ``page_identity.json`` is only a
small, rebuildable identity registry used to keep legacy pages stable across a
rename/move; it is not a second copy of the page or its hierarchy.

Hierarchy is semantic frontmatter ``parent`` metadata.  Physical file paths are
locators only and do not determine the persisted identity after reconciliation.
"""

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
REGISTRY_VERSION = 1


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """파일 상단의 `---` fenced frontmatter를 (meta, body)로 분리."""
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
    """Legacy fallback ID for a first-seen page.

    This remains the initial ID for compatibility, but PageStore persists the
    resulting mapping so later path changes do not recompute a new identity.
    """
    digest = hashlib.sha1(relative_posix.encode("utf-8")).hexdigest()
    return f"p-{digest[:12]}"


def new_page_id() -> str:
    """Generate an identity for a page first seen after the registry exists."""
    return f"p-{uuid.uuid4().hex[:12]}"


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped:
            break
    return fallback


def _content_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class PageIdentityRegistry:
    """Small derived mapping from stable Page ID to its current locator.

    A unique content fingerprint is used only to reconcile a path change during
    the same scan.  Once a record has already been marked missing, it is not
    silently rebound on a later scan.
    """

    def __init__(self, registry_file: str | Path) -> None:
        self.registry_file = Path(registry_file)
        self.records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        self.records = {}
        if not self.registry_file.exists():
            return
        try:
            raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or raw.get("version") != REGISTRY_VERSION:
            return
        for item in raw.get("pages", []):
            if not isinstance(item, dict):
                continue
            page_id = item.get("id")
            path = item.get("path")
            if isinstance(page_id, str) and isinstance(path, str):
                self.records[page_id] = {
                    "id": page_id,
                    "path": path,
                    "fingerprint": str(item.get("fingerprint") or ""),
                    "missing": bool(item.get("missing", False)),
                }

    def save(self) -> None:
        payload = {
            "version": REGISTRY_VERSION,
            "pages": [self.records[key] for key in sorted(self.records)],
        }
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.registry_file.parent / (
            f".{self.registry_file.name}.tmp-{uuid.uuid4().hex[:8]}"
        )
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(temp, self.registry_file)

    def assign(
        self,
        records: list[dict[str, Any]],
        conflicts: list[str],
    ) -> None:
        """Assign IDs, preferring explicit IDs, then path, then unique move evidence."""
        by_path = {
            item["path"]: item
            for item in self.records.values()
            if not item.get("missing")
        }
        by_fingerprint: dict[str, list[dict[str, Any]]] = {}
        for item in self.records.values():
            fingerprint = item.get("fingerprint") or ""
            if fingerprint and not item.get("missing"):
                by_fingerprint.setdefault(fingerprint, []).append(item)

        used: set[str] = set()
        matched_registry: set[str] = set()
        claimed_explicit: set[str] = set()
        had_registry = bool(self.records)

        for record in records:
            requested = record.get("requested_id")
            duplicate_explicit = bool(
                requested and sum(
                    1 for other in records if other.get("requested_id") == requested
                ) > 1
            )
            if duplicate_explicit and requested:
                if requested in claimed_explicit:
                    conflicts.append(
                        f"중복 id {requested!r}: {record['path']} — 안전을 위해 폴백 identity를 사용"
                    )
                    requested = None
                else:
                    claimed_explicit.add(requested)
                    conflicts.append(
                        f"중복 id {requested!r}: 여러 페이지가 사용 — 첫 페이지를 유지하고 나머지는 폴백 identity를 사용"
                    )

            old = by_path.get(record["path"])
            if requested and requested not in used:
                page_id = requested
            elif old and old["id"] not in used:
                page_id = old["id"]
            else:
                candidates = [
                    item for item in by_fingerprint.get(record["fingerprint"], [])
                    if item["id"] not in matched_registry and item["id"] not in used
                ]
                if len(candidates) == 1:
                    old = candidates[0]
                    page_id = old["id"]
                else:
                    if len(candidates) > 1:
                        conflicts.append(
                            f"Page 이동 identity가 애매합니다: {record['path']}"
                        )
                    page_id = (
                        derived_page_id(record["path"])
                        if not had_registry
                        else new_page_id()
                    )
                    while page_id in used or page_id in self.records:
                        page_id = new_page_id()

            used.add(page_id)
            if old is not None:
                matched_registry.add(old["id"])
            record["id"] = page_id
            record["explicit_id"] = bool(requested)
            self.records[page_id] = {
                "id": page_id,
                "path": record["path"],
                "fingerprint": record["fingerprint"],
                "missing": False,
            }

        for page_id, item in self.records.items():
            if page_id not in used:
                item["missing"] = True

        self.save()


class PageStore:
    """pages/ 디렉터리의 Markdown 페이지들을 재귀 트리로 읽는 read-only store."""

    def __init__(
        self,
        pages_dir: str | Path,
        identity_file: str | Path | None = None,
    ) -> None:
        self.pages_dir = Path(pages_dir)
        self.identity_file = Path(identity_file) if identity_file else self.pages_dir.parent / "page_identity.json"

    def _scan_files(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.pages_dir.is_dir():
            return records

        for path in sorted(self.pages_dir.rglob("*.md")):
            relative = path.relative_to(self.pages_dir)
            relative_posix = relative.as_posix()[: -len(".md")]
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue

            meta, body = split_frontmatter(text)
            stem = relative_posix.rsplit("/", 1)[-1]
            title = meta.get("title") or _title_from_body(body, stem)
            explicit_id = (meta.get("id") or "").strip()
            valid_explicit = explicit_id if _SAFE_ID_RE.match(explicit_id) else None
            if explicit_id and valid_explicit is None:
                # Preserve the existing safety behavior: malformed IDs are not identities.
                explicit_id = ""
            records.append({
                "id": None,
                "title": title,
                "path": relative_posix,
                "parent_raw": (meta.get("parent") or "").strip(),
                "requested_id": valid_explicit,
                "explicit_id": bool(valid_explicit),
                "fingerprint": _content_fingerprint(text),
            })
        return records

    def snapshot(self) -> dict[str, Any]:
        """재귀 페이지 트리 스냅샷.

        Page content stays in Markdown; only stable identity-to-locator metadata
        is persisted in the derived registry.
        """
        records = self._scan_files()
        if not self.pages_dir.is_dir():
            return {"pages": [], "count": 0, "conflicts": []}

        conflicts: list[str] = []
        registry = PageIdentityRegistry(self.identity_file)
        registry.assign(records, conflicts)

        seen_ids: dict[str, str] = {}
        for record in records:
            first_path = seen_ids.get(record["id"])
            if first_path is None:
                seen_ids[record["id"]] = record["path"]
                continue
            conflicts.append(
                f"Page identity 충돌 {record['id']!r}: {first_path}와 {record['path']}"
            )
            record["id"] = derived_page_id(record["path"])

        by_id = {record["id"]: record for record in records}
        by_path = {record["path"]: record for record in records}

        parent_of: dict[str, str | None] = {}
        for record in records:
            raw = record["parent_raw"]
            parent: dict[str, Any] | None = None
            if raw:
                parent = by_id.get(raw) or by_path.get(raw)
            parent_of[record["id"]] = parent["id"] if parent else None

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
