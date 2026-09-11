from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.tools.file_store import parse_roots


class WorkspaceRootError(ValueError):
    pass


REGISTRY_VERSION = 1


def default_workspace_roots_file(memory_dir: Path | str | None) -> Path:
    if memory_dir is None:
        raise WorkspaceRootError("memory_dir 미설정 — JARVIS memory 경로가 없습니다")
    return Path(memory_dir) / "workspace_roots.json"


def resolve_effective_roots(
    memory_dir: Path | str | None,
    legacy_file_roots: str | dict[str, str] | None,
) -> dict[str, Path]:
    """Merge persistent registry + legacy env/config roots into effective FileStore roots.

    Precedence (highest wins):
    1. Persistent registry (<memory_dir>/workspace_roots.json) — user-managed via picker.
       Only available paths (is_dir) are included as FileStore entries.
    2. Legacy roots (JARVIS_FILE_ROOTS env + configs/harness.yaml tools.file_roots)
       — transitional developer config. Only added if neither the logical id nor
       the canonical physical path duplicates a registry entry.

    Registry wins for same root_id or same canonical path to avoid duplication.
    Unavailable registry roots (path missing) are not added as FileStore entries
    but their logical identity is preserved for ProjectWorkspace availability reporting.
    """
    registry_dict: dict[str, Path] = {}
    if memory_dir is not None:
        try:
            md = Path(memory_dir)
            if md.is_dir():
                wr = WorkspaceRoots(default_workspace_roots_file(md), memory_dir=md)
                registry_dict = wr.as_roots_dict()
        except Exception:
            registry_dict = {}
    # Parse legacy roots (string or dict)
    legacy_dict: dict[str, Path] = {}
    if legacy_file_roots is not None:
        try:
            if isinstance(legacy_file_roots, dict):
                legacy_dict = parse_roots(legacy_file_roots)
            else:
                legacy_dict = parse_roots(str(legacy_file_roots))
        except Exception:
            legacy_dict = {}
    # Dedup legacy against registry (platform-aware case comparison)
    effective: dict[str, Path] = dict(registry_dict)
    registry_resolved: list[Path] = []
    for p in registry_dict.values():
        try:
            registry_resolved.append(p.resolve())
        except OSError:
            registry_resolved.append(p)
    for rid, path in legacy_dict.items():
        if rid in effective:
            continue
        try:
            canon = path.resolve()
        except OSError:
            canon = path
        if any(_same_physical_path(canon, rp) for rp in registry_resolved):
            continue
        effective[rid] = path
    return effective


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slugify(basename: str) -> str:
    """basename → safe root_id: lower, spaces→-, keep alnum/-,_ only."""
    raw = basename.strip().lower()
    # Replace whitespace and path separators with -
    raw = re.sub(r"[\s/\\]+", "-", raw)
    # Keep only alphanumeric, -, _
    slug = re.sub(r"[^a-z0-9_-]", "", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")
    if not slug:
        slug = "workspace"
    # Must match parse_roots name pattern ^[A-Za-z0-9_-]+$
    if not re.match(r"^[a-z0-9_-]+$", slug):
        slug = "workspace"
    return slug


def _canonicalize_path(raw: str | Path) -> Path:
    p = Path(str(raw)).expanduser().resolve()
    if not p.exists():
        raise WorkspaceRootError(f"존재하지 않는 경로입니다: {raw!r}")
    if not p.is_dir():
        raise WorkspaceRootError(f"디렉터리가 아닙니다: {raw!r}")
    return p


def _is_path_like_id(text: str) -> bool:
    """Detect path-shaped strings that must not be accepted as root_id."""
    if "/" in text or "\\" in text:
        return True
    if len(text) >= 2 and text[1] == ":":
        return True
    if text.startswith("."):
        return True
    return False


def _same_physical_path(a: Path, b: Path) -> bool:
    """Platform-aware physical path equality for dedup.

    Windows: case-insensitive (D:\\Thesis == d:\\thesis).
    POSIX: case-sensitive.
    Stored/displayed path casing is never altered.
    """
    try:
        ra, rb = a.resolve(), b.resolve()
    except OSError:
        return str(a) == str(b)
    if os.name == "nt":
        return str(ra).lower() == str(rb).lower()
    return ra == rb


class WorkspaceRoots:
    """Persistent WorkspaceRoot registry — <memory_dir>/workspace_roots.json.

    Single writer for logical WorkspaceRoot lifecycle. Physical filesystem
    remains canonical for file contents — this registry only tracks approved
    logical roots and their current-device physical mapping.

    Merge with legacy env/config roots happens in the resolver, not here —
    this class only manages its own JSON file.
    """

    def __init__(
        self,
        registry_file: str | Path,
        memory_dir: str | Path | None = None,
    ) -> None:
        self.registry_file = Path(registry_file)
        self.memory_dir = Path(memory_dir) if memory_dir else None
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        try:
            raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._entries = {}
            return
        except (OSError, json.JSONDecodeError):
            self._entries = {}
            return
        roots = raw.get("roots")
        if not isinstance(roots, dict):
            self._entries = {}
            return
        # Validate entries lightly — skip malformed
        valid: dict[str, dict[str, Any]] = {}
        for rid, entry in roots.items():
            if not isinstance(entry, dict):
                continue
            if not isinstance(rid, str) or not rid.strip():
                continue
            dev = entry.get("device_path")
            if not isinstance(dev, str) or not dev.strip():
                continue
            # Must have id matching key
            if entry.get("id") and str(entry["id"]) != rid:
                continue
            valid[rid] = dict(entry)
        self._entries = valid

    def reload(self) -> None:
        self._load()

    def save(self) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": REGISTRY_VERSION, "roots": self._entries}
        # Deterministic ordering
        payload["roots"] = dict(sorted(self._entries.items()))
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.registry_file.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_name, self.registry_file)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ---------- helpers ----------

    def _unique_root_id(self, base_slug: str, existing: set[str]) -> str:
        if base_slug not in existing:
            return base_slug
        suffix = 2
        while f"{base_slug}-{suffix}" in existing:
            suffix += 1
        return f"{base_slug}-{suffix}"

    def _find_by_device_path(self, canonical: Path) -> str | None:
        """Return root_id whose device_path resolves to same canonical path, if any."""
        for rid, entry in self._entries.items():
            try:
                existing = Path(entry["device_path"]).expanduser().resolve()
            except OSError:
                continue
            if _same_physical_path(existing, canonical):
                return rid
        return None

    def _roots_dict(self) -> dict[str, dict[str, Any]]:
        return dict(self._entries)

    # ---------- read ----------

    def list_roots(self) -> list[dict[str, Any]]:
        """Return all registered roots sorted by id, with availability flag."""
        self.reload()
        result: list[dict[str, Any]] = []
        for rid in sorted(self._entries):
            entry = dict(self._entries[rid])
            dev = entry.get("device_path", "")
            try:
                available = Path(dev).expanduser().is_dir() if dev else False
            except OSError:
                available = False
            entry["available"] = available
            if not available:
                entry["reason"] = "path_unavailable"
            result.append(entry)
        return result

    def get_root(self, root_id: str) -> dict[str, Any] | None:
        self.reload()
        entry = self._entries.get(root_id)
        if entry is None:
            return None
        out = dict(entry)
        try:
            available = Path(out["device_path"]).expanduser().is_dir()
        except OSError:
            available = False
        out["available"] = available
        if not available:
            out["reason"] = "path_unavailable"
        return out

    def as_roots_dict(self) -> dict[str, Path]:
        """Return {root_id: Path} for FileStore construction (only available paths)."""
        self.reload()
        result: dict[str, Path] = {}
        for rid, entry in self._entries.items():
            try:
                p = Path(entry["device_path"]).expanduser().resolve()
            except OSError:
                continue
            if p.is_dir():
                result[rid] = p
        return result

    def all_entries_raw(self) -> dict[str, dict[str, Any]]:
        """Raw entries including unavailable — for merge logic."""
        self.reload()
        return dict(self._entries)

    # ---------- write ----------

    def register(
        self,
        device_path: str | Path,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """Register or reuse a WorkspaceRoot for device_path.

        - Validates path exists and is directory
        - Canonicalizes path
        - Duplicate physical path → reuse existing root_id (idempotent)
        - Generates safe unique root_id from folder basename
        - Persists atomically
        """
        canonical = _canonicalize_path(device_path)
        self.reload()

        # Duplicate physical path → reuse
        existing_id = self._find_by_device_path(canonical)
        if existing_id is not None:
            return {"created": False, "root": dict(self._entries[existing_id])}

        basename = canonical.name or str(canonical).replace("\\", "/").rstrip("/").split("/")[-1]
        base_slug = _slugify(basename)
        root_id = self._unique_root_id(base_slug, set(self._entries.keys()))

        entry: dict[str, Any] = {
            "id": root_id,
            "display_name": (display_name.strip() if isinstance(display_name, str) and display_name.strip() else basename),
            "device_path": str(canonical),
            "access_mode": "read",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        self._entries[root_id] = entry
        self.save()
        return {"created": True, "root": dict(entry)}

    def update(
        self,
        root_id: str,
        display_name: str | None = None,
        device_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Update display_name and/or device_path for existing root_id.

        - root_id must exist
        - display_name change never affects root_id
        - device_path change preserves root_id (reconnect)
        - validates new device_path if provided
        """
        if not root_id or not isinstance(root_id, str):
            raise WorkspaceRootError("root_id가 비어 있습니다")
        if _is_path_like_id(root_id):
            raise WorkspaceRootError(f"root_id는 논리 identity여야 합니다: {root_id!r}")
        self.reload()
        entry = self._entries.get(root_id)
        if entry is None:
            raise WorkspaceRootError(f"존재하지 않는 WorkspaceRoot입니다: {root_id!r}")

        changed = False
        if display_name is not None:
            if not isinstance(display_name, str):
                raise WorkspaceRootError("display_name은 문자열이어야 합니다")
            trimmed = display_name.strip()
            if trimmed and trimmed != entry.get("display_name"):
                entry["display_name"] = trimmed
                changed = True

        if device_path is not None:
            canonical = _canonicalize_path(device_path)
            # Check duplicate with another root (not self)
            dup = self._find_by_device_path(canonical)
            if dup is not None and dup != root_id:
                raise WorkspaceRootError(
                    f"이미 다른 WorkspaceRoot({dup!r})가 같은 경로를 사용 중입니다: {canonical}"
                )
            new_str = str(canonical)
            if new_str != entry.get("device_path"):
                entry["device_path"] = new_str
                changed = True

        if changed:
            entry["updated_at"] = _now_iso()
            self.save()

        return {"root": dict(entry), "updated": changed}

    def remove(self, root_id: str) -> dict[str, Any]:
        """Remove a WorkspaceRoot registration. Never touches filesystem.

        If dependencies exist (ProjectWorkspaces or ResourceLinks reference this
        root), refuse with dependency info instead of cascade-deleting.
        """
        if not root_id or not isinstance(root_id, str):
            raise WorkspaceRootError("root_id가 비어 있습니다")
        self.reload()
        entry = self._entries.get(root_id)
        if entry is None:
            raise WorkspaceRootError(f"존재하지 않는 WorkspaceRoot입니다: {root_id!r}")

        # Check dependencies via existing domain APIs — no raw JSON parsing.
        dependencies: list[str] = []
        if self.memory_dir is not None and self.memory_dir.is_dir():
            # 1. ProjectWorkspaces — any project using this root as primary
            try:
                from harness.tools.project_workspaces import ProjectWorkspaces, default_project_workspaces_file

                pw = ProjectWorkspaces(default_project_workspaces_file(self.memory_dir), self.memory_dir, roots={})
                pw.reload()
                for pid, mapping in pw._entries.items():
                    if mapping.get("root_id") == root_id:
                        dependencies.append(f"project:{pid} primary workspace")
            except ImportError:
                pass

            # 2. FileRefRegistry — active file references under this root
            file_ids_in_root: set[str] = set()
            try:
                from harness.tools.workspace import FileRefRegistry, default_registry_file

                reg = FileRefRegistry(default_registry_file(self.memory_dir))
                reg.reload()
                for ref in reg.refs.values():
                    if ref.root_id == root_id and not ref.missing:
                        file_ids_in_root.add(ref.id)
                        dependencies.append(f"file:{ref.id} ({ref.relative_path})")
                        if len(dependencies) > 5:
                            dependencies.append("... and more")
                            break
            except ImportError:
                pass

            # 3. ResourceLinks — any link whose to_id references a file in this root
            if file_ids_in_root:
                try:
                    from harness.tools.resource_links import ResourceLinkRegistry, default_links_file

                    lr = ResourceLinkRegistry(default_links_file(self.memory_dir))
                    lr.reload()
                    for link in lr.links.values():
                        if link.to_id in file_ids_in_root:
                            dependencies.append(f"resource_link:{link.id} (project:{link.from_id})")
                            if len(dependencies) > 8:
                                dependencies.append("... and more")
                                break
                except ImportError:
                    pass

        if dependencies:
            raise WorkspaceRootError(
                f"WorkspaceRoot {root_id!r}는 다음에 의해 참조되고 있어 제거할 수 없습니다: "
                + "; ".join(dependencies)
                + " — 먼저 연결을 해제하세요"
            )

        removed = self._entries.pop(root_id)
        self.save()
        return {"removed": True, "root": dict(removed)}
