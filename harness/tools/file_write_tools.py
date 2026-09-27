"""모델용 파일 쓰기 tool — create_file (write, 승인 gate §8.4).

사용자 결정(2026-09-26): "새 파일 생성은 둘 다" — 모델은 승인 경로로,
사용자는 UI에서 deterministic하게. 이 모듈은 모델 경로다.

경계:
- 대상은 **승인된 루트 안의 상대 경로**만 — 절대 경로/루트 밖은 FileStore가
  원천 차단한다. 디스크 전체 읽기(list_disk/read_disk_file)와 달리 쓰기는
  승인 루트로 한정한다(읽기 확장과 쓰기 확장의 위험이 다르기 때문).
- 새 파일만 — 이미 있는 파일은 실패한다(덮어쓰기는 UI 편집기의 revision
  충돌 검사 경로만 허용한다).
- kind="write"이므로 registry gate가 사용자 confirm 없이는 handler를
  호출하지 않는다(§8.3-2) — create_task와 동일한 승인 흐름을 그대로 쓴다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.tools.file_store import (
    DEFAULT_MAX_FILE_SIZE,
    TEXT_EXTENSIONS,
    FileStore,
    FileStoreError,
)
from harness.tools.registry import Tool
from harness.tools.schemas import create_file as create_file_schema
from harness.tools.workspace import WorkspaceManager, default_registry_file

# 새 파일 1개 기준 상한 — read 상한과 별개로 잡는다(초기 생성은 크지 않다).
MAX_CREATE_BYTES = 256 * 1024


def _preflight(arguments: dict[str, Any]) -> dict[str, Any]:
    """승인 카드에 표시할 요약과 사전 검증 (side-effect 없음)."""
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        raise FileStoreError("path가 필요합니다")
    content = arguments.get("content")
    if content is not None and not isinstance(content, str):
        raise FileStoreError("content는 문자열이어야 합니다")
    if content is not None and len(content.encode("utf-8")) > MAX_CREATE_BYTES:
        raise FileStoreError(f"내용이 너무 큽니다 ({len(content.encode('utf-8'))} bytes)")
    return {"summary": path.strip()}


def build_create_file_tool(
    files: FileStore,
    memory_dir: str | Path | None = None,
) -> Tool:
    """승인된 루트 안에 새 텍스트 파일을 만드는 write tool."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            raise FileStoreError("path가 필요합니다")
        content = arguments.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise FileStoreError("content는 문자열이어야 합니다")
        if len(content.encode("utf-8")) > MAX_CREATE_BYTES:
            raise FileStoreError(f"내용이 너무 큽니다 ({len(content.encode('utf-8'))} bytes)")

        root_name, root_path = files.resolve_root(arguments.get("root"))
        rel = str(path).strip().replace("\\", "/")
        if rel in ("", "."):
            raise FileStoreError("파일 이름이 필요합니다")
        candidate = Path(rel)
        if candidate.is_absolute() or ":" in rel:
            raise FileStoreError(f"절대 경로는 허용되지 않습니다: {rel!r}")
        if ".." in candidate.parts:
            raise FileStoreError(f"상위 경로 이동(..)은 허용되지 않습니다: {rel!r}")
        target = (root_path / candidate)
        # containment — resolve 후 재검사(심볼릭 링크 포함). 루트 밖이면 실패.
        resolved_target = target.resolve()
        resolved_root = root_path.resolve()
        if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
            raise FileStoreError(f"루트 밖 경로입니다: {rel!r} (루트: {root_name})")
        target = resolved_target
        root_name_for_result = root_name
        if target.exists():
            raise FileStoreError(
                f"이미 존재하는 파일입니다 — 덮어쓰지 않습니다: {path!r}"
            )
        if target.suffix.lower() not in TEXT_EXTENSIONS:
            raise FileStoreError(
                f"만들 수 없는 파일 형식입니다 (확장자 {target.suffix!r})"
            )
        parent = target.parent
        if not parent.is_dir():
            # 중간 폴더가 없으면 만든다 — 루트 안에서만 허용된다.
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise FileStoreError(f"상위 폴더를 만들 수 없습니다: {exc}") from exc

        tmp = parent / f".{target.name}.jarvis-create-{path and ''}{abs(hash(path)) % 99999}.tmp"
        try:
            tmp.write_text(content, encoding="utf-8", newline="")
            tmp.replace(target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise FileStoreError(f"파일을 만들 수 없습니다: {exc}") from exc

        # identity 부착 — 승인 루트 안이므로 FileRef를 발급할 수 있다.
        result = {
            "ok": True,
            "root": root_name,
            "path": target.relative_to(files.roots[root_name]).as_posix(),
            "name": target.name,
            "size": len(content.encode("utf-8")),
            "created": True,
        }
        if memory_dir is not None:
            try:
                workspace = WorkspaceManager(
                    files, default_registry_file(Path(memory_dir))
                )
                workspace.scan_root(root_name)
                identity = workspace.file_identity(root_name, result["path"])
                if "id" in identity:
                    result["file_id"] = identity["id"]
                    result["root_id"] = identity.get("root_id")
            except Exception:
                # identity 부착 실패가 생성 자체를 무효로 하지 않는다.
                pass
        return result

    return Tool(
        schema=create_file_schema(),
        kind="write",
        handler=handler,
        preflight=_preflight,
    )
