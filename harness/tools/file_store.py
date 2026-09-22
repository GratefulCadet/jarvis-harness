from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

"""Read-only filesystem store (JARVIS_IMPLEMENTATION_CONTEXT §7 — file access).

Qwen·renderer에게 무제한 파일시스템 접근을 주지 않기 위한 Harness 경계다
(§7 preferred boundary: Qwen → Harness Tool → path/permission validation → FS).

보안 규칙 (§7 + milestone 요구사항):
- 접근은 **사용자가 명시적으로 승인한 루트** 안에서만 허용된다. 루트는
  JARVIS_FILE_ROOTS 환경변수(`name=path` 콤마 나열) 또는 configs/harness.yaml의
  tools.file_roots(name=path dict)로만 등록된다. 기본 루트는 없다 — 사용자
  프로필/드라이브를 자동으로 스캔하지 않는다.
- 루트 안에서도 민감 대상은 기본 차단한다: .env, .ssh, credentials, 토큰/키
  파일, 브라우저 프로필 등 (민감 디렉터리는 그 아래 전체 차단).
- `..`, 절대경로, 드라이브 문자, 심볼릭 링크 탈출은 resolve 후 containment
  검사로 막는다 — 루트 밖이면 내용을 1바이트도 반환하지 않는다.
- 검색·나열에는 상한이 있다: max_depth, max_results, max_file_size,
  텍스트 확장자 화이트리스트, 바이너리 제외. 백그라운드 인덱싱은 없다.

File != Page (§3): 이 모듈은 물리 파일의 canonical read view일 뿐이며,
PageStore(마크다운 지식 페이지)와 정체성을 공유하지 않는다.
"""

# 텍스트로 취급할 확장자 화이트리스트 (검색/읽기 대상)
TEXT_EXTENSIONS = frozenset({
    ".md", ".txt", ".py", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv",
    ".html", ".htm", ".css", ".sh", ".bat", ".ps1", ".rst", ".log",
    ".c", ".h", ".cpp", ".hpp", ".java", ".rs", ".go", ".sql", ".tex",
})

# 본문 검색/읽기 차단 파일명 (대소문자 무시, 확장자 포함 매칭)
_SENSITIVE_FILE_NAMES = frozenset({
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials", "credentials.json", "credentials.yaml",
    ".netrc", ".git-credentials", ".npmrc", ".pypirc",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "authorized_keys", "known_hosts",
})

# 파일명에 이 패턴이 있으면 차단 (token/secret/key/credential 계열)
_SENSITIVE_FILE_RE = re.compile(
    r"(credential|secret|token|passw|private[_-]?key|api[_-]?key|\.pem$|\.key$|\.pfx$|\.p12$)",
    re.IGNORECASE,
)

# 이 이름의 디렉터리 아래는 전체 차단 (브라우저 프로필·시스템 상태 등)
_SENSITIVE_DIR_NAMES = frozenset({
    ".ssh", ".gnupg", ".aws", ".kube", ".docker",
    "credentials", ".password-store", ".config", ".git",
})

DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_RESULTS = 20
DEFAULT_MAX_FILE_SIZE = 256 * 1024  # 검색 시 읽어 보는 최대 파일 크기 (256KB)
DEFAULT_READ_CHARS = 8000
MAX_READ_CHARS_CAP = 50_000
CONTEXT_CHARS = 120  # 본문 매칭 시 앞뒤로 보여줄 문자 수


class FileStoreError(ValueError):
    """승인 루트·경로·대상 검증 실패 — tool 결과로 되돌려진다."""


def parse_roots(raw: str | dict[str, str] | None) -> dict[str, Path]:
    """JARVIS_FILE_ROOTS(`name=path,name2=path2`) 또는 dict → {name: Path}.

    빈 값/잘못된 항목은 건너뛴다(등록 실패가 전체를 막지 않게). 이름은 영문
    숫자 - _ 만 허용한다.
    """
    roots: dict[str, Path] = {}
    if not raw:
        return roots
    if isinstance(raw, dict):
        items = list(raw.items())
    else:
        items = []
        for chunk in str(raw).split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            name, sep, value = chunk.partition("=")
            if not sep:
                continue
            items.append((name, value))
    for name, value in items:
        name = str(name).strip()
        value = str(value).strip().strip('"')
        if not name or not value:
            continue
        if not re.match(r"^[A-Za-z0-9_-]+$", name):
            continue
        try:
            path = Path(value).expanduser().resolve()
        except OSError:
            continue
        if path.is_dir():
            roots[name] = path
    return roots


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in _SENSITIVE_FILE_NAMES or lowered in _SENSITIVE_DIR_NAMES:
        return True
    return bool(_SENSITIVE_FILE_RE.search(lowered))


class FileStore:
    """승인된 루트들의 read-only 파일 접근 계층. File의 canonical read source."""

    def __init__(self, roots: dict[str, str | Path] | None = None) -> None:
        resolved: dict[str, Path] = {}
        for name, path in (roots or {}).items():
            candidate = Path(path).expanduser().resolve()
            if candidate.is_dir():
                resolved[name] = candidate
        self.roots = resolved

    # ---------- 루트/경로 검증 ----------

    def resolve_root(self, root: str | None) -> tuple[str, Path]:
        """root 이름 → (name, path). None이면 유일한 루트를 쓴다."""
        if not self.roots:
            raise FileStoreError(
                "승인된 파일 루트가 없습니다. JARVIS_FILE_ROOTS(name=path, ...) "
                "또는 configs/harness.yaml tools.file_roots로 사용자가 직접 "
                "승인한 루트만 등록됩니다."
            )
        if root is None or not str(root).strip():
            if len(self.roots) > 1:
                raise FileStoreError(
                    "루트가 여러 개입니다 — root를 지정하세요: "
                    + ", ".join(sorted(self.roots))
                )
            name = next(iter(self.roots))
            return name, self.roots[name]
        name = str(root).strip()
        if name not in self.roots:
            raise FileStoreError(
                f"승인되지 않은 루트: {name!r}. 승인된 루트: "
                + ", ".join(sorted(self.roots))
            )
        return name, self.roots[name]

    def resolve_path(self, root: str | None, relative: str | None) -> tuple[str, Path]:
        """루트 기준 상대경로 → 검증된 절대경로. containment·민감 검사 포함."""
        root_name, root_path = self.resolve_root(root)
        rel = (relative or "").strip().replace("\\", "/")
        if rel in ("", "."):
            return root_name, root_path
        # 명밴한 공격 벡터는 resolve 전에 거부 (오류 메시지를 명확하게)
        candidate = Path(rel)
        if candidate.is_absolute() or re.match(r"^[A-Za-z]:", rel):
            raise FileStoreError(f"절대 경로는 허용되지 않습니다: {rel!r}")
        if ".." in candidate.parts:
            raise FileStoreError(f"상위 경로 이동(..)은 허용되지 않습니다: {rel!r}")

        target = (root_path / candidate).resolve()
        # containment — resolve 후 재검사(심볼릭 링크 포함). 루트 밖이면 실패.
        if target != root_path and root_path not in target.parents:
            raise FileStoreError(
                f"루트 밖 경로입니다: {rel!r} (루트: {root_name})"
            )
        if not target.exists():
            raise FileStoreError(f"존재하지 않는 경로: {rel!r}")
        # 민감 대상 차단 — 경로의 어떤 구성요소도 민감하면 차단
        try:
            relative_to_root = target.relative_to(root_path)
        except ValueError as exc:  # 방어적 — containment 재확인
            raise FileStoreError(f"루트 밖 경로입니다: {rel!r}") from exc
        for part in relative_to_root.parts:
            if _is_sensitive_name(part):
                raise FileStoreError(
                    f"민감 파일/디렉터리는 접근이 차단됩니다: {part!r}"
                )
        return root_name, target

    # ---------- 나열 ----------

    def list_tree(
        self,
        root: str | None = None,
        relative: str | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> dict[str, Any]:
        """디렉터리 → 경계 있는 재귀 트리. 민감 항목은 'blocked' 노드로 노출."""
        root_name, base = self.resolve_root(root)
        root_path = self.roots[root_name]
        _, target = self.resolve_path(root, relative)
        if not target.is_dir():
            raise FileStoreError(f"디렉터리가 아닙니다: {relative!r}")
        depth_limit = max(1, min(int(max_depth), 8))
        entries: list[dict[str, Any]] = []
        stats = {"dirs": 0, "files": 0, "blocked": 0, "truncated": False}

        def scan(directory: Path, depth: int) -> None:
            if stats["truncated"]:
                return
            if depth > depth_limit:
                stats["truncated"] = True
                return
            try:
                children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                return
            for child in children:
                if _is_sensitive_name(child.name):
                    stats["blocked"] += 1
                    entries.append({
                        "type": "blocked",
                        "name": child.name,
                        "path": child.relative_to(root_path).as_posix(),
                        "depth": depth,
                    })
                    continue
                rel_posix = child.relative_to(root_path).as_posix()
                if child.is_dir():
                    stats["dirs"] += 1
                    node: dict[str, Any] = {
                        "type": "dir",
                        "name": child.name,
                        "path": rel_posix,
                        "depth": depth,
                        "children": [],
                    }
                    entries.append(node)
                    scan(child, depth + 1)
                elif child.is_file():
                    stats["files"] += 1
                    entries.append({
                        "type": "file",
                        "name": child.name,
                        "path": rel_posix,
                        "depth": depth,
                        "size": child.stat().st_size,
                        "text": child.suffix.lower() in TEXT_EXTENSIONS,
                    })

        scan(target, 0)
        return {
            "root": root_name,
            "path": target.relative_to(root_path).as_posix() if target != root_path else "",
            "entries": entries,
            "stats": stats,
        }

    # ---------- 읽기 ----------

    def read_text(
        self,
        path: str,
        root: str | None = None,
        max_chars: int = DEFAULT_READ_CHARS,
    ) -> dict[str, Any]:
        """텍스트 파일 → 경계 있는 내용. 바이너리/민감/초과 크기는 거부."""
        root_name, target = self.resolve_path(root, path)
        if not target.is_file():
            raise FileStoreError(f"파일이 아닙니다: {path!r}")
        if target.suffix.lower() not in TEXT_EXTENSIONS:
            raise FileStoreError(
                f"텍스트 파일이 아닙니다 (확장자 {target.suffix!r}): {path!r}"
            )
        size = target.stat().st_size
        if size > DEFAULT_MAX_FILE_SIZE:
            raise FileStoreError(
                f"파일이 너무 큽니다 ({size} bytes > {DEFAULT_MAX_FILE_SIZE}): {path!r}"
            )
        limit = max(1, min(int(max_chars or DEFAULT_READ_CHARS), MAX_READ_CHARS_CAP))
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise FileStoreError(
                f"UTF-8로 해석할 수 없는 파일은 편집할 수 없습니다: {path!r}"
            ) from exc
        except OSError as exc:
            raise FileStoreError(f"파일을 읽을 수 없습니다: {exc}") from exc
        truncated = len(content) > limit
        return {
            "root": root_name,
            "path": target.relative_to(self.roots[root_name]).as_posix(),
            "size": size,
            "chars": len(content[:limit]),
            "truncated": truncated,
            "content": content[:limit],
        }

    # ---------- 검색 ----------

    def search(
        self,
        query: str,
        root: str | None = None,
        limit: int = DEFAULT_MAX_RESULTS,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> dict[str, Any]:
        """이름/경로/텍스트 본문 결정적 검색. 결과 수·깊이·크기 상한 적용."""
        needle = (query or "").strip().lower()
        if not needle:
            raise FileStoreError("검색어가 비어 있습니다")
        max_results = max(1, min(int(limit or DEFAULT_MAX_RESULTS), 100))
        depth_limit = max(1, min(int(max_depth), 8))

        # root 미지정 시 모든 루트 검색
        if root and str(root).strip():
            root_name, _ = self.resolve_root(root)
            search_roots = {root_name: self.roots[root_name]}
        else:
            search_roots = dict(self.roots)

        results: list[dict[str, Any]] = []
        scanned = 0
        for root_name, root_path in sorted(search_roots.items()):
            if len(results) >= max_results:
                break
            for current, dirnames, filenames in os.walk(root_path):
                rel_dir = Path(current).relative_to(root_path)
                depth = 0 if str(rel_dir) == "." else len(rel_dir.parts)
                # 민감 디렉터리/숨김 디렉터리는 아예 내려가지 않는다
                dirnames[:] = sorted(
                    (d for d in dirnames
                     if not _is_sensitive_name(d) and not d.startswith(".")),
                    key=str.lower,
                )
                if depth >= depth_limit:
                    dirnames[:] = []
                for filename in sorted(filenames, key=str.lower):
                    if len(results) >= max_results:
                        break
                    if _is_sensitive_name(filename):
                        continue
                    filepath = Path(current) / filename
                    rel_posix = filepath.relative_to(root_path).as_posix()
                    name_match = (
                        needle in filename.lower()
                        or needle in rel_posix.lower()
                    )
                    if name_match:
                        results.append({
                            "root": root_name,
                            "path": rel_posix,
                            "name": filename,
                            "matched_on": "name",
                        })
                        continue
                    if filepath.suffix.lower() not in TEXT_EXTENSIONS:
                        continue
                    try:
                        if filepath.stat().st_size > DEFAULT_MAX_FILE_SIZE:
                            continue
                        scanned += 1
                        text = filepath.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        continue
                    lowered = text.lower()
                    position = lowered.find(needle)
                    if position < 0:
                        continue
                    start = max(0, position - CONTEXT_CHARS)
                    end = min(len(text), position + len(needle) + CONTEXT_CHARS)
                    snippet = text[start:end].replace("\n", " ").strip()
                    results.append({
                        "root": root_name,
                        "path": rel_posix,
                        "name": filename,
                        "matched_on": "content",
                        "snippet": (
                            ("…" if start > 0 else "") + snippet
                            + ("…" if end < len(text) else "")
                        ),
                    })
        return {
            "query": query,
            "count": len(results),
            "results": results,
            "scanned_files": scanned,
            "roots": sorted(search_roots),
        }
