"""AI Edit V1 — 제안(edit proposal) 기반 파일 수정 (authoritative mutation owner).

사용자 흐름(2026-09-27 milestone):
    read → edit proposal → visible diff → explicit approval
    → revision-safe apply → editor refresh → 1-step undo

이 모듈이 canonical 파일을 바꾸는 유일한 경로다. 핵심 설계:

- **제안은 canonical 파일을 건드리지 않는다.** create()는 파일을 읽어 base_revision과
  diff를 계산하고, 제안 레코드만 남긴다. 승인 전까지 디스크는 그대로다.
- **base_revision이 제안에서 apply까지 살아야 한다.** 모델이 만든 tool_call 인자는
  재실행 시 새로 평가되므로 여기에 의존하면 사용자가 승인한 것과 다른 내용이
  쓰인다. 그래서 제안 레코드를 한 개 저장한다(신규 persistence layer가 아니라
  file_refs.json과 같은 <memory_dir> 관례를 따르는 필수 상태다).
- **쓰기 경로를 두 개 만들지 않는다.** apply()는 기존
  WorkspaceManager.update_text를 호출한다 — revision 검사 + 원자적 tmp/os.replace가
  이미 그 안에 있다. 여기서 파일을 직접 쓰지 않는다.
- **undo는 같은 revision-safe 경로를 재사용한다.** 되돌리기 역시 update_text로
  하므로 "수정"과 "복원"이 한 규칙을 공유한다.

경계(범위 밖, V1에서 명시적으로 거부):
- multi-file edit, rename, move, delete
- 검토 없는 덮어쓰기 (revision 불일치 시 절대 쓰지 않는다)
- autonomous background mutation (apply는 renderer가 사용자 클릭으로만 호출)
"""
from __future__ import annotations

import difflib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from harness.tools.file_store import FileStoreError, _is_sensitive_name
from harness.tools.workspace import MAX_EDIT_BYTES, WorkspaceManager

STORE_VERSION = 1
PROPOSAL_ID_PREFIX = "e-"
DIFF_CONTEXT_LINES = 3
# 거절된 제안까지 무한히 쌓이지 않게 최근 N개만 유지한다. 디스크 파일을
# 정본으로 삼는 제안이라 조용히 오래 남겨두지 않는다.
MAX_RECORDS = 50


class EditConflictError(FileStoreError):
    """제안 이후 파일이 externally 변경됨 — 절대 덮어쓰지 않는다."""


def default_edit_proposals_file(memory_dir: Path | str | None) -> Path:
    """제안 저장 위치 관례 — <memory_dir>/edit_proposals.json (file_refs.json 동급)."""
    base = Path(memory_dir) if memory_dir else Path(".")
    return base / "edit_proposals.json"


# ---------- diff ----------


def unified_diff_lines(
    before: str,
    after: str,
    relative_path: str,
    context: int = DIFF_CONTEXT_LINES,
) -> list[str]:
    """표준 unified diff를 라인 배열로 만든다 (개행 없는 문자열).

    사용자에게 보여줄 것은 "무엇이 없어지고 무엇이 생기는가"이므로 표준 형식을
    그대로 쓴다. 바이트가 다를 뿐이라 content만으로 계산한다.
    """
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    raw = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{relative_path}",
        tofile=f"b/{relative_path}",
        n=context,
    )
    return [_strip_line_end(line) for line in raw]


def _strip_line_end(line: str) -> str:
    return line.rstrip("\n").rstrip("\r")


def _read_exact_text(target: Path) -> str:
    """바이트 그대로의 텍스트 — 개행 변환을 하지 않는다.

    Path.read_text()는 유니버설 개행으로 CRLF를 \n으로 접는다. 그 값을 undo의
    before_content로 쓰면 되돌릴 때 원본 바이트를 재현할 수 없다(줄 끝이 LF로
    정규화돼 파일 전체가 바뀐다). 그래서 newline=""로 정확히 읽는다.
    """
    try:
        with open(target, "r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise FileStoreError("UTF-8로 해석할 수 없는 파일은 편집할 수 없습니다") from exc
    except OSError as exc:
        raise FileStoreError(f"파일을 읽을 수 없습니다: {exc}") from exc


def _detect_newline(text: str) -> str:
    """파일의 지배적 개행 — CRLF 파일을 LF로 바꾸지 않기 위해 필요하다."""
    index = text.find("\n")
    if index > 0 and text[index - 1] == "\r":
        return "\r\n"
    return "\n"


def _apply_newline(text: str, newline: str) -> str:
    """모델이 준 LF 텍스트를 대상 파일의 개행 규약에 맞춘다.

    CRLF 파일을 LF로 쓰면 diff의 '변경 없음' 행까지 전부 바뀐 것으로 보인다.
    모델이 줄을 리터럴 \r\n으로 보낼 필요는 없고, 우리는 항상 LF로 해석한 뒤
    대상 파일 규약으로 되돌린다.
    """
    if newline == "\n":
        return text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


def diff_stats(diff: list[str]) -> dict[str, int]:
    """+/- 줄 수와 총 변경 줄 수를 센다 (헤더는 제외)."""
    added = 0
    removed = 0
    for line in diff:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed, "changed": added + removed}


# ---------- 저장소 ----------


class EditProposalStore:
    """제안 레코드 저장소 — 단일 JSON 파일, 파생 상태다.

    파일시스템이 canonical이다. 이 파일은 제안(아직 적용되지 않은 의도)와
    undo 기록만 보유하고, 언제든 재구성·삭제할 수 있다.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._records: dict[str, dict[str, Any]] | None = None

    # -- persistence --
    def _load(self) -> dict[str, dict[str, Any]]:
        if self._records is not None:
            return self._records
        records: dict[str, dict[str, Any]] = {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            raw = ""
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict) and parsed.get("version") == STORE_VERSION:
                items = parsed.get("proposals")
                if isinstance(items, dict):
                    records = {
                        str(k): v for k, v in items.items() if isinstance(v, dict)
                    }
        self._records = records
        return records

    def reload(self) -> None:
        self._records = None
        self._load()

    def _save(self) -> None:
        records = self._load()
        # 오래된 제안부터 버린다 (dict는 삽입 순서를 유지한다).
        if len(records) > MAX_RECORDS:
            ordered = list(records.items())
            records.clear()
            records.update(ordered[-MAX_RECORDS:])
        payload = {
            "version": STORE_VERSION,
            "proposals": records,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # -- record helpers --
    def get(self, proposal_id: Any) -> dict[str, Any] | None:
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            return None
        return self._load().get(proposal_id.strip())

    def put(self, record: dict[str, Any]) -> None:
        self._load()[str(record["proposal_id"])] = record
        self._save()

    def latest_proposed(self, file_id: str | None = None) -> dict[str, Any] | None:
        """가장 최근의 미해결(제안/적용됨) 제안 — UI 복구용."""
        candidates = [
            record
            for record in self._load().values()
            if record.get("status") in ("proposed", "applied")
            and (file_id is None or record.get("file_id") == file_id)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: float(r.get("created_at") or 0.0))


def new_proposal_id() -> str:
    return f"{PROPOSAL_ID_PREFIX}{uuid.uuid4().hex[:12]}"


# ---------- public projection ----------


def public_view(record: dict[str, Any]) -> dict[str, Any]:
    """저장 레코드 → UI/브리지로 나가는 형태.

    before/after 전문을 그대로 노출하지 않는다 — UI는 diff로 충분하고, 내용을
    두 벌 더 싣는 것은 bridge payload를 불필요하게 키운다. undo는
    저장소(신뢰 경계 내부)에서만 읽는다.
    """
    diff = list(record.get("diff") or [])
    return {
        "proposal_id": record.get("proposal_id"),
        "root_id": record.get("root_id"),
        "file_id": record.get("file_id"),
        "relative_path": record.get("relative_path"),
        "name": record.get("name"),
        "absolute_path": record.get("absolute_path"),
        "summary": record.get("summary") or "",
        "base_revision": record.get("base_revision"),
        "revision_after_apply": record.get("revision_after_apply"),
        "status": record.get("status"),
        "diff": diff,
        "stats": diff_stats(diff),
        "created_at": record.get("created_at"),
    }


# ---------- operations ----------


def _resolve_target(
    workspace: WorkspaceManager,
    root_id: Any,
    file_id: Any,
    relative_path: Any,
) -> tuple[str, str]:
    """FileRef identity + 경로 정합성 검사. 추측으로 다른 파일을 건드리지 않는다."""
    if not isinstance(root_id, str) or not root_id.strip():
        raise FileStoreError("root_id가 필요합니다")
    if not isinstance(file_id, str) or not file_id.strip():
        raise FileStoreError("file_id(FileRef identity)가 필요합니다")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise FileStoreError("path가 필요합니다")

    workspace.registry.reload()
    ref = workspace.registry.by_id(file_id.strip())
    if ref is None or ref.missing or ref.unresolved:
        raise FileStoreError("존재하지 않거나 해결되지 않은 FileRef입니다")
    if ref.root_id != root_id.strip():
        raise FileStoreError("FileRef가 지정한 WorkspaceRoot에 속하지 않습니다")
    normalized = relative_path.strip().replace("\\", "/")
    if normalized != ref.relative_path:
        raise FileStoreError("FileRef 경로와 요청 경로가 일치하지 않습니다")
    if _is_sensitive_name(ref.name):
        raise FileStoreError(f"민감 파일은 편집할 수 없습니다: {ref.name!r}")
    return ref.root_id, ref.relative_path


def create_proposal(
    workspace: WorkspaceManager,
    store: EditProposalStore,
    *,
    root_id: Any,
    file_id: Any,
    relative_path: Any,
    after_content: Any,
    summary: str = "",
) -> dict[str, Any]:
    """승인 대기 중인 편집 제안을 만든다. **canonical 파일은 변경하지 않는다.**

    모델은 먼저 read로 현재 내용을 확인한 뒤 전체 새 내용을 넘긴다. 여기서는
    그 content와 "지금 디스크에 있는 것"을 비교해 diff를 만들고, 그 시점의
    revision을 base_revision으로 고정한 제안만 남긴다.
    """
    if not isinstance(after_content, str):
        raise FileStoreError("content는 문자열이어야 합니다")
    # 편집 용량 상한은 제안 시점에 건다. apply까지 미루면 사용자가 diff를 보고
    # 승인한 뒤에야 거절당해 흐름이 어긋난다.
    if len(after_content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise FileStoreError(f"파일이 너무 큽니다 ({MAX_EDIT_BYTES} bytes 제한)")

    resolved_root, resolved_path = _resolve_target(
        workspace, root_id, file_id, relative_path
    )
    # 읽기로 현재 상태 + revision을 확보한다 (파일은 그대로 둔다).
    # read_text는 개행을 \n으로 접으므로, undo 기준점이 되는 원본은 정확히 다시 읽는다.
    current = workspace.read_text(resolved_root, resolved_path)
    _, target_path = workspace.store.resolve_path(resolved_root, resolved_path)
    before_content = _read_exact_text(target_path)

    # 대상 파일의 개행 규약을 지킨다 — CRLF 파일을 LF로 바꾸면 diff 전체가
    # '변경'으로 보이고, undo로도 원본 개행을 복원할 수 없다.
    newline = _detect_newline(before_content)
    normalized_after = _apply_newline(after_content, newline)

    if before_content == normalized_after:
        raise FileStoreError("제안된 내용이 현재 파일과 동일합니다 — 변경할 내용이 없습니다.")

    diff = unified_diff_lines(before_content, normalized_after, resolved_path)
    root_path = workspace.store.roots.get(resolved_root)
    record = {
        "proposal_id": new_proposal_id(),
        "root_id": resolved_root,
        "file_id": file_id.strip(),
        "relative_path": resolved_path,
        "name": current.get("name") or Path(resolved_path).name,
        "absolute_path": (
            str((root_path / resolved_path).resolve())
            if root_path is not None
            else resolved_path
        ),
        "summary": (summary or "").strip(),
        "base_revision": current["revision"],
        "before_content": before_content,
        "after_content": normalized_after,
        "diff": diff,
        "status": "proposed",
        "created_at": time.time(),
        "undo": None,
    }
    store.put(record)
    return public_view(record)


def apply_proposal(
    workspace: WorkspaceManager,
    store: EditProposalStore,
    proposal_id: Any,
) -> dict[str, Any]:
    """승인된 제안을 revision-safe하게 적용한다.

    쓰기 직전에 current revision == base_revision인지 확인한다. 다르면 예외로
    거부한다 — 조용한 덮어쓰기가 절대 일어나지 않는다. 실제 바이트 쓰기는
    기존 WorkspaceManager.update_text가 담당한다(여기서 직접 쓰지 않는다).
    """
    record = store.get(proposal_id)
    if record is None:
        raise FileStoreError("편집 제안을 찾을 수 없습니다")
    if record.get("status") == "applied":
        raise FileStoreError("이미 적용된 제안입니다")
    if record.get("status") != "proposed":
        raise FileStoreError(
            f"적용할 수 없는 제안 상태입니다: {record.get('status')!r}"
        )

    resolved_root, resolved_path = _resolve_target(
        workspace, record.get("root_id"), record.get("file_id"), record.get("relative_path")
    )

    # 방어적 재확인 — 저장된 base_revision과 지금 디스크의 revision을 직접 비교한다.
    # update_text도 같은 검사를 하지만, 거부 사유를 명시적으로 만들려고 먼저 본다.
    current = workspace.read_text(resolved_root, resolved_path)
    base = record.get("base_revision") or {}
    if (
        base.get("size") != current["revision"].get("size")
        or base.get("fingerprint") != current["revision"].get("fingerprint")
    ):
        raise EditConflictError(
            "이 파일이 외부에서 변경되었습니다. 승인된 diff를 그대로 적용할 수 "
            "없습니다 — 다시 계산해 주세요."
        )

    # 실제 쓰기 — revision 검사 + 원자적 교체를 기존 경로가 수행한다.
    saved = workspace.update_text(
        resolved_root,
        str(record["file_id"]).strip(),
        resolved_path,
        str(record.get("after_content") or ""),
        record.get("base_revision"),
    )

    record["status"] = "applied"
    record["revision_after_apply"] = saved.get("revision")
    record["applied_at"] = time.time()
    record["undo"] = {
        "before_content": record.get("before_content"),
        "revision_before_apply": record.get("base_revision"),
        "revision_after_apply": saved.get("revision"),
    }
    # undo는 저장소 내부에서만 읽으므로 before_content는 레코드에 남겨 둔다.
    store.put(record)

    view = public_view(record)
    view["file_id"] = saved.get("file_id") or record.get("file_id")
    view["revision"] = saved.get("revision")
    return view


def cancel_proposal(store: EditProposalStore, proposal_id: Any) -> dict[str, Any]:
    """사용자 취소 — 디스크는 이미 건드리지 않았으므로 상태만 정리한다."""
    record = store.get(proposal_id)
    if record is None:
        raise FileStoreError("편집 제안을 찾을 수 없습니다")
    if record.get("status") == "applied":
        raise FileStoreError(
            "이미 적용된 제안은 취소할 수 없습니다 — 되돌리기를 사용하세요."
        )
    record["status"] = "cancelled"
    store.put(record)
    return public_view(record)


def undo_proposal(
    workspace: WorkspaceManager,
    store: EditProposalStore,
    proposal_id: Any,
) -> dict[str, Any]:
    """1단계 되돌리기.

    현재 revision이 revision_after_apply와 같을 때만 복원한다. 그 뒤 다른 변경이
    들어왔다면 자동 복원하지 않는다 — 사용자가 그 뒤의 작업을 잃을 수 있다.
    되돌리기 자체도 update_text를 쓰므로 revision 검사가 동일하게 적용된다.
    """
    record = store.get(proposal_id)
    if record is None:
        raise FileStoreError("편집 제안을 찾을 수 없습니다")
    if record.get("status") != "applied":
        raise FileStoreError("적용된 제안만 되돌릴 수 있습니다")
    undo = record.get("undo") or {}
    before_content = undo.get("before_content")
    revision_after = undo.get("revision_after_apply")
    if not isinstance(before_content, str) or not isinstance(revision_after, dict):
        raise FileStoreError("되돌리기 기록이 없습니다")

    resolved_root, resolved_path = _resolve_target(
        workspace, record.get("root_id"), record.get("file_id"), record.get("relative_path")
    )
    current = workspace.read_text(resolved_root, resolved_path)
    if (
        revision_after.get("size") != current["revision"].get("size")
        or revision_after.get("fingerprint") != current["revision"].get("fingerprint")
    ):
        raise EditConflictError(
            "적용 이후 이 파일이 다시 변경되었습니다. 자동 복원하지 않습니다."
        )

    restored = workspace.update_text(
        resolved_root,
        str(record["file_id"]).strip(),
        resolved_path,
        before_content,
        revision_after,
    )

    record["status"] = "undone"
    record["undone_at"] = time.time()
    store.put(record)

    view = public_view(record)
    view["file_id"] = restored.get("file_id") or record.get("file_id")
    view["revision"] = restored.get("revision")
    return view


def build_edit_proposal_tool(
    workspace: WorkspaceManager,
    store: EditProposalStore,
) -> Any:
    """모델용 제안 tool — kind="read"가 의도적이다.

    이 tool의 handler는 canonical 파일을 쓰지 않는다. 읽어 diff를 만들고 제안
    레코드만 남긴다. kind="write"로 두면 Permission Gate가 handler 실행 *전에*
    막아 버려 diff가 만들어지지 않으므로, 사용자가 승인 전에 무엇이 바뀌는지
    볼 수 없다 — milestone이 가장 먼저 금지한 그 순서 역전이 된다.

    실제 디스크 쓰기는 renderer가 사용자 클릭으로 호출하는 edit_apply 경로에만
    있고, 그 경로는 모델에게 열려 있지 않다.
    """
    from harness.tools.registry import Tool
    from harness.tools.schemas import edit_file as edit_file_schema

    def preflight(arguments: dict[str, Any]) -> None:
        content = arguments.get("content")
        if content is not None and not isinstance(content, str):
            raise FileStoreError("content는 문자열이어야 합니다")

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        proposal = create_proposal(
            workspace,
            store,
            root_id=arguments.get("root"),
            file_id=arguments.get("file_id"),
            relative_path=arguments.get("path"),
            after_content=arguments.get("content"),
            summary=str(arguments.get("summary") or ""),
        )
        return {
            "ok": True,
            "kind": "edit_proposal",
            "proposal": proposal,
            "note": (
                "Nothing has changed on disk yet. Present the diff to the user and "
                "ask them to approve; the change is applied only after explicit "
                "approval. Do not claim the file was edited."
            ),
        }

    return Tool(
        schema=edit_file_schema(),
        kind="read",
        handler=handler,
        preflight=preflight,
    )
