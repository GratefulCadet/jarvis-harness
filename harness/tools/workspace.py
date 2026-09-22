from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.tools.file_store import (
    DEFAULT_MAX_FILE_SIZE,
    TEXT_EXTENSIONS,
    FileStoreError,
    _is_sensitive_name,
)

"""WorkspaceRoot + FileRef identity V1 (WORKSPACE ARCHITECTURE §7·§11).

결정(architecture contract 반영):
- root_id는 논리적이고 안정적이다. device_path는 이 장비의 로컬 설정일 뿐이며
  persistent identity에 절대 들어가지 않는다(§7 — 멀티디바이스).
- FileRef.id는 경로·파일명·내용 해시로부터 파생되지 않는다(§11 — File identity
  ≠ path). 랜덤 uuid + 짧은 접두어. 레지스트리가 유일한 매핑 소유자다.
- 레지스트리는 파생/보조 메타데이터다(§3·§16). 유일한 파일 내용 사본을 담지
  않고, 언제든 실제 파일시스템 스캔으로 재구성(rebuild)할 수 있어야 한다.
- rename/move 조정은 보수적이다: 명확한 후보 1개일 때만 ID를 유지하고, 애매하면
  조용히 묶지 않고 unresolved로 보고한다(§11 "ambiguous matches should not be
  silently accepted").

파일시스템은 언제나 canonical이다 — 레지스트리는 identity 안정성만 책임진다.
"""

REF_PREFIX = "f-"
FINGERPRINT_BYTES = 64 * 1024  # 본문 지문은 앞 64KB만 읽는다 (bounded safe read)
FINGERPRINT_MAX_FILE = 4 * 1024 * 1024  # 4MB 초과 파일은 지문 없이 크기/mtime만
MAX_EDIT_BYTES = 1024 * 1024
REGISTRY_VERSION = 1


def new_file_ref_id() -> str:
    """불변 FileRef id — 랜덤 uuid, 경로/이름/내용과 무관 (§11)."""
    return f"{REF_PREFIX}{uuid.uuid4().hex[:12]}"


def content_fingerprint(path: Path) -> str | None:
    """경계 있는 로컬 지문 (PART F 정책).

    - 앞 64KB만 sha256 (FINGERPRINT_BYTES)
    - 4MB 초과 파일은 지문하지 않는다 (None — 크기/mtime으로만 조정)
    - 텍스트/바이너리 구분 없이 바이트 단위 (내용을 메모리에 통째로 두지 않음)
    - 절대 파일 내용을 저장하지 않는다 — 해시 16 hex만.
    지문은 증거일 뿐 identity가 아니다(§11 "fingerprint is evidence").
    """
    try:
        if path.stat().st_size > FINGERPRINT_MAX_FILE:
            return None
        with path.open("rb") as handle:
            digest = hashlib.sha256(handle.read(FINGERPRINT_BYTES)).hexdigest()
        return digest[:16]
    except OSError:
        return None


@dataclass
class WorkspaceRoot:
    """논리적 승인 루트 (§7). id는 경로와 무관하게 안정적."""

    id: str
    device_path: Path
    display_name: str = ""
    access_mode: str = "read"  # V1은 read-only; write는 미래 마일스톤(§20)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name or self.id,
            "device_path": str(self.device_path),
            "access_mode": self.access_mode,
        }


@dataclass
class FileRef:
    """불변 identity + 현재 locator (§11). id는 rename/move에도 유지된다."""

    id: str
    root_id: str
    relative_path: str
    name: str
    size: int = 0
    mtime: float = 0.0
    fingerprint: str | None = None
    last_seen: str = ""  # ISO 8601 — reconciliation 증거용
    unresolved: bool = False  # 애매한 move 후보 — 사용자 확인 전까지 미바인딩
    missing: bool = False  # 마지막 스캔에서 파일을 못 찾음 (deleted/ambiguous)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "root_id": self.root_id,
            "relative_path": self.relative_path,
            "name": self.name,
            "size": self.size,
            "mtime": self.mtime,
            "fingerprint": self.fingerprint,
            "last_seen": self.last_seen,
            "unresolved": self.unresolved,
            "missing": self.missing,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FileRef":
        return cls(
            id=str(raw["id"]),
            root_id=str(raw["root_id"]),
            relative_path=str(raw.get("relative_path") or ""),
            name=str(raw.get("name") or ""),
            size=int(raw.get("size") or 0),
            mtime=float(raw.get("mtime") or 0.0),
            fingerprint=raw.get("fingerprint") or None,
            last_seen=str(raw.get("last_seen") or ""),
            unresolved=bool(raw.get("unresolved") or False),
            missing=bool(raw.get("missing") or False),
        )


class FileRefRegistry:
    """FileRef 영속 레지스트리 — 작은 JSON 메타데이터 파일 (PART D).

    위치: <memory_dir>/file_refs.json (scratch·real 모두 JARVIS state 안).
    파생 상태다 — 파일이 없으면 재스캔으로 재구성 가능. 내용 사본을 담지 않는다.
    """

    def __init__(self, registry_file: str | Path) -> None:
        self.registry_file = Path(registry_file)
        self.refs: dict[str, FileRef] = {}
        self._load()

    # ---------- 영속화 ----------

    def _load(self) -> None:
        if not self.registry_file.exists():
            self.refs = {}
            return
        try:
            raw = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.refs = {}
            return
        if not isinstance(raw, dict) or raw.get("version") != REGISTRY_VERSION:
            self.refs = {}
            return
        self.refs = {}
        for item in raw.get("refs", []):
            try:
                ref = FileRef.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue
            self.refs[ref.id] = ref

    def reload(self) -> None:
        """디스크에서 재적재 — 같은 레지스트리 파일을 여러 인스턴스(Discovery와
        file tools, bridge 내부 어댑터)가 공유하므로 스캔/조회 전에 항상 최신
        상태를 읽는다. 그렇지 않으면 오래된 메모리 상태가 저장 시 덮어쓴다."""
        self._load()

    def save(self) -> None:
        payload = {
            "version": REGISTRY_VERSION,
            "refs": [ref.to_dict() for ref in sorted(self.refs.values(), key=lambda r: r.id)],
        }
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_file.parent / f".{self.registry_file.name}.tmp-{uuid.uuid4().hex[:8]}"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.registry_file)

    # ---------- 조회 ----------

    def by_path(self, root_id: str, relative_path: str) -> FileRef | None:
        rel = relative_path.replace("\\", "/")
        for ref in self.refs.values():
            if ref.root_id == root_id and ref.relative_path == rel:
                return ref
        return None

    def by_id(self, ref_id: str) -> FileRef | None:
        return self.refs.get(ref_id)

    def add(self, ref: FileRef) -> FileRef:
        self.refs[ref.id] = ref
        return ref


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _stat_file(path: Path, *, with_fingerprint: bool) -> tuple[int, float, str | None]:
    try:
        stat = path.stat()
    except OSError:
        return 0, 0.0, None
    fingerprint = content_fingerprint(path) if with_fingerprint else None
    return stat.st_size, stat.st_mtime, fingerprint


def _is_ref_candidate(path: Path, root_path: Path) -> bool:
    """민감/숨김 제외 — FileStore의 차단 규칙과 동일한 기준."""
    if _is_sensitive_name(path.name):
        return False
    try:
        rel = path.relative_to(root_path)
    except ValueError:
        return False
    return not any(_is_sensitive_name(part) for part in rel.parts)


def _walk_files(root_path: Path, max_depth: int = 8) -> list[Path]:
    files: list[Path] = []
    for current, dirnames, filenames in os.walk(root_path):
        rel_dir = Path(current).relative_to(root_path)
        depth = 0 if str(rel_dir) == "." else len(rel_dir.parts)
        dirnames[:] = sorted(
            (d for d in dirnames if not d.startswith(".") and not _is_sensitive_name(d)),
            key=str.lower,
        )
        if depth >= max_depth:
            dirnames[:] = []
        for filename in sorted(filenames, key=str.lower):
            if not _is_sensitive_name(filename):
                files.append(Path(current) / filename)
    return files


class WorkspaceManager:
    """WorkspaceRoot 목록 + FileRef 레지스트리의 조정자 (PART B–E).

    FileStore의 경계(루트 검증·containment·민감 차단)를 재사용하고, 그 위에
    identity 계층을 얹는다. filesystem은 canonical — 이 클래스는 identity만.
    """

    def __init__(
        self,
        store: Any,  # FileStore — 순환 import 피하려고 duck typing
        registry_file: str | Path,
    ) -> None:
        from harness.tools.file_store import FileStore

        self.store = store if isinstance(store, FileStore) else FileStore(store)
        self.registry = FileRefRegistry(registry_file)

    # ---------- 스캔/조정 ----------

    def scan_root(
        self,
        root: str | None = None,
        *,
        with_fingerprints: bool = True,
    ) -> dict[str, Any]:
        """한 루트(또는 전체)를 스캔해 FileRef를 만들고 조정한다 (PART E).

        5단계 알고리즘 — 조정이 신규 생성보다 **먼저** 온다(그래야 rename된
        파일이 새 identity를 낭비하지 않는다):
        1. 실제 파일 목록 + 기존 ref의 path 매핑을 만든다.
        2. path가 같은 파일은 기존 ref와 직접 매치(소비).
        3. 매치 안 된 기존 ref(missing)를 매치 안 된 파일(unclaimed)과 조정:
           - 유일 후보(size+fingerprint 또는 size+mtime±90s) → ID 보존 이동.
           - 후보 여러 개 → unresolved(조용한 바인딩 금지) + 보고.
           - 후보 없음 → deleted로 남긴다(영구 삭제 처리 안 함 — 이동 감지용).
        4. 남은 unclaimed 파일에만 새 FileRef를 만든다.
        5. 매치된 ref 메타데이터를 갱신하고 저장.
        """
        root_name, _ = self.store.resolve_root(root)
        root_path = self.store.roots[root_name]

        # 다른 인스턴스가 방금 저장했을 수 있으므로 항상 최신 레지스트리에서 시작
        self.registry.reload()

        # 1. 실제 파일 목록 (민감 제외 — FileStore 규칙과 동일)
        present: dict[str, Path] = {
            path.relative_to(root_path).as_posix(): path
            for path in _walk_files(root_path)
        }

        # 2. path 직접 매치
        matched: dict[str, FileRef] = {}  # rel -> ref
        for ref in list(self.registry.refs.values()):
            if ref.root_id == root_name and ref.relative_path in present:
                matched[ref.relative_path] = ref

        missing = [
            ref for ref in self.registry.refs.values()
            if ref.root_id == root_name and ref.relative_path not in matched
        ]
        unclaimed = [rel for rel in present if rel not in matched]

        # 3. 보수적 rename/move 조정 (신규 생성보다 먼저)
        renamed: list[dict[str, Any]] = []
        ambiguous: list[dict[str, Any]] = []
        missing_paths: list[str] = []
        unclaimed_set = set(unclaimed)
        for ref in sorted(missing, key=lambda r: r.id):
            candidates = self._match_candidates(
                root_path, ref, unclaimed_set, present, with_fingerprints
            )
            if len(candidates) == 1:
                new_path = candidates[0]
                old_path = ref.relative_path
                ref.relative_path = new_path
                ref.name = new_path.rsplit("/", 1)[-1]
                size, mtime, fingerprint = _stat_file(
                    present[new_path], with_fingerprint=with_fingerprints
                )
                ref.size = size
                ref.mtime = mtime
                if with_fingerprints:
                    ref.fingerprint = fingerprint
                ref.last_seen = _now_iso()
                ref.unresolved = False
                ref.missing = False
                matched[new_path] = ref
                unclaimed_set.discard(new_path)
                renamed.append({"id": ref.id, "from": old_path, "to": new_path})
            elif len(candidates) > 1:
                # 애매 — 조용히 묶지 않는다. old ref는 unresolved로 남고,
                # 후보 파일들은 4단계에서 각자 새 identity를 받는다.
                ref.unresolved = True
                ref.missing = True
                ref.last_seen = _now_iso()
                ambiguous.append({
                    "id": ref.id,
                    "from": ref.relative_path,
                    "candidates": sorted(candidates),
                })
                missing_paths.append(ref.relative_path)
            else:
                # deleted — 레지스트리에 남겨 나중 복귀/이동 감지에 쓴다
                ref.missing = True
                missing_paths.append(ref.relative_path)

        # 4. 남은 unclaimed 파일 → 새 FileRef
        created = 0
        for rel in sorted(unclaimed_set):
            size, mtime, fingerprint = _stat_file(
                present[rel], with_fingerprint=with_fingerprints
            )
            self.registry.add(FileRef(
                id=new_file_ref_id(),
                root_id=root_name,
                relative_path=rel,
                name=rel.rsplit("/", 1)[-1],
                size=size,
                mtime=mtime,
                fingerprint=fingerprint,
                last_seen=_now_iso(),
            ))
            created += 1

        # 5. 매치된 ref 메타데이터 갱신
        for rel, ref in matched.items():
            size, mtime, fingerprint = _stat_file(
                present[rel], with_fingerprint=with_fingerprints
            )
            ref.name = rel.rsplit("/", 1)[-1]
            ref.size = size
            ref.mtime = mtime
            if with_fingerprints:
                ref.fingerprint = fingerprint
            ref.last_seen = _now_iso()

        self.registry.save()
        return {
            "root": root_name,
            "scanned_files": len(present),
            "created": created,
            "total_refs": sum(
                1 for r in self.registry.refs.values() if r.root_id == root_name
            ),
            "renamed": renamed,
            "ambiguous": ambiguous,
            "missing": sorted(missing_paths),
        }

    def _match_candidates(
        self,
        root_path: Path,
        ref: FileRef,
        unclaimed: set[str],
        present: dict[str, Path],
        with_fingerprints: bool,
    ) -> list[str]:
        """missing ref의 rename/move 후보를 unclaimed 파일 중에서 찾는다.

        증거 우선순위 (파일명 일치만으로는 매치하지 않는다):
        1. size 동일 + fingerprint 동일 (강한 증거)
        2. size 동일 + mtime ±90초 (이동 중 무수정 가정)
        """
        candidates: list[str] = []
        for rel in sorted(unclaimed):
            size, mtime, fingerprint = _stat_file(
                present[rel], with_fingerprint=with_fingerprints
            )
            if ref.size and size == ref.size:
                if (
                    with_fingerprints
                    and ref.fingerprint
                    and fingerprint == ref.fingerprint
                ):
                    candidates.append(rel)
                    continue
                if abs(mtime - ref.mtime) <= 90:
                    candidates.append(rel)
        return candidates

    # ---------- identity 조회 ----------

    def file_identity(
        self, root: str | None, relative_path: str
    ) -> dict[str, Any]:
        """root+relpath → identity 정보가 포함된 결과 (PART G).

        레지스트리에 없어도 파일이 존재하면 identity 없이 locator만 반환한다 —
        filesystem이 canonical이므로 읽기는 막지 않는다.
        """
        root_name, target = self.store.resolve_path(root, relative_path)
        root_path = self.store.roots[root_name]
        rel_posix = target.relative_to(root_path).as_posix()
        ref = self.registry.by_path(root_name, rel_posix)
        result: dict[str, Any] = {
            "root_id": root_name,
            "relative_path": rel_posix,
            "name": target.name,
        }
        if ref is not None and not ref.missing:
            result["id"] = ref.id
            result["unresolved"] = ref.unresolved
        return result

    def enrich_entry(self, entry: dict[str, Any], root_name: str) -> dict[str, Any]:
        """list/search 결과에 stable identity를 덧붙인다 (PART G).

        identity(id)와 locator(root_id·relative_path)를 분리해 실는다.
        ref가 missing(삭제/애매)이면 id를 붙이지 않는다 — 존재하지 않는 파일을
        identity로 표현하지 않는다(Test 7).
        """
        path = entry.get("path")
        if not path:
            return entry
        self.registry.reload()
        ref = self.registry.by_path(root_name, path)
        if ref is not None and not ref.missing:
            entry["id"] = ref.id
            entry["root_id"] = ref.root_id
        else:
            entry["root_id"] = root_name
        return entry

    def ensure_scanned(self, max_age_s: float = 30.0) -> None:
        """읽기 전에 레지스트리를 파일시스템과 동기화한다 (watcher 없는 V1).

        빠른 경로 검사(파일 경로 집합 비교, 메타데이터 읽기 없음)로 변화를
        감지하면 전체 스캔/조정을 돌린다. rename·move·생성·삭제가 모두 경로
        집합 변화로 드러나므로 mtime 기반보다 정확하고, 파일 내용을 읽지 않으므로
        저렴한다. 집합이 같으면(내용 수정만 있는 경우) max_age 안에서는 스킵 —
        내용 수정은 identity에 영향이 없다.
        """
        self.registry.reload()
        if self._paths_changed():
            for root_name in sorted(self.store.roots):
                self.scan_root(root_name)
            return
        registry_mtime = 0.0
        try:
            registry_mtime = self.registry.registry_file.stat().st_mtime
        except OSError:
            pass
        if time.time() - registry_mtime <= max_age_s and self.registry.refs:
            return
        for root_name in sorted(self.store.roots):
            self.scan_root(root_name)

    def read_text(self, root: str | None, relative_path: str) -> dict[str, Any]:
        """Read an approved text file with its stable identity and revision."""
        root_name, target = self.store.resolve_path(root, relative_path)
        self.ensure_scanned()
        result = self.store.read_text(relative_path, root=root_name)
        ref = self.registry.by_path(root_name, result["path"])
        if ref is None or ref.missing or ref.unresolved:
            raise FileStoreError("파일 identity를 확인할 수 없습니다")
        result["id"] = ref.id
        result["root_id"] = root_name
        result["revision"] = {
            "size": target.stat().st_size,
            "mtime": target.stat().st_mtime,
            "fingerprint": content_fingerprint(target),
        }
        return result

    def update_text(
        self,
        root_id: str,
        file_id: str,
        relative_path: str,
        content: str,
        expected_revision: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Atomically update one existing UTF-8 text file after revision checking."""
        if not isinstance(content, str):
            raise FileStoreError("content는 문자열이어야 합니다")
        if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
            raise FileStoreError(f"파일이 너무 큽니다 ({MAX_EDIT_BYTES} bytes 제한)")
        if not isinstance(file_id, str) or not file_id.strip():
            raise FileStoreError("file_id(FileRef identity)가 필요합니다")
        if not isinstance(root_id, str) or not root_id.strip():
            raise FileStoreError("root_id가 필요합니다")

        self.registry.reload()
        ref = self.registry.by_id(file_id.strip())
        if ref is None or ref.missing or ref.unresolved:
            raise FileStoreError("존재하지 않거나 해결되지 않은 FileRef입니다")
        if ref.root_id != root_id.strip():
            raise FileStoreError("FileRef가 지정한 WorkspaceRoot에 속하지 않습니다")
        normalized = str(relative_path or "").replace("\\", "/")
        if normalized != ref.relative_path:
            raise FileStoreError("FileRef 경로와 요청 경로가 일치하지 않습니다")

        resolved_root, target = self.store.resolve_path(ref.root_id, ref.relative_path)
        if resolved_root != ref.root_id or not target.is_file():
            raise FileStoreError("대상 파일을 확인할 수 없습니다")
        if target.suffix.lower() not in TEXT_EXTENSIONS:
            raise FileStoreError(f"편집할 수 없는 파일 형식입니다: {target.suffix or '(확장자 없음)'}")

        try:
            target.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FileStoreError(
                "UTF-8로 해석할 수 없는 파일은 편집할 수 없습니다"
            ) from exc
        except OSError as exc:
            raise FileStoreError(f"파일을 읽을 수 없습니다: {exc}") from exc

        current_revision = {
            "size": target.stat().st_size,
            "mtime": target.stat().st_mtime,
            "fingerprint": content_fingerprint(target),
        }
        expected = expected_revision or {}
        if (
            expected.get("size") != current_revision["size"]
            or expected.get("fingerprint") != current_revision["fingerprint"]
        ):
            raise FileStoreError("이 파일이 외부에서 변경되었습니다. Reload 후 다시 저장하세요.")

        tmp = target.with_name(f".{target.name}.jarvis-save-{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8", newline="")
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise FileStoreError(f"파일을 저장할 수 없습니다: {exc}") from exc

        self.scan_root(ref.root_id)
        updated = self.read_text(ref.root_id, ref.relative_path)
        updated["saved"] = True
        return updated

    def _paths_changed(self) -> bool:
        """현재 파일 경로 집합 != 레지스트리의 live 경로 집합인지 (metadata-only)."""
        live: dict[str, set[str]] = {}
        for root_name, root_path in self.store.roots.items():
            live[root_name] = {
                path.relative_to(root_path).as_posix()
                for path in _walk_files(root_path)
            }
        for root_name, paths in live.items():
            registered = {
                ref.relative_path for ref in self.registry.refs.values()
                if ref.root_id == root_name and not ref.missing
            }
            if paths != registered:
                return True
        return False


def default_registry_file(memory_dir: Path | str | None) -> Path:
    """레지스트리 위치 관례: <memory_dir>/file_refs.json (PART D)."""
    base = Path(memory_dir) if memory_dir else Path(".")
    return base / "file_refs.json"
