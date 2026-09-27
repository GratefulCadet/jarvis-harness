"""AI Edit V1 — 제안/적용/되돌리기 테스트.

검증 대상(acceptance):
- happy path: 제안 → 적용 → 디스크 반영 → 되돌리기 → 원본 복원
- conflict: 제안 이후 외부 변경 → 적용 거부(silent overwrite 없음)
- cancel: 승인 전 디스크 불변
- wrong target: FileRef/경로 불일치 거부 (다른 파일을 추측 수정하지 않음)
- safety: 루트 밖·민감·과대 용량 정책 유지
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from harness.tools.edit_proposals import (
    EditConflictError,
    EditProposalStore,
    apply_proposal,
    cancel_proposal,
    create_proposal,
    default_edit_proposals_file,
    diff_stats,
    mark_terminal,
    recalculate_source,
    undo_proposal,
    unified_diff_lines,
)
from harness.tools.file_store import FileStore, FileStoreError
from harness.tools.workspace import WorkspaceManager, default_registry_file


class EditProposalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.memory = self.tmp / "memory"
        self.memory.mkdir()
        (self.root / "experiment-notes.md").write_text(
            "# Notes\n\nold line\nkeep me\n", encoding="utf-8"
        )
        (self.root / "other.md").write_text("other\n", encoding="utf-8")
        (self.root / "image.bin").write_bytes(b"\x00\x01\x02")
        self.workspace = WorkspaceManager(
            FileStore({"scratch": str(self.root)}),
            default_registry_file(self.memory),
        )
        self.workspace.scan_root("scratch")
        self.store = EditProposalStore(default_edit_proposals_file(self.memory))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers --
    def opened(self, relative: str = "experiment-notes.md") -> dict:
        result = self.workspace.read_text("scratch", relative)
        return {
            "file_id": result["id"],
            "root_id": result["root_id"],
            "path": result["path"],
            "content": result["content"],
            "revision": result["revision"],
        }

    def propose(self, new_content: str, relative: str = "experiment-notes.md") -> dict:
        ref = self.opened(relative)
        return create_proposal(
            self.workspace,
            self.store,
            root_id=ref["root_id"],
            file_id=ref["file_id"],
            relative_path=ref["path"],
            after_content=new_content,
            summary="test edit",
        )

    # ---------- diff ----------
    def test_unified_diff_reports_removals_and_additions(self) -> None:
        diff = unified_diff_lines("a\nb\nc\n", "a\nB\nc\nd\n", "x.md")
        stats = diff_stats(diff)
        self.assertEqual(stats["added"], 2)
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(stats["changed"], 3)
        self.assertTrue(any(line.startswith("-b") for line in diff))
        self.assertTrue(any(line.startswith("+B") for line in diff))

    # ---------- happy path ----------
    def test_propose_does_not_touch_disk(self) -> None:
        before = self.opened()["content"]
        proposal = self.propose("# Notes\n\nnew line\nkeep me\n")
        self.assertEqual(proposal["status"], "proposed")
        # 승인 전에는 canonical 파일이 그대로여야 한다.
        self.assertEqual(self.opened()["content"], before)

    def test_apply_writes_and_undo_restores(self) -> None:
        original = self.opened()["content"]
        new_content = "# Notes\n\nnew line\nkeep me\n"
        proposal = self.propose(new_content)

        applied = apply_proposal(self.workspace, self.store, proposal["proposal_id"])
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(self.opened()["content"], new_content)

        undone = undo_proposal(self.workspace, self.store, proposal["proposal_id"])
        self.assertEqual(undone["status"], "undone")
        self.assertEqual(self.opened()["content"], original)

    def test_apply_is_idempotently_guarded(self) -> None:
        proposal = self.propose("changed\n")
        apply_proposal(self.workspace, self.store, proposal["proposal_id"])
        with self.assertRaises(FileStoreError):
            apply_proposal(self.workspace, self.store, proposal["proposal_id"])

    # ---------- conflict ----------
    def test_conflict_rejects_silent_overwrite(self) -> None:
        proposal = self.propose("model version\n")
        # 제안 이후 외부에서 파일이 바뀐다.
        target = self.root / "experiment-notes.md"
        target.write_text("external edit\n", encoding="utf-8")

        with self.assertRaises(EditConflictError):
            apply_proposal(self.workspace, self.store, proposal["proposal_id"])
        # 핵심: 외부 변경 내용이 살아 있어야 한다.
        self.assertEqual(target.read_text(encoding="utf-8"), "external edit\n")

    def test_undo_refuses_after_later_change(self) -> None:
        proposal = self.propose("applied version\n")
        apply_proposal(self.workspace, self.store, proposal["proposal_id"])
        # 적용 이후 사용자가 다시 편집한다.
        (self.root / "experiment-notes.md").write_text("later human edit\n", encoding="utf-8")

        with self.assertRaises(EditConflictError):
            undo_proposal(self.workspace, self.store, proposal["proposal_id"])
        self.assertEqual(
            (self.root / "experiment-notes.md").read_text(encoding="utf-8"),
            "later human edit\n",
        )

    # ---------- cancel ----------
    def test_cancel_leaves_disk_unchanged(self) -> None:
        original = self.opened()["content"]
        proposal = self.propose("never written\n")
        cancelled = cancel_proposal(self.store, proposal["proposal_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.opened()["content"], original)
        with self.assertRaises(FileStoreError):
            apply_proposal(self.workspace, self.store, proposal["proposal_id"])

    # ---------- wrong target protection ----------
    def test_mismatched_path_is_rejected(self) -> None:
        ref = self.opened("experiment-notes.md")
        with self.assertRaises(FileStoreError):
            create_proposal(
                self.workspace,
                self.store,
                root_id=ref["root_id"],
                file_id=ref["file_id"],
                relative_path="other.md",  # 다른 파일로의 추측 시도
                after_content="hijacked\n",
            )
        self.assertEqual(
            (self.root / "other.md").read_text(encoding="utf-8"), "other\n"
        )

    def test_forged_file_id_is_rejected(self) -> None:
        with self.assertRaises(FileStoreError):
            create_proposal(
                self.workspace,
                self.store,
                root_id="scratch",
                file_id="f-forged",
                relative_path="experiment-notes.md",
                after_content="hijacked\n",
            )

    def test_explicit_target_differs_from_active_file(self) -> None:
        """Active File=A 이어도 명시적 target=B면 B만 수정된다."""
        proposal = self.propose("edited B\n", relative="other.md")
        self.assertEqual(proposal["relative_path"], "other.md")
        apply_proposal(self.workspace, self.store, proposal["proposal_id"])
        self.assertEqual((self.root / "other.md").read_text(encoding="utf-8"), "edited B\n")
        # A는 건드리지 않았다.
        self.assertTrue(self.opened("experiment-notes.md")["content"].startswith("# Notes"))

    # ---------- safety ----------
    def test_binary_file_cannot_be_proposed(self) -> None:
        with self.assertRaises(FileStoreError):
            self.propose("x", relative="image.bin")

    def test_oversized_content_rejected(self) -> None:
        with self.assertRaises(FileStoreError):
            self.propose("x" * (1024 * 1024 + 1))

    def test_sensitive_file_rejected(self) -> None:
        (self.root / "id_rsa").write_text("secret\n", encoding="utf-8")
        self.workspace.scan_root("scratch")
        with self.assertRaises(FileStoreError):
            self.propose("x", relative="id_rsa")

    def test_no_op_edit_rejected(self) -> None:
        with self.assertRaises(FileStoreError):
            self.propose(self.opened()["content"])

    # ---------- newline fidelity ----------
    def test_crlf_file_round_trips_exactly(self) -> None:
        """CRLF 파일은 apply/undo를 거쳐도 바이트가 그대로여야 한다.

        read_text는 유니버설 개행으로 \n을 돌려준다. 그 값을 그대로 저장하면
        undo가 줄 끝을 LF로 정규화해 원본 바이트를 잃는다 — 조용한 데이터 손실.
        """
        target = self.root / "experiment-notes.md"
        target.write_bytes(b"# Notes\r\n\r\nkeep\r\n")
        original_bytes = target.read_bytes()

        proposal = self.propose("# Notes\r\n\r\nnew\r\nkeep\r\n")
        apply_proposal(self.workspace, self.store, proposal["proposal_id"])
        applied_bytes = target.read_bytes()
        self.assertEqual(applied_bytes, b"# Notes\r\n\r\nnew\r\nkeep\r\n")

        undo_proposal(self.workspace, self.store, proposal["proposal_id"])
        self.assertEqual(target.read_bytes(), original_bytes)

    def test_model_lf_content_maps_to_file_newline(self) -> None:
        """모델이 LF로 줘도 CRLF 파일은 CRLF로 유지된다."""
        target = self.root / "experiment-notes.md"
        target.write_bytes(b"# Notes\r\nkeep\r\n")
        proposal = self.propose("# Notes\nchanged\nkeep\n")
        apply_proposal(self.workspace, self.store, proposal["proposal_id"])
        self.assertEqual(target.read_bytes(), b"# Notes\r\nchanged\r\nkeep\r\n")

    # ---------- recalculate (V1.1) ----------
    def test_instruction_is_persisted_and_surfaced(self) -> None:
        proposal = create_proposal(
            self.workspace, self.store,
            root_id="scratch", file_id=self.opened()["file_id"],
            relative_path="experiment-notes.md",
            after_content="new\n", instruction="결론을 더 짧게 수정해줘",
        )
        self.assertEqual(proposal["instruction"], "결론을 더 짧게 수정해줘")
        self.assertEqual(
            self.store.get(proposal["proposal_id"])["instruction"],
            "결론을 더 짧게 수정해줘",
        )

    def test_recalculate_source_uses_latest_revision(self) -> None:
        proposal = create_proposal(
            self.workspace, self.store,
            root_id="scratch", file_id=self.opened()["file_id"],
            relative_path="experiment-notes.md",
            after_content="new\n", instruction="더 짧게",
        )
        # 제안 이후 파일이 바뀌었다.
        (self.root / "experiment-notes.md").write_text("human edit\n", encoding="utf-8")
        source = recalculate_source(self.workspace, self.store, proposal["proposal_id"])
        self.assertEqual(source["instruction"], "더 짧게")
        # 온디스크 바이트 그대로 읽으므로 개행도 원본 그대로다(Windows = CRLF).
        self.assertEqual(
            source["latest_content"],
            (self.root / "experiment-notes.md").read_bytes().decode("utf-8"),
        )
        self.assertIn("human edit", source["latest_content"])
        # 반드시 현재 revision이어야 한다(예전 base_revision이 아니다).
        current = self.workspace.read_text("scratch", "experiment-notes.md")
        self.assertEqual(source["latest_revision"], current["revision"])
        self.assertNotEqual(source["latest_revision"], proposal["base_revision"])

    def test_recalculate_without_instruction_fails(self) -> None:
        proposal = self.propose("new\n")  # summary만 있고 instruction 없음
        with self.assertRaises(FileStoreError):
            recalculate_source(self.workspace, self.store, proposal["proposal_id"])

    def test_recalculate_rejects_deleted_file(self) -> None:
        proposal = create_proposal(
            self.workspace, self.store,
            root_id="scratch", file_id=self.opened()["file_id"],
            relative_path="experiment-notes.md",
            after_content="new\n", instruction="더 짧게",
        )
        (self.root / "experiment-notes.md").unlink()
        self.workspace.scan_root("scratch")
        with self.assertRaises(FileStoreError):
            recalculate_source(self.workspace, self.store, proposal["proposal_id"])

    def test_recalculate_rejects_moved_file(self) -> None:
        proposal = create_proposal(
            self.workspace, self.store,
            root_id="scratch", file_id=self.opened()["file_id"],
            relative_path="experiment-notes.md",
            after_content="new\n", instruction="더 짧게",
        )
        (self.root / "experiment-notes.md").rename(self.root / "moved.md")
        self.workspace.scan_root("scratch")
        with self.assertRaises(FileStoreError):
            recalculate_source(self.workspace, self.store, proposal["proposal_id"])

    def test_mark_terminal_moves_proposal_out_of_play(self) -> None:
        proposal = self.propose("new\n")
        mark_terminal(self.store, proposal["proposal_id"], "superseded")
        self.assertEqual(self.store.get(proposal["proposal_id"])["status"], "superseded")
        with self.assertRaises(FileStoreError):
            apply_proposal(self.workspace, self.store, proposal["proposal_id"])

    def test_mark_terminal_rejects_unknown_status(self) -> None:
        proposal = self.propose("new\n")
        with self.assertRaises(FileStoreError):
            mark_terminal(self.store, proposal["proposal_id"], "whatever")

    def test_two_store_instances_do_not_clobber_each_other(self) -> None:
        """서로 다른 인스턴스가 순차적으로 써도 기록이 사라지지 않는다.

        bridge는 tool registry용 저장소와 op 요청마다의 저장소를 따로 만든다.
        오래 살아 있는 인스턴스가 자기 스냅샷으로 덮어쓰면, 그 사이에 만들어진
        새 제안이 조용히 사라진다(재계산 직후 새 제안이 증발하던 버그).
        """
        first = EditProposalStore(default_edit_proposals_file(self.memory))
        p1 = self.propose("first\n")  # goes through `self.store` (a third instance)

        # 오래 살아 있는 인스턴스가 먼저 읽고(스냅샷 획득) 새 제안을 만든다.
        stale_reader = EditProposalStore(default_edit_proposals_file(self.memory))
        stale_reader.get(p1["proposal_id"])

        p2 = create_proposal(
            self.workspace, self.store,
            root_id="scratch", file_id=self.opened()["file_id"],
            relative_path="experiment-notes.md", after_content="second\n",
        )
        # stale_reader가 이전 스냅샷으로 P1만 갱신한다.
        mark_terminal(first, p1["proposal_id"], "superseded")

        final = EditProposalStore(default_edit_proposals_file(self.memory))
        self.assertEqual(final.get(p1["proposal_id"])["status"], "superseded")
        self.assertIsNotNone(
            final.get(p2["proposal_id"]),
            "새 제안이 이전 인스턴스에 의해 덮어써짐",
        )

    # ---------- persistence ----------
    def test_proposal_survives_store_reload(self) -> None:
        proposal = self.propose("persisted\n")
        reloaded = EditProposalStore(default_edit_proposals_file(self.memory))
        found = reloaded.get(proposal["proposal_id"])
        self.assertIsNotNone(found)
        self.assertEqual(found["status"], "proposed")
        # 새 store 인스턴스로도 base_revision이 살아 있어 apply가 가능하다.
        applied = apply_proposal(self.workspace, reloaded, proposal["proposal_id"])
        self.assertEqual(applied["status"], "applied")

    def test_public_view_omits_file_contents(self) -> None:
        proposal = self.propose("secret new content\n")
        self.assertNotIn("after_content", proposal)
        self.assertNotIn("before_content", proposal)
        self.assertTrue(proposal["diff"])


if __name__ == "__main__":
    unittest.main()
