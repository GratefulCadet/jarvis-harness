"""디스크 전체 읽기 계층 (File access 확장).

사용자 결정(2026-09-26): 읽기는 전체 디스크로 허용한다. 쓰기·FileRef identity는
여전히 승인된 루트 안에서만 가능하다 — FileStore/ProjectWorkspace를 건드리지
않고 별도 경량 모듈로 둔다. FileStore의 민감 이름 차단(_is_sensitive_name)과
텍스트 확장자·크기 상한 정책을 재사용해 위험도를 낮춘다.

경계:
- list: 지정 절대 경로(또는 드라이브 루트)의 한 단계 목록 — 재귀 없음.
- read: 텍스트 확장자 + 크기 상한 + 민감 이름 차단. 바이너리 거부.
- 쓰기 경로는 없다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harness.tools.file_store import (
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_READ_CHARS,
    MAX_READ_CHARS_CAP,
    TEXT_EXTENSIONS,
    FileStoreError,
    _is_sensitive_name,
)

_UNREADABLE_DIR_HINTS = (
    "System Volume Information",
    "$RECYCLE.BIN",
    "Windows",
    "Config.Msi",
    "Recovery",
)


def drive_roots() -> list[dict[str, str]]:
    """마운트된 드라이브 루트 목록 (Windows 드라이브 문자 + POSIX '/')."""
    roots: list[dict[str, str]] = []
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:\\")
            if root.exists():
                roots.append({"name": f"{letter}:", "path": str(root)})
    else:
        roots.append({"name": "/", "path": "/"})
    return roots


def _safe_dir_label(path: Path) -> str:
    name = path.name or str(path)
    if name in _UNREADABLE_DIR_HINTS:
        return name
    return name


def list_disk(path: str | None = None, limit: int = 200) -> dict[str, Any]:
    """절대 경로(또는 드라이브 루트)의 한 단계 목록을 반환한다.

    재귀 없음 — 디스크 전체 순회를 막는다. OS가 탐색을 거부하는 시스템
    디렉터리는 error 항목으로 표시한다(전체 실패로 만들지 않는다).
    """
    if not path or not str(path).strip():
        return {
            "kind": "drives",
            "drives": drive_roots(),
            "hint": "path에 절대 경로를 지정하면 그 폴더의 한 단계 목록을 반환한다.",
        }

    target_raw = str(path).strip()
    try:
        target = Path(target_raw).resolve()
    except OSError as exc:
        raise FileStoreError(f"경로를 해석할 수 없습니다: {target_raw!r} ({exc})") from exc
    if not target.exists():
        raise FileStoreError(f"존재하지 않는 경로: {target_raw!r}")

    if target.is_file():
        # 파일을 가리키면 부모 목록 + 파일 자체 정보를 함께 준다.
        parent_entries = list_disk(str(target.parent), limit=limit)
        return {
            "kind": "listing",
            "path": str(target.parent),
            "entries": parent_entries.get("entries", []),
            "focus": {
                "name": target.name,
                "path": str(target),
                "type": "file",
                "size": target.stat().st_size,
                "text": target.suffix.lower() in TEXT_EXTENSIONS,
            },
        }

    entries: list[dict[str, Any]] = []
    blocked = 0
    truncated = False
    try:
        children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        return {
            "kind": "listing",
            "path": str(target),
            "entries": [],
            "error": f"폴더를 열 수 없습니다: {exc}",
            "stats": {"entries": 0, "blocked": 0, "truncated": False},
        }

    for child in children:
        if len(entries) >= max(1, int(limit)):
            truncated = True
            break
        if _is_sensitive_name(child.name):
            blocked += 1
            entries.append({
                "type": "blocked",
                "name": child.name,
                "path": str(child),
            })
            continue
        try:
            if child.is_dir():
                entries.append({
                    "type": "dir",
                    "name": child.name,
                    "path": str(child),
                })
            elif child.is_file():
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
                entries.append({
                    "type": "file",
                    "name": child.name,
                    "path": str(child),
                    "size": size,
                    "text": child.suffix.lower() in TEXT_EXTENSIONS,
                })
        except OSError:
            continue

    return {
        "kind": "listing",
        "path": str(target),
        "entries": entries,
        "stats": {"entries": len(entries), "blocked": blocked, "truncated": truncated},
    }


def read_disk_file(path: str, max_chars: int | None = None) -> dict[str, Any]:
    """절대 경로의 텍스트 파일을 경계 있는 내용으로 읽는다.

    FileStore.read_text와 동일한 정책(텍스트 확장자·크기 상한·UTF-8·민감
    이름 차단)을 절대 경로에 적용한다. 쓰기는 제공하지 않는다.
    """
    raw = str(path or "").strip()
    if not raw:
        raise FileStoreError("path가 필요합니다")
    try:
        target = Path(raw).resolve()
    except OSError as exc:
        raise FileStoreError(f"경로를 해석할 수 없습니다: {raw!r} ({exc})") from exc

    if _is_sensitive_name(target.name):
        raise FileStoreError(f"민감 파일은 읽을 수 없습니다: {target.name!r}")
    if not target.is_file():
        raise FileStoreError(f"파일이 아닙니다: {raw!r}")
    if target.suffix.lower() not in TEXT_EXTENSIONS:
        raise FileStoreError(
            f"텍스트 파일이 아닙니다 (확장자 {target.suffix!r}): {target.name!r}"
        )
    size = target.stat().st_size
    if size > DEFAULT_MAX_FILE_SIZE:
        raise FileStoreError(
            f"파일이 너무 큽니다 ({size} bytes > {DEFAULT_MAX_FILE_SIZE}): {target.name!r}"
        )
    limit = max(1, min(int(max_chars or DEFAULT_READ_CHARS), MAX_READ_CHARS_CAP))
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise FileStoreError(
            f"UTF-8 텍스트가 아니라 읽을 수 없습니다: {target.name!r}"
        ) from exc
    except OSError as exc:
        raise FileStoreError(f"파일을 읽을 수 없습니다: {exc}") from exc
    truncated = len(content) > limit
    return {
        "path": str(target),
        "name": target.name,
        "size": size,
        "chars": len(content[:limit]),
        "truncated": truncated,
        "content": content[:limit],
    }
