from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness.agent_runs import (
    AgentRunError,
    AgentRunStore,
    CodexRunner,
    INTERRUPTED_STATUS,
    VERIFICATION_COMMANDS,
    WorktreeError,
    _git_changed_files,
    default_agent_runs_file,
    default_run_log_dir,
    prepare_run_worktree,
    resolve_verification_argv,
    start_verification,
)
from scripts.harness_bridge import (
    DEFAULT_PROJECT,
    _agent_run_cancel,
    _agent_run_list,
    _agent_run_payload,
    _agent_run_start,
    _agent_run_status,
    _agent_run_verify,
    _resolve_repo_path,
    handle_message,
    mark_agent_runs_interrupted,
)
from harness.config import HarnessConfig

"""Agent Delegation V1 — AgentRun/CodexRunner 검증.

실제 Codex를 단위 테스트에서 실행하지 않는다(느리고 인증 의존). JARVIS_CODEX_BIN으로
행동이 동일한 fake binary(python 스크립트)를 주입해 subprocess 수명/취소/종료코드/
worktree 변경 수집을 결정적으로 검증한다. 실제 Codex end-to-end는 별도 스모크에서.
"""


def _write_fake_codex(tmp: Path, mode: str) -> Path:
    """fake codex binary — mode에 따라 성공/실패/장수 실행을 흉내 낸다.

    계약 흉내: stdin에서 instruction을 읽고, `-C`로 받은 작업 루트에 파일을
    만들거나(성공), 비정상 종료하거나(실패), SIGTERM을 기다린다(취소 테스트).
    stdout에 --json 스타일 JSONL을 흉내 내어 로그 캡처도 함께 검증한다.
    """
    script = f'''import sys, time, os, json
mode = {mode!r}
args = sys.argv[1:]
root = args[args.index("-C") + 1] if "-C" in args else "."
sys.stdout.write(json.dumps({{"type": "item.completed", "mode": mode}}))
sys.stdout.flush()
instruction = sys.stdin.read()
if mode == "success":
    with open(os.path.join(root, "AGENT_RUN_SMOKE.txt"), "w", encoding="utf-8") as f:
        f.write("agent run smoke test")
    with open(os.path.join(root, "AGENT_RUN_SMOKE2.txt"), "w", encoding="utf-8") as f:
        f.write("second file")
    with open(os.path.join(root, ".agent-last-message.txt"), "w", encoding="utf-8") as f:
        f.write("Created smoke files as requested. " + instruction.strip()[:40])
    print("EVENT " + instruction.strip()[:20])
    sys.exit(0)
if mode == "fail":
    sys.stderr.write("intentional failure\\n")
    sys.exit(3)
if mode == "sleep":
    print("started", flush=True)
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(0.2)
    sys.exit(0)
'''
    path = tmp / f"fake_codex_{mode}.py"
    path.write_text(script, encoding="utf-8")
    return path


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Path(self.tmp.name)
        self.store = AgentRunStore(default_agent_runs_file(self.memory))

    def test_create_run_defaults(self) -> None:
        run = self.store.create_run(
            agent_type="codex", instruction="do it", repo_path="C:/x"
        )
        self.assertEqual(run["status"], "queued")
        self.assertTrue(run["run_id"].startswith("ar-"))
        self.assertIsNone(run["task_id"])
        self.assertEqual(run["changed_files"], [])

    def test_roundtrip_and_unknown_fields(self) -> None:
        run = self.store.create_run(agent_type="codex", instruction="x", repo_path="C:/x")
        self.store.update(run["run_id"], status="running", pid=123)
        loaded = self.store.get(run["run_id"])
        self.assertEqual(loaded["status"], "running")
        self.assertEqual(loaded["pid"], 123)
        with self.assertRaises(AgentRunError):
            self.store.update(run["run_id"], evil_field="nope")

    def test_mark_interrupted_on_startup(self) -> None:
        running = self.store.create_run(agent_type="codex", instruction="x", repo_path="C:/x")
        self.store.update(running["run_id"], status="running", pid=os.getpid())
        done = self.store.create_run(agent_type="codex", instruction="y", repo_path="C:/x")
        self.store.update(done["run_id"], status="completed", exit_code=0)
        marked = self.store.mark_interrupted_on_startup()
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["run_id"], running["run_id"])
        self.assertEqual(self.store.get(running["run_id"])["status"], INTERRUPTED_STATUS)
        # pid가 살아 있으면(이 테스트 프로세스) 그 사실을 정직하게 덧붙인다
        self.assertIn(str(os.getpid()), self.store.get(running["run_id"])["error"])
        self.assertEqual(self.store.get(done["run_id"])["status"], "completed")


class WorktreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
            cwd=self.repo, check=True,
        )
        # 사용자의 dirty tree — 절대 변하지 않아야 한다
        (self.repo / "dirty.txt").write_text("user edit", encoding="utf-8")

    def test_prepare_and_main_tree_untouched(self) -> None:
        info = prepare_run_worktree(self.repo, "ar-test1")
        wt = Path(info["worktree_path"])
        self.assertTrue((wt / "seed.txt").exists())
        self.assertEqual(info["branch"], "ar-test1")
        # main tree dirty 파일 그대로
        self.assertEqual(
            (self.repo / "dirty.txt").read_text(encoding="utf-8"), "user edit"
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(status.stdout.strip(), "?? dirty.txt")
        # worktree는 exclude에 의해 노이즈가 아니다
        self.assertNotIn(".worktrees/", status.stdout)

    def test_no_commit_repo_reports_blocker(self) -> None:
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=empty, check=True)
        with self.assertRaises(WorktreeError) as ctx:
            prepare_run_worktree(empty, "ar-x")
        self.assertIn("커밋", str(ctx.exception))

    def test_changed_files_collection(self) -> None:
        info = prepare_run_worktree(self.repo, "ar-chg")
        wt = Path(info["worktree_path"])
        (wt / "AGENT_RUN_SMOKE.txt").write_text("x", encoding="utf-8")
        (wt / "new_dir").mkdir()
        (wt / "new_dir" / "nested.txt").write_text("y", encoding="utf-8")
        files = _git_changed_files(wt)
        self.assertIn("AGENT_RUN_SMOKE.txt", files)
        self.assertTrue(
            any(f == "new_dir/nested.txt" or f == "new_dir/" for f in files),
            f"nested file missing from {files}",
        )
        self.assertNotIn("seed.txt", files)


class CodexRunnerTest(unittest.TestCase):
    def _runner(self) -> tuple[CodexRunner, AgentRunStore, Path]:
        tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, tmp)
        memory = Path(tmp) / "memory"
        memory.mkdir()
        store = AgentRunStore(default_agent_runs_file(memory))
        return CodexRunner(store, memory), store, memory

    def _repo(self) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(_rmtree, tmp)
        repo = Path(tmp) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "seed.txt").write_text("seed", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
            cwd=repo, check=True,
        )
        return repo

    def _wait_terminal(self, store: AgentRunStore, run_id: str, timeout: float = 60) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = store.get(run_id)
            if run and run["status"] not in ("queued", "running"):
                return run
            time.sleep(0.2)
        raise AssertionError(f"run이 {timeout}s 안에 끝나지 않았다: {store.get(run_id)}")

    def test_success_run_collects_result(self) -> None:
        runner, store, memory = self._runner()
        fake = _write_fake_codex(Path(tempfile.mkdtemp()), "success")
        os.environ["JARVIS_CODEX_BIN"] = f"{sys.executable} {fake}"
        self.addCleanup(os.environ.pop, "JARVIS_CODEX_BIN", None)
        repo = self._repo()
        run = store.create_run(agent_type="codex", instruction="make smoke files", repo_path=str(repo))
        runner.start(run["run_id"], {"repo_path": str(repo), "instruction": "make smoke files"})
        finished = self._wait_terminal(store, run["run_id"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["exit_code"], 0)
        self.assertIn("AGENT_RUN_SMOKE.txt", finished["changed_files"])
        self.assertIn("AGENT_RUN_SMOKE2.txt", finished["changed_files"])
        # worktree 안에 실제 파일이 존재한다
        wt = Path(finished["worktree_path"])
        self.assertEqual(
            (wt / "AGENT_RUN_SMOKE.txt").read_text(encoding="utf-8"),
            "agent run smoke test",
        )
        # main tree는 깨끗하다
        self.assertFalse((repo / "AGENT_RUN_SMOKE.txt").exists())
        # result_summary는 -o 파일에서 왔다
        self.assertIn("make smoke", finished["result_summary"] or "")
        # 로그가 캡처됐다
        self.assertTrue(Path(finished["log_path"]).exists())
        self.assertIn("EVENT", finished["log_tail"])

    def test_failure_run_records_exit_code(self) -> None:
        runner, store, _memory = self._runner()
        fake = _write_fake_codex(Path(tempfile.mkdtemp()), "fail")
        os.environ["JARVIS_CODEX_BIN"] = f"{sys.executable} {fake}"
        self.addCleanup(os.environ.pop, "JARVIS_CODEX_BIN", None)
        repo = self._repo()
        run = store.create_run(agent_type="codex", instruction="x", repo_path=str(repo))
        runner.start(run["run_id"], {"repo_path": str(repo), "instruction": "x"})
        finished = self._wait_terminal(store, run["run_id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["exit_code"], 3)
        self.assertIn("exit code 3", finished["error"])

    def test_cancel_kills_process_tree(self) -> None:
        runner, store, _memory = self._runner()
        fake = _write_fake_codex(Path(tempfile.mkdtemp()), "sleep")
        os.environ["JARVIS_CODEX_BIN"] = f"{sys.executable} {fake}"
        self.addCleanup(os.environ.pop, "JARVIS_CODEX_BIN", None)
        repo = self._repo()
        run = store.create_run(agent_type="codex", instruction="x", repo_path=str(repo))
        runner.start(run["run_id"], {"repo_path": str(repo), "instruction": "x"})
        deadline = time.time() + 30
        while time.time() < deadline and store.get(run["run_id"])["status"] != "running":
            time.sleep(0.1)
        self.assertEqual(store.get(run["run_id"])["status"], "running")
        pid = store.get(run["run_id"])["pid"]
        runner.cancel(run["run_id"])
        finished = self._wait_terminal(store, run["run_id"])
        self.assertEqual(finished["status"], "cancelled")
        # 프로세스가 실제로 죽었다 (orphan check)
        if os.name == "nt":
            probe = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
            )
            self.assertNotIn(str(pid), probe.stdout.split("INFO:")[0].replace("=", ""))
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertFalse(runner.is_process_alive(run["run_id"]))


class VerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Path(self.tmp.name)
        self.store = AgentRunStore(default_agent_runs_file(self.memory))

    def test_whitelist_rejects_arbitrary_command(self) -> None:
        with self.assertRaises(AgentRunError):
            resolve_verification_argv("rm -rf /")
        with self.assertRaises(AgentRunError):
            resolve_verification_argv("../../../evil")
        self.assertIn("verify:fast", VERIFICATION_COMMANDS)
        argv = resolve_verification_argv("verify:fast")
        self.assertEqual(argv[-2:], ["run", "verify:fast"])
        # binary는 해석된 실제 경로여야 한다 (shim 포함)
        self.assertTrue(Path(argv[0]).is_file())

    def test_verify_runs_and_stores_result(self) -> None:
        # Electron repo 자체를 대상으로 하면 실제 verify:fast가 돈다 — 너무 무겁다.
        # 대신 같은 npm 계약을 지키는 scratch repo를 만든다(npm run smoke-ok).
        scratch = Path(self.tmp.name) / "scratch"
        (scratch / "node_modules" / ".bin").mkdir(parents=True)
        (scratch / "package.json").write_text(
            json.dumps({
                "name": "scratch", "version": "1.0.0",
                "scripts": {"verify:fast": "node -e \"process.exit(0)\""},
            }),
            encoding="utf-8",
        )
        run = self.store.create_run(agent_type="codex", instruction="x", repo_path=str(scratch))
        self.store.update(
            run["run_id"], status="completed",
            worktree_path=str(scratch),
        )
        started = start_verification(self.store, run["run_id"], "verify:fast")
        self.assertEqual(started["verification"]["status"], "running")
        deadline = time.time() + 60
        while time.time() < deadline:
            verification = self.store.get(run["run_id"])["verification"]
            if verification["status"] != "running":
                break
            time.sleep(0.2)
        verification = self.store.get(run["run_id"])["verification"]
        self.assertEqual(verification["command"], "verify:fast")
        self.assertEqual(verification["status"], "completed")
        self.assertEqual(verification["exit_code"], 0)
        self.assertGreater(verification["duration_ms"], 0)

    def test_verify_requires_completed_run(self) -> None:
        run = self.store.create_run(agent_type="codex", instruction="x", repo_path="C:/x")
        with self.assertRaises(AgentRunError):
            start_verification(self.store, run["run_id"], "verify:fast")

    def test_verify_failed_exit_code_stored(self) -> None:
        scratch = Path(self.tmp.name) / "scratch"
        scratch.mkdir()
        (scratch / "package.json").write_text(
            json.dumps({
                "name": "scratch", "version": "1.0.0",
                "scripts": {"verify:fast": "node -e \"process.exit(7)\""},
            }),
            encoding="utf-8",
        )
        run = self.store.create_run(agent_type="codex", instruction="x", repo_path=str(scratch))
        self.store.update(run["run_id"], status="completed", worktree_path=str(scratch))
        start_verification(self.store, run["run_id"], "verify:fast")
        deadline = time.time() + 60
        while time.time() < deadline:
            verification = self.store.get(run["run_id"])["verification"]
            if verification["status"] != "running":
                break
            time.sleep(0.2)
        verification = self.store.get(run["run_id"])["verification"]
        self.assertEqual(verification["status"], "failed")
        self.assertEqual(verification["exit_code"], 7)


class _FakeAgentClient:
    """handle_message 경로 테스트용 최소 client — config만 필요하다."""

    def __init__(self, memory_dir: Path) -> None:
        self.config = HarnessConfig(runtime="mock", memory_dir=memory_dir, trace_dir=memory_dir / "traces")
        self.tools = type("R", (), {"get": staticmethod(lambda name: None)})()


class BridgeOpsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Path(self.tmp.name) / "memory"
        self.memory.mkdir()
        # projects.md + tasks.md — task 검증 경로용
        (self.memory / "projects.md").write_text(
            "# Projects\n\n## Active\n\n- id: jarvis-app — title: JARVIS App\n",
            encoding="utf-8",
        )
        (self.memory / "tasks.md").write_text(
            "# Tasks\n\n## jarvis-app\n\n- [ ] t-abc123def456: smoke task — reason\n",
            encoding="utf-8",
        )
        # 승인 root 등록
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
            cwd=self.repo, check=True,
        )
        self.client = _FakeAgentClient(self.memory)
        self.client.config.file_roots = {"scratch": str(self.repo)}

    def test_repo_path_boundary_enforced(self) -> None:
        resolved = _resolve_repo_path(self.client, str(self.repo / "sub"))
        self.assertEqual(Path(resolved), (self.repo / "sub").resolve())
        with self.assertRaises(AgentRunError):
            _resolve_repo_path(self.client, "C:/Windows")
        with self.assertRaises(AgentRunError):
            _resolve_repo_path(self.client, str(self.memory / ".." / "elsewhere"))

    def test_start_rejects_unknown_task_and_bad_args(self) -> None:
        response = _agent_run_start(self.client, 1, {
            "agent_type": "codex", "instruction": "x",
            "task_id": "t-doesnotexist", "repo_path": str(self.repo),
        })
        self.assertEqual(response["status"], "error")
        self.assertIn("t-doesnotexist", response["error"])
        response = _agent_run_start(self.client, 2, {
            "agent_type": "codex", "instruction": "", "repo_path": str(self.repo),
        })
        self.assertEqual(response["status"], "error")

    def test_full_lifecycle_over_bridge_ops(self) -> None:
        fake = _write_fake_codex(Path(self.tmp.name), "success")
        os.environ["JARVIS_CODEX_BIN"] = f"{sys.executable} {fake}"
        self.addCleanup(os.environ.pop, "JARVIS_CODEX_BIN", None)
        # --json 스타일 로그 흉내를 위해 fake가 stdout에 이벤트를 쓴다 — 이미 쓴다.
        started = _agent_run_start(self.client, 3, {
            "agent_type": "codex",
            "instruction": "create smoke file",
            "task_id": "t-abc123def456",
            "project_id": "jarvis-app",
            "repo_path": str(self.repo),
        })
        self.assertEqual(started["status"], "ok", started)
        run = started["run"]
        self.assertEqual(run["task_id"], "t-abc123def456")
        self.assertEqual(run["agent_type"], "codex")
        self.assertIn(run["status"], ("running", "queued"))
        # payload에서 instruction 전문은 200자로 제한된다
        self.assertLessEqual(len(run["instruction"]), 200)

        status = _agent_run_status(self.client, 4, {"run_id": run["run_id"]})
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["run"]["run_id"], run["run_id"])

        listing = _agent_run_list(self.client, 5, {"task_id": "t-abc123def456"})
        self.assertEqual(listing["status"], "ok")
        self.assertEqual(len(listing["runs"]), 1)

        # 완료 대기
        store = AgentRunStore(default_agent_runs_file(self.memory))
        deadline = time.time() + 60
        while time.time() < deadline:
            current = store.get(run["run_id"])
            if current["status"] not in ("queued", "running"):
                break
            time.sleep(0.2)
        current = store.get(run["run_id"])
        self.assertEqual(current["status"], "completed", current)
        self.assertIn("AGENT_RUN_SMOKE.txt", current["changed_files"])

    def test_unknown_agent_type_rejected(self) -> None:
        response = _agent_run_start(self.client, 6, {
            "agent_type": "freebuff-runner", "instruction": "x", "repo_path": str(self.repo),
        })
        self.assertEqual(response["status"], "error")
        self.assertIn("agent_type", response["error"])

    def test_verify_bridge_op(self) -> None:
        # completed run에 대해 verify op가 화이트리스트 밖 명령을 거부하는지
        store = AgentRunStore(default_agent_runs_file(self.memory))
        run = store.create_run(agent_type="codex", instruction="x", repo_path=str(self.repo))
        store.update(run["run_id"], status="completed", worktree_path=str(self.repo))
        response = _agent_run_verify(self.client, 7, {
            "run_id": run["run_id"], "command": "arbitrary-shell",
        })
        self.assertEqual(response["status"], "error")
        self.assertIn("허용되지 않은", response["error"])
        response = _agent_run_verify(self.client, 8, {
            "run_id": run["run_id"], "command": "verify:fast",
        })
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["run"]["verification"]["command"], "verify:fast")
        # worker thread가 tmpdir cleanup과 경합하지 않게 종료를 기다린다
        # (Windows: npm 자식이 verify.log를 붙잡는 동안 rmtree가 실패한다)
        deadline = time.time() + 60
        while time.time() < deadline:
            verification = store.get(run["run_id"]).get("verification") or {}
            if verification.get("status") != "running":
                break
            time.sleep(0.2)

    def test_handle_message_dispatch(self) -> None:
        response = handle_message(self.client, type("S", (), {})(), {
            "type": "agent_run_list", "id": 9,
        })
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["runs"], [])

    def test_mark_interrupted_bridge_helper(self) -> None:
        store = AgentRunStore(default_agent_runs_file(self.memory))
        run = store.create_run(agent_type="codex", instruction="x", repo_path=str(self.repo))
        store.update(run["run_id"], status="running", pid=os.getpid())
        marked = mark_agent_runs_interrupted(self.client)
        self.assertEqual(len(marked), 1)
        self.assertEqual(store.get(run["run_id"])["status"], INTERRUPTED_STATUS)

    def test_payload_hides_full_instruction(self) -> None:
        run = {
            "run_id": "ar-x", "task_id": None, "agent_type": "codex", "status": "queued",
            "instruction": "A" * 500, "repo_path": "C:/x", "worktree_path": None,
            "branch": None, "started_at": None, "finished_at": None, "exit_code": None,
            "result_summary": None, "error": None, "changed_files": [],
            "verification": None, "pid": None, "log_path": None, "timeout_s": 1800,
            "created_at": "2026-09-28T00:00:00+00:00",
        }
        payload = _agent_run_payload(run)
        self.assertEqual(len(payload["instruction"]), 200)
        self.assertEqual(payload["run_id"], "ar-x")


def _rmtree(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
