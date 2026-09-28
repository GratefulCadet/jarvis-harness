from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from harness.agent_runs import AgentRunStore
from harness.agent_sessions import (
    AGENT_TYPES,
    AgentSessionError,
    AgentSessionStore,
    default_agent_sessions_file,
)
from harness.freebuff_client import THREAD_ID_RE, FreebuffCapability, validate_thread
from harness.config import HarnessConfig
from scripts.harness_bridge import (
    DEFAULT_PROJECT,
    _agent_run_start,
    _agent_run_status,
    _agent_session_archive,
    _agent_session_create,
    _agent_session_get,
    _agent_session_list,
    _agent_session_update,
    _freebuff_session_validate,
    handle_message,
    mark_agent_runs_interrupted,
)

"""AgentSession V1 — Freebuff thread binding + Agent selector 도메인 검증.

실제 Freebuff orchestrator를 요구하지 않는다: 로컬 HTTPServer로 orchestrator의
확인된 최소 계약(healthz / project/open / thread GET)을 흉내 내어 read-only
validation 경로를 결정적으로 검증한다(§9/§10/§21-G). 사용자의 Freebuff state에는
절대 접촉하지 않는다(§20).
"""


def _write_fake_codex(tmp: Path) -> Path:
    """세션→run 통합 테스트용 fake codex (test_agent_runs와 동일 계약)."""
    script = '''import sys, os, json
args = sys.argv[1:]
root = args[args.index("-C") + 1] if "-C" in args else "."
sys.stdin.read()
with open(os.path.join(root, "SESSION_SMOKE.txt"), "w", encoding="utf-8") as f:
    f.write("session run")
with open(os.path.join(root, ".agent-last-message.txt"), "w", encoding="utf-8") as f:
    f.write("done")
sys.exit(0)
'''
    path = tmp / "fake_codex.py"
    path.write_text(script, encoding="utf-8")
    return path


class _OrchHandler(BaseHTTPRequestHandler):
    """audit probe가 확정한 orchestrator 최소 계약의 흉내.

    /healthz → 200
    /api/project/open (POST {path}) → 200 (등록된 workspace만)
    /api/thread/:id → 200 thread snapshot | 404
    """

    known_threads: dict[str, dict] = {}
    open_projects: set[str] = set()

    def log_message(self, *args) -> None:  # 테스트 출력 정리
        pass

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if self.path.startswith("/api/thread/"):
            tid = self.path.rsplit("/", 1)[-1]
            if _OrchHandler.open_projects and any(
                p in _OrchHandler.open_projects
                for p in (_OrchHandler.known_threads.get(tid, {}).get("__projects") or [])
            ):
                thread = _OrchHandler.known_threads.get(tid)
                if thread:
                    self._json(200, {
                        "thread": {
                            "id": tid,
                            "title": "Scratch",
                            "model": thread["model"],
                            "harnessId": "codebuff",
                            "reasoningEffort": thread["reasoningEffort"],
                            "status": "open",
                        },
                        "messages": [{"role": "user"}, {"role": "assistant"}],
                    })
                    return
            self._json(404, {"error": "thread not found"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/project/open":
            path = str(body.get("path") or "")
            if path in _OPENABLE_WORKSPACES:
                _OrchHandler.open_projects.add(path)
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "unknown project"})
            return
        self._json(404, {"error": "not found"})


_OPENABLE_WORKSPACES: set[str] = set()


def _start_orch() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _OrchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _fake_client(memory: Path) -> object:
    class _FakeAgentClient:
        def __init__(self) -> None:
            self.config = HarnessConfig(
                runtime="mock", memory_dir=memory, trace_dir=memory / "traces"
            )
            self.tools = type("R", (), {"get": staticmethod(lambda name: None)})()

    return _FakeAgentClient()


class SessionStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Path(self.tmp.name)
        self.store = AgentSessionStore(default_agent_sessions_file(self.memory))

    def test_create_defaults_and_schema(self) -> None:
        session = self.store.create_session(
            agent_type="freebuff",
            task_id="t-abc123def456",
            project_id="p1",
            purpose="Agent orchestration",
            workspace="C:/ws",
            external_session_id=str(uuid.uuid4()),
            model="z-ai/glm-5.3-flash",
            reasoning_effort="high",
        )
        self.assertTrue(session["session_id"].startswith("as-"))
        self.assertEqual(session["status"], "active")
        self.assertEqual(session["run_count"], 0)
        self.assertIsNone(session["last_run_id"])
        # vendor-specific 이름이 domain schema에 박히지 않았다(§3)
        self.assertNotIn("freebuff_conversation_id", session)
        for key in (
            "session_id", "agent_type", "project_id", "task_id", "purpose",
            "workspace_root_id", "workspace", "external_session_id", "model",
            "reasoning_effort", "status", "created_at", "updated_at",
            "last_run_id", "run_count",
        ):
            self.assertIn(key, session)

    def test_invalid_agent_type_rejected(self) -> None:
        with self.assertRaises(AgentSessionError):
            self.store.create_session(agent_type="claude")

    def test_freebuff_requires_external_session_id(self) -> None:
        with self.assertRaises(AgentSessionError):
            self.store.create_session(agent_type="freebuff")

    def test_codex_allows_null_external_session_id(self) -> None:
        session = self.store.create_session(agent_type="codex", purpose="Code Review")
        self.assertIsNone(session["external_session_id"])

    def test_persistence_roundtrip(self) -> None:
        session = self.store.create_session(agent_type="freebuff", external_session_id=str(uuid.uuid4()))
        again = AgentSessionStore(default_agent_sessions_file(self.memory))
        self.assertEqual(again.get(session["session_id"])["purpose"], session["purpose"])

    def test_corrupted_state_recovered(self) -> None:
        self.store.create_session(agent_type="codex")
        path = default_agent_sessions_file(self.memory)
        path.write_text("{ not json !!", encoding="utf-8")
        recovered = AgentSessionStore(default_agent_sessions_file(self.memory))
        self.assertEqual(recovered.list_sessions(), [])

    def test_list_filters_by_agent_and_task(self) -> None:
        fb = self.store.create_session(agent_type="freebuff", task_id="t-aaa", external_session_id=str(uuid.uuid4()))
        cx = self.store.create_session(agent_type="codex", task_id="t-bbb")
        self.assertEqual([s["session_id"] for s in self.store.list_sessions(agent_type="freebuff")], [fb["session_id"]])
        self.assertEqual([s["session_id"] for s in self.store.list_sessions(agent_type="codex")], [cx["session_id"]])
        self.assertEqual([s["session_id"] for s in self.store.list_sessions(task_id="t-aaa")], [fb["session_id"]])

    def test_archive_hides_from_default_list(self) -> None:
        session = self.store.create_session(agent_type="codex")
        self.store.archive(session["session_id"])
        self.assertEqual(self.store.list_sessions(), [])
        archived = self.store.list_sessions(include_archived=True)
        self.assertEqual(archived[0]["status"], "archived")

    def test_update_whitelist(self) -> None:
        session = self.store.create_session(agent_type="codex")
        with self.assertRaises(AgentSessionError):
            self.store.update(session["session_id"], session_id="evil")
        with self.assertRaises(AgentSessionError):
            self.store.update(session["session_id"], status="exploded")
        updated = self.store.update(session["session_id"], purpose="New purpose", status="broken")
        self.assertEqual(updated["purpose"], "New purpose")
        self.assertEqual(updated["status"], "broken")

    def test_record_run_link(self) -> None:
        session = self.store.create_session(agent_type="codex")
        self.store.record_run_link(session["session_id"], "ar-1")
        again = self.store.record_run_link(session["session_id"], "ar-2")
        self.assertEqual(again["run_count"], 2)
        self.assertEqual(again["last_run_id"], "ar-2")


class FreebuffClientTest(unittest.TestCase):
    """§9/§10 — orchestrator 계약 흉내로 validate_thread를 결정적으로 검증."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _start_orch()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        _OrchHandler.known_threads.clear()
        _OrchHandler.open_projects.clear()
        _OPENABLE_WORKSPACES.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = str(Path(self.tmp.name).resolve())
        _OPENABLE_WORKSPACES.add(self.workspace)
        self.old_port = os.environ.get("JARVIS_FREEBUFF_PORT")
        os.environ["JARVIS_FREEBUFF_PORT"] = str(self.port)
        os.environ.pop("JARVIS_FREEBUFF_LAUNCH_ID", None)

    def tearDown(self) -> None:
        if self.old_port is None:
            os.environ.pop("JARVIS_FREEBUFF_PORT", None)
        else:
            os.environ["JARVIS_FREEBUFF_PORT"] = self.old_port

    def test_thread_id_format(self) -> None:
        self.assertRegex(str(uuid.uuid4()), THREAD_ID_RE)
        self.assertFalse(THREAD_ID_RE.match("not-a-uuid"))

    def test_validate_available_reads_authoritative_metadata(self) -> None:
        tid = str(uuid.uuid4())
        _OrchHandler.known_threads[tid] = {"model": "z-ai/glm-5.3-flash", "reasoningEffort": "high", "__projects": [self.workspace]}
        cap = validate_thread(self.workspace, tid)
        self.assertEqual(cap.status, "available")
        self.assertEqual(cap.thread["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(cap.thread["reasoningEffort"], "high")
        self.assertEqual(cap.messages_count, 2)

    def test_validate_thread_missing(self) -> None:
        tid = str(uuid.uuid4())
        cap = validate_thread(self.workspace, tid)
        self.assertEqual(cap.status, "thread_missing", cap.detail)

    def test_validate_unavailable_without_orchestrator(self) -> None:
        os.environ["JARVIS_FREEBUFF_PORT"] = "1"  # 아무도 안 듣는 포트
        cap = validate_thread(self.workspace, str(uuid.uuid4()))
        self.assertEqual(cap.status, "unavailable")

    def test_validate_requires_project_open_before_thread_get(self) -> None:
        # project/open 없이는 404 → open 후 200 (audit probe5 계약)
        tid = str(uuid.uuid4())
        _OrchHandler.known_threads[tid] = {"model": "m", "reasoningEffort": "high", "__projects": [self.workspace]}
        _OrchHandler.open_projects.clear()
        cap = validate_thread(self.workspace, tid)
        self.assertEqual(cap.status, "available")

    def test_capability_dict_shape(self) -> None:
        cap = FreebuffCapability(status="unavailable", detail="x", detected_version="0.0.151")
        as_dict = cap.as_dict()
        for key in ("status", "detail", "detected_version", "verified_version", "thread", "messages_count"):
            self.assertIn(key, as_dict)


class SessionBridgeOpsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Path(self.tmp.name) / "memory"
        self.memory.mkdir()
        (self.memory / "projects.md").write_text(
            "# Projects\n\n## Active\n\n- id: jarvis-app — title: JARVIS App\n",
            encoding="utf-8",
        )
        (self.memory / "tasks.md").write_text(
            "# Tasks\n\n## jarvis-app\n\n- [ ] t-abc123def456: smoke task — reason\n",
            encoding="utf-8",
        )
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
            cwd=self.repo, check=True,
        )
        self.client = _fake_client(self.memory)
        self.client.config.file_roots = {"scratch": str(self.repo)}

    # ---- create ------------------------------------------------------------

    def test_create_freebuff_requires_thread_uuid(self) -> None:
        resp = _agent_session_create(self.client, 1, {"agent_type": "freebuff", "task_id": "t-abc123def456"})
        self.assertEqual(resp["status"], "error")
        self.assertIn("external_session_id", resp["error"])

    def test_create_codex_session(self) -> None:
        resp = _agent_session_create(self.client, 1, {
            "agent_type": "codex", "task_id": "t-abc123def456",
            "purpose": "Code Review", "workspace": str(self.repo),
        })
        self.assertEqual(resp["status"], "ok")
        session = resp["session"]
        self.assertEqual(session["agent_type"], "codex")
        self.assertIsNone(session["external_session_id"])
        self.assertEqual(session["task_id"], "t-abc123def456")

    def test_create_rejects_unknown_task(self) -> None:
        resp = _agent_session_create(self.client, 1, {"agent_type": "codex", "task_id": "t-doesnotexist0"})
        self.assertEqual(resp["status"], "error")
        self.assertIn("task", resp["error"])

    def test_create_freebuff_with_orchestrator_validation(self) -> None:
        port = 1
        server = _start_orch()
        try:
            port = server.server_address[1]
            old = os.environ.get("JARVIS_FREEBUFF_PORT")
            os.environ["JARVIS_FREEBUFF_PORT"] = str(port)
            try:
                workspace = str(self.repo.resolve())
                # _OPENABLE_WORKSPACES/known_threads는 모듈 전역 — 이 서버 인스턴스에 등록
                _OPENABLE_WORKSPACES.add(workspace)
                tid = str(uuid.uuid4())
                _OrchHandler.known_threads[tid] = {
                    "model": "z-ai/glm-5.3-flash", "reasoningEffort": "high", "__projects": [workspace],
                }
                resp = _agent_session_create(self.client, 1, {
                    "agent_type": "freebuff",
                    "task_id": "t-abc123def456",
                    "purpose": "Agent orchestration",
                    "workspace": workspace,
                    "external_session_id": tid,
                    "model": "user/claims-wrong-model",
                })
                self.assertEqual(resp["status"], "ok", resp.get("error"))
                session = resp["session"]
                # thread metadata가 사용자 입력보다 authoritative(§9)
                self.assertEqual(session["model"], "z-ai/glm-5.3-flash")
                self.assertEqual(session["reasoning_effort"], "high")
                self.assertEqual(resp["validation"]["status"], "available")
                self.assertEqual(session["last_validation"]["status"], "available")
            finally:
                if old is None:
                    os.environ.pop("JARVIS_FREEBUFF_PORT", None)
                else:
                    os.environ["JARVIS_FREEBUFF_PORT"] = old
        finally:
            server.shutdown()

    def test_create_freebuff_degraded_when_orchestrator_down(self) -> None:
        # §10 — validation 실패가 binding 자체를 막지 않는다(degraded mode)
        old = os.environ.get("JARVIS_FREEBUFF_PORT")
        os.environ["JARVIS_FREEBUFF_PORT"] = "1"
        try:
            resp = _agent_session_create(self.client, 1, {
                "agent_type": "freebuff",
                "task_id": "t-abc123def456",
                "workspace": str(self.repo),
                "external_session_id": str(uuid.uuid4()),
                "model": "z-ai/glm-5.3-flash",
            })
            self.assertEqual(resp["status"], "ok", resp.get("error"))
            self.assertEqual(resp["validation"]["status"], "unavailable")
            self.assertEqual(resp["session"]["model"], "z-ai/glm-5.3-flash")
        finally:
            if old is None:
                os.environ.pop("JARVIS_FREEBUFF_PORT", None)
            else:
                os.environ["JARVIS_FREEBUFF_PORT"] = old

    # ---- get/list/update/archive -------------------------------------------

    def test_get_list_roundtrip(self) -> None:
        created = _agent_session_create(self.client, 1, {"agent_type": "codex", "purpose": "Review"})
        sid = created["session"]["session_id"]
        got = _agent_session_get(self.client, 2, {"session_id": sid})
        self.assertEqual(got["session"]["purpose"], "Review")
        listing = _agent_session_list(self.client, 3, {"agent_type": "codex"})
        self.assertEqual(listing["sessions"][0]["session_id"], sid)
        unknown = _agent_session_get(self.client, 4, {"session_id": "as-nope"})
        self.assertEqual(unknown["status"], "error")

    def test_update_and_archive_ops(self) -> None:
        created = _agent_session_create(self.client, 1, {"agent_type": "codex"})
        sid = created["session"]["session_id"]
        bad = _agent_session_update(self.client, 2, {"session_id": sid, "fields": {"session_id": "x"}})
        self.assertEqual(bad["status"], "error")
        updated = _agent_session_update(self.client, 3, {"session_id": sid, "fields": {"purpose": "New"}})
        self.assertEqual(updated["session"]["purpose"], "New")
        archived = _agent_session_archive(self.client, 4, {"session_id": sid})
        self.assertEqual(archived["session"]["status"], "archived")
        # §15 — 기본 목록에서 빠지고 include_archived로만 보인다
        listing = _agent_session_list(self.client, 5, {})
        self.assertEqual(listing["sessions"], [])
        listing_all = _agent_session_list(self.client, 6, {"include_archived": True})
        self.assertEqual(listing_all["sessions"][0]["status"], "archived")

    def test_freebuff_validate_op(self) -> None:
        server = _start_orch()
        try:
            old = os.environ.get("JARVIS_FREEBUFF_PORT")
            os.environ["JARVIS_FREEBUFF_PORT"] = str(server.server_address[1])
            try:
                workspace = str(self.repo.resolve())
                _OPENABLE_WORKSPACES.add(workspace)
                tid = str(uuid.uuid4())
                _OrchHandler.known_threads[tid] = {"model": "m1", "reasoningEffort": "low", "__projects": [workspace]}
                resp = _freebuff_session_validate(self.client, 7, {
                    "workspace": workspace, "external_session_id": tid,
                })
                self.assertEqual(resp["status"], "ok")
                self.assertEqual(resp["capability"]["status"], "available")
                # thread_missing도 fake success 없이(§21-G)
                resp2 = _freebuff_session_validate(self.client, 8, {
                    "workspace": workspace, "external_session_id": str(uuid.uuid4()),
                })
                self.assertEqual(resp2["capability"]["status"], "thread_missing")
            finally:
                if old is None:
                    os.environ.pop("JARVIS_FREEBUFF_PORT", None)
                else:
                    os.environ["JARVIS_FREEBUFF_PORT"] = old
        finally:
            server.shutdown()

    def test_validate_op_records_broken_state(self) -> None:
        # §16 — thread missing 시 session metadata를 지우지 않고 broken 표시만
        server = _start_orch()
        try:
            old = os.environ.get("JARVIS_FREEBUFF_PORT")
            os.environ["JARVIS_FREEBUFF_PORT"] = str(server.server_address[1])
            try:
                workspace = str(self.repo.resolve())
                _OPENABLE_WORKSPACES.add(workspace)
                created = _agent_session_create(self.client, 1, {
                    "agent_type": "freebuff", "task_id": "t-abc123def456",
                    "workspace": workspace, "external_session_id": str(uuid.uuid4()),
                    "model": "z-ai/glm-5.3-flash",
                })
                sid = created["session"]["session_id"]
                _freebuff_session_validate(self.client, 2, {
                    "workspace": workspace, "external_session_id": str(uuid.uuid4()),
                    "session_id": sid,
                })
                got = _agent_session_get(self.client, 3, {"session_id": sid})
                self.assertEqual(got["session"]["last_validation"]["status"], "thread_missing")
                self.assertEqual(got["session"]["model"], "z-ai/glm-5.3-flash")  # metadata 보존
            finally:
                if old is None:
                    os.environ.pop("JARVIS_FREEBUFF_PORT", None)
                else:
                    os.environ["JARVIS_FREEBUFF_PORT"] = old
        finally:
            server.shutdown()

    def test_handle_message_dispatch_sessions(self) -> None:
        resp = handle_message(self.client, type("S", (), {})(), {
            "type": "agent_session_list", "id": 9, "agent_type": "freebuff",
        })
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["sessions"], [])


class SessionRunIntegrationTest(unittest.TestCase):
    """§5/§21-E — Codex session에서 run 시작 시 authoritative 필드 결정."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Path(self.tmp.name) / "memory"
        self.memory.mkdir()
        (self.memory / "projects.md").write_text(
            "# Projects\n\n## Active\n\n- id: jarvis-app — title: JARVIS App\n",
            encoding="utf-8",
        )
        (self.memory / "tasks.md").write_text(
            "# Tasks\n\n## jarvis-app\n\n"
            "- [ ] t-abc123def456: smoke task — reason\n"
            "- [ ] t-000000000001: other task — reason\n",
            encoding="utf-8",
        )
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
            cwd=self.repo, check=True,
        )
        self.client = _fake_client(self.memory)
        self.client.config.file_roots = {"scratch": str(self.repo)}
        self.other_roots: list[Path] = []
        self.old_bin = os.environ.get("JARVIS_CODEX_BIN")
        self.old_timeout = os.environ.get("JARVIS_AGENT_RUN_TIMEOUT_S")
        os.environ["JARVIS_CODEX_BIN"] = f"python {_write_fake_codex(Path(self.tmp.name))}"
        os.environ["JARVIS_AGENT_RUN_TIMEOUT_S"] = "30"
        created = _agent_session_create(self.client, 1, {
            "agent_type": "codex",
            "task_id": "t-abc123def456",
            "purpose": "Code Review",
            "workspace": str(self.repo),
        })
        self.session = created["session"]

    def tearDown(self) -> None:
        try:
            store = AgentRunStore(self.memory / "agent_runs.json")
            for run in store.list_runs(limit=100):
                if run.get("status") in {"queued", "running"}:
                    runner = __import__("scripts.harness_bridge", fromlist=["_AGENT_RUNNERS"])._AGENT_RUNNERS.get(run.get("agent_type"))
                    if runner is not None:
                        runner.cancel(run["run_id"])
        except Exception:
            pass
        if self.old_bin is None:
            os.environ.pop("JARVIS_CODEX_BIN", None)
        else:
            os.environ["JARVIS_CODEX_BIN"] = self.old_bin
        if self.old_timeout is None:
            os.environ.pop("JARVIS_AGENT_RUN_TIMEOUT_S", None)
        else:
            os.environ["JARVIS_AGENT_RUN_TIMEOUT_S"] = self.old_timeout

    def _wait_terminal(self, run_id: str, deadline: float = 30.0) -> dict:
        end = time.time() + deadline
        last = {}
        while time.time() < end:
            resp = _agent_run_status(self.client, 99, {"run_id": run_id})
            last = resp["run"]
            if last["status"] in {"completed", "failed", "cancelled"}:
                return last
            time.sleep(0.1)
        return last

    def test_run_from_session_stamps_authoritative_fields(self) -> None:
        resp = _agent_run_start(self.client, 2, {
            "session_id": self.session["session_id"],
            # renderer가 모순 값을 보내도 Harness가 session 값을 쓴다(§5)
            "agent_type": "codex",
            "task_id": "t-000000000001",
            "instruction": "review the code",
            "repo_path": str(self.repo),
        })
        self.assertEqual(resp["status"], "ok", resp.get("error"))
        run = resp["run"]
        self.assertEqual(run["session_id"], self.session["session_id"])
        self.assertEqual(run["task_id"], "t-abc123def456")  # session 것이 이긴다
        self.assertEqual(run["agent_type"], "codex")
        final = self._wait_terminal(run["run_id"])
        self.assertEqual(final["status"], "completed")
        # session에 run 링크 기록
        got = _agent_session_get(self.client, 3, {"session_id": self.session["session_id"]})
        self.assertEqual(got["session"]["run_count"], 1)
        self.assertEqual(got["session"]["last_run_id"], run["run_id"])

    def test_unknown_session_rejected(self) -> None:
        resp = _agent_run_start(self.client, 2, {
            "session_id": "as-nope", "instruction": "x", "repo_path": str(self.repo),
        })
        self.assertEqual(resp["status"], "error")

    def test_archived_session_rejected(self) -> None:
        _agent_session_archive(self.client, 2, {"session_id": self.session["session_id"]})
        resp = _agent_run_start(self.client, 3, {
            "session_id": self.session["session_id"],
            "instruction": "x", "repo_path": str(self.repo),
        })
        self.assertEqual(resp["status"], "error")
        self.assertIn("archived", resp["error"])

    def test_run_without_session_still_works(self) -> None:
        # migration-safe — session 없는 기존 경로는 session_id=None
        resp = _agent_run_start(self.client, 2, {
            "agent_type": "codex", "instruction": "x", "repo_path": str(self.repo),
        })
        self.assertEqual(resp["status"], "ok", resp.get("error"))
        self.assertIsNone(resp["run"]["session_id"])
        self._wait_terminal(resp["run"]["run_id"])

    def test_session_workspace_wins_over_renderer_repo_path(self) -> None:
        # session의 workspace가 authoritative — 다른 approved root를 보내도 session 것 사용
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.other_roots.append(other)
        subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        (other / "a.txt").write_text("a", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=other, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
            cwd=other, check=True,
        )
        self.client.config.file_roots = {"scratch": str(self.repo), "other": str(other)}
        created = _agent_session_create(self.client, 4, {
            "agent_type": "codex", "workspace": str(other),
        })
        resp = _agent_run_start(self.client, 5, {
            "session_id": created["session"]["session_id"],
            "instruction": "x",
            "repo_path": str(self.repo),  # 다른 root — 무시되어야 한다
        })
        self.assertEqual(resp["status"], "ok", resp.get("error"))
        self.assertEqual(Path(resp["run"]["repo_path"]).resolve(), other.resolve())
        self._wait_terminal(resp["run"]["run_id"])

    def test_startup_interrupt_marking_still_works_with_sessions(self) -> None:
        store = AgentRunStore(self.memory / "agent_runs.json")
        run = store.create_run(agent_type="codex", instruction="x", repo_path=str(self.repo))
        store.update(run["run_id"], status="running", pid=999999)
        marked = mark_agent_runs_interrupted(self.client)
        self.assertTrue(any(m["run_id"] == run["run_id"] for m in marked))


if __name__ == "__main__":
    unittest.main()
