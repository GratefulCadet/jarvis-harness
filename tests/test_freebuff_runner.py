from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from harness.agent_runs import AgentRunStore, default_agent_runs_file, get_runner
from harness.agent_sessions import AgentSessionStore, default_agent_sessions_file
from harness.freebuff_orchestrator import (
    AUTH_HOST,
    FreebuffOrchestratorError,
    FreebuffOrchestratorManager,
)
from harness.freebuff_runner import FreebuffRunner
from harness.config import HarnessConfig
from scripts.harness_bridge import (
    _agent_run_cancel, _agent_run_result, _agent_run_start, _agent_run_status,
    handle_message, run_bridge,
)


class _FakeState:
    def __init__(self) -> None:
        self.mode = "success"
        self.project = None
        self.thread_id = str(uuid.uuid4())
        self.messages = [
            {"id": "old-u", "role": "user", "content": "prior user"},
            {"id": "old-a", "role": "assistant", "content": "PRIOR_RESULT", "committed": True},
        ]
        self.lock = threading.RLock()
        self.stop_count = 0
        self.message_count = 0
        self.turn_state = "idle"
        self.auth_header_values: list[str] = []
        self.injected_process = None

    def reset_turn(self, mode: str | None = None) -> None:
        with self.lock:
            self.mode = mode or self.mode
            self.messages = [
                {"id": "old-u", "role": "user", "content": "prior user"},
                {"id": "old-a", "role": "assistant", "content": "PRIOR_RESULT", "committed": True},
            ]
            self.stop_count = 0
            self.message_count = 0
            self.turn_state = "idle"


_STATE = _FakeState()


class _FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        return

    def _send(self, code: int, value: object, *, raw: bytes | None = None) -> None:
        data = raw if raw is not None else json.dumps(value).encode("utf-8")
        if code == 404:
            _STATE.auth_header_values.append(f"404:{self.command}:{self.path}")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self) -> bool:
        _STATE.auth_header_values.append(f"{self.command}:{self.path}:{self.headers.get('x-freebuff-launch-id', '')}")
        return bool(self.headers.get("x-freebuff-launch-id"))

    def do_GET(self) -> None:
        if not self._auth_ok():
            self._send(401, {"error": "missing launch id"})
            return
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        if self.path == "/api/auth/status":
            self._send(200, {"authed": True})
            return
        if self.path.startswith("/api/thread/"):
            if _STATE.mode == "http_error":
                self._send(503, {"error": "unavailable"})
                return
            if _STATE.mode == "malformed":
                self._send(200, {}, raw=b"not-json")
                return
            with _STATE.lock:
                snapshot = [dict(message) for message in _STATE.messages]
            self._send(200, {
                "thread": {"id": _STATE.thread_id, "status": "open", "model": "test-model", "turnState": _STATE.turn_state},
                "messages": snapshot,
            })
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            self._send(401, {"error": "missing launch id"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "bad json"})
            return
        if self.path == "/api/project/open":
            _STATE.project = body.get("path")
            self._send(200, {"ok": True})
            return
        if self.path.startswith("/api/auth/status"):
            self._send(200, {"authed": True})
            return
        if self.path == f"/api/thread/{_STATE.thread_id}/message":
            _STATE.message_count += 1
            _STATE.turn_state = "running"
            self._send(200, {"ok": True})
            if _STATE.mode == "success":
                def complete() -> None:
                    time.sleep(0.04)
                    with _STATE.lock:
                        _STATE.messages.extend([
                            {"id": f"new-u-{_STATE.message_count}", "role": "user", "content": body.get("text", "")},
                            {"id": f"new-a-{_STATE.message_count}", "role": "assistant", "content": "FREEBUFF_RUNNER_OK", "committed": True},
                        ])
                        _STATE.turn_state = "idle"
                threading.Thread(target=complete, daemon=True).start()
            elif _STATE.mode == "schema_no_ids":
                def complete_schema() -> None:
                    time.sleep(0.04)
                    with _STATE.lock:
                        _STATE.messages.extend([
                            {"role": "user", "content": body.get("text", "")},
                            {
                                "role": "assistant",
                                "parts": [
                                    {"kind": "reasoning", "text": "not user-visible"},
                                    {"kind": "text", "text": "FREEBUFF_RUNNER_OK"},
                                ],
                                "metrics": {},
                                "ts": "2026-09-29T10:00:01Z",
                            },
                        ])
                        _STATE.turn_state = "idle"
                threading.Thread(target=complete_schema, daemon=True).start()
            elif _STATE.mode == "empty_committed":
                def complete_empty() -> None:
                    time.sleep(0.04)
                    with _STATE.lock:
                        _STATE.messages.extend([
                            {"role": "user", "content": body.get("text", "")},
                            {"role": "assistant", "parts": [], "metrics": {}, "ts": "2026-09-29T10:00:01Z"},
                        ])
                        _STATE.turn_state = "idle"
                threading.Thread(target=complete_empty, daemon=True).start()
            elif _STATE.mode == "manual":
                # Test controls the committed response to place barriers at exact races.
                return
            elif _STATE.mode == "long":
                def emit() -> None:
                    index = 0
                    while _STATE.stop_count == 0:
                        index += 1
                        with _STATE.lock:
                            _STATE.turn_state = "running"
                            _STATE.messages = [m for m in _STATE.messages if m.get("id") != "long-a"]
                            _STATE.messages.extend([
                                {"id": "long-u", "role": "user", "content": "long request"},
                                {"id": "long-a", "role": "assistant", "content": f"working-{index}", "committed": False},
                            ])
                        time.sleep(0.08)
                threading.Thread(target=emit, daemon=True).start()
            return
        if self.path == f"/api/thread/{_STATE.thread_id}/stop":
            if _STATE.mode == "stop_error":
                self._send(503, {"error": "stop unavailable"})
                return
            _STATE.stop_count += 1
            with _STATE.lock:
                _STATE.turn_state = "idle"
                for message in _STATE.messages:
                    if message.get("id") == "long-a":
                        message["committed"] = True
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})


def _server() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _FakeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Client:
    def __init__(self, memory: Path, workspace: Path) -> None:
        self.config = HarnessConfig(runtime="mock", memory_dir=memory, trace_dir=memory / "traces")
        self.config.file_roots = {"scratch": str(workspace)}
        self.tools = type("Tools", (), {"get": staticmethod(lambda name: None)})()


class FreebuffManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.user_state = self.root / "user-state.json"
        self.secret = "test-secret-do-not-log"
        self.user_payload = {
            "authSessions": {AUTH_HOST: {"token": self.secret, "user": {"id": "test-user"}}, "https://unrelated.invalid": {"secret": "other"}},
            "recentProjects": ["private-project"],
            "tabs": ["private-tab"],
            "uiPrefs": {"secret": "private-pref"},
        }
        self.user_state.write_text(json.dumps(self.user_payload), encoding="utf-8")
        self.server = _server()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.manager = FreebuffOrchestratorManager(
            user_state_path=self.user_state,
            request_timeout_s=2,
            _test_base_url=f"http://127.0.0.1:{self.server.server_port}",
            _test_launch_id="runner-test-launch",
            _test_state_dir=self.root / "isolated",
        )
        self.addCleanup(self.manager.cleanup)

    def test_minimal_auth_state_isolated_and_user_state_unchanged(self) -> None:
        before = self.user_state.read_bytes()
        self.manager.ensure_running()
        state_path = self.manager._auth_state.state_file
        stored = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(set(stored), {"authSessions"})
        self.assertEqual(set(stored["authSessions"]), {AUTH_HOST})
        self.assertEqual(stored["authSessions"][AUTH_HOST]["token"], self.secret)
        self.assertNotIn("recentProjects", state_path.read_text(encoding="utf-8"))
        self.assertNotIn("uiPrefs", state_path.read_text(encoding="utf-8"))
        self.assertEqual(self.user_state.read_bytes(), before)
        self.assertNotEqual(state_path.stat().st_ino, self.user_state.stat().st_ino)
        self.assertNotIn(self.secret, repr(self.manager.__dict__))

    def test_reuse_health_and_loopback_path_guard(self) -> None:
        self.manager.ensure_running()
        first_process = self.manager._proc
        self.manager.ensure_running()
        self.assertIs(self.manager._proc, first_process)
        self.assertEqual(self.manager.describe()["running"], True)
        with self.assertRaises(FreebuffOrchestratorError):
            self.manager.request("http://example.com/healthz")
        with self.assertRaises(FreebuffOrchestratorError):
            FreebuffOrchestratorManager(_test_base_url="http://example.com:1234")

    def test_child_death_is_detected_and_temp_state_removed(self) -> None:
        self.manager.ensure_running()
        self.manager.ensure_running()
        state_dir = self.manager._auth_state.state_dir
        self.manager._proc.dead = True
        with self.assertRaises(FreebuffOrchestratorError):
            self.manager.request("/healthz")
        self.assertIsNone(self.manager._proc)
        self.assertIsNotNone(state_dir)
        self.assertFalse(state_dir.exists())

    def test_cleanup_removes_credential_state(self) -> None:
        self.manager.ensure_running()
        state_dir = self.manager._auth_state.state_dir
        self.assertIsNotNone(state_dir)
        self.manager.cleanup()
        self.assertFalse(state_dir.exists())
        self.assertIsNone(self.manager._handle)
        self.assertIsNone(self.manager._proc)


class FreebuffRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        _STATE.reset_turn("success")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.memory = self.root / "memory"
        self.memory.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.server = _server()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.manager = FreebuffOrchestratorManager(
            user_state_path=self.root / "unused.json",
            request_timeout_s=2,
            _test_base_url=f"http://127.0.0.1:{self.server.server_port}",
            _test_launch_id="runner-test-launch",
            _test_state_dir=self.root / "isolated",
        )
        self.store = AgentRunStore(default_agent_runs_file(self.memory))
        self.sessions = AgentSessionStore(default_agent_sessions_file(self.memory))
        (self.memory / "tasks.md").write_text(
            "# Tasks" + chr(10) + chr(10) + "## jarvis-app" + chr(10) + chr(10)
            + "- [ ] t-authoritative: runner test task — reason" + chr(10),
            encoding="utf-8",
        )
        self.session = self.sessions.create_session(
            agent_type="freebuff", workspace=str(self.workspace), external_session_id=_STATE.thread_id,
            task_id="t-authoritative", model="session-model", reasoning_effort="high",
        )
        self.runner = FreebuffRunner(self.store, self.memory, session_store=self.sessions, manager=self.manager,
                                     poll_interval_s=0.04, completion_timeout_s=2, cancel_confirm_timeout_s=1)
        self.addCleanup(self.manager.cleanup)

    def _client(self, memory: Path, workspace: Path):
        return _Client(memory, workspace)

    def _new_run(self, instruction="reply"):
        return self.store.create_run(agent_type="freebuff", instruction=instruction, repo_path=str(self.workspace),
                                     session_id=self.session["session_id"], task_id=self.session["task_id"])

    def _wait(self, run_id: str, timeout: float = 3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = self.store.get(run_id)
            if run["status"] in {"completed", "failed", "cancelled"}:
                return run
            time.sleep(0.02)
        return self.store.get(run_id)

    def test_success_extracts_only_new_assistant_result_and_reuses_manager(self) -> None:
        first = self._new_run()
        self.runner.start(first["run_id"], {"instruction": "reply"})
        completed = self._wait(first["run_id"])
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(completed["result_summary"], "FREEBUFF_RUNNER_OK")
        self.assertNotEqual(completed["result_summary"], "PRIOR_RESULT")
        self.assertEqual(completed["external_thread_id"], _STATE.thread_id)
        self.assertEqual(completed["started_message_count"], 2)
        process = self.manager._proc
        _STATE.reset_turn("success")
        second = self._new_run("reply again")
        self.runner.start(second["run_id"], {"instruction": "reply again"})
        self.assertIs(self.manager._proc, process)
        self.assertEqual(self._wait(second["run_id"])["status"], "completed")
        self.assertEqual(_STATE.message_count, 1)

    def test_empty_committed_assistant_response_is_failure_not_success(self) -> None:
        _STATE.reset_turn("empty_committed")
        run = self._new_run("empty response")
        self.runner.start(run["run_id"], {"instruction": "empty response"})
        result = self._wait(run["run_id"])
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("empty", (result.get("error") or "").lower())
        self.assertIsNone(result["result_summary"])

    def test_stop_failure_does_not_report_cancelled(self) -> None:
        _STATE.reset_turn("stop_error")
        run = self._new_run("stop failure")
        self.runner.start(run["run_id"], {"instruction": "stop failure"})
        with self.assertRaises(Exception):
            self.runner.cancel(run["run_id"])
        result = self.store.get(run["run_id"])
        self.assertEqual(result["status"], "failed", result)
        self.assertNotEqual(result["status"], "cancelled")

    def test_cancel_calls_real_stop_and_waits_for_stable_output(self) -> None:
        _STATE.reset_turn("long")
        run = self._new_run("long harmless request")
        self.runner.start(run["run_id"], {"instruction": "long harmless request"})
        time.sleep(0.15)
        cancelled = self.runner.cancel(run["run_id"])
        self.assertEqual(_STATE.stop_count, 1)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.store.get(run["run_id"])["status"], "cancelled")

    def test_real_committed_schema_without_ids_uses_timestamp_and_baseline_boundary(self) -> None:
        _STATE.reset_turn("schema_no_ids")
        _STATE.messages = [
            {"role": "user", "content": "old"},
            {
                "role": "assistant",
                "parts": [{"kind": "text", "text": "PRIOR_RESULT"}],
                "metrics": {},
                "ts": "2026-09-29T09:00:00Z",
            },
        ]
        run = self._new_run()
        self.runner.start(run["run_id"], {"instruction": "schema check"})
        result = self._wait(run["run_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["result_summary"], "FREEBUFF_RUNNER_OK")
        baseline = result["freebuff_baseline"]
        self.assertEqual(baseline["baseline_assistant_count"], 1)
        self.assertEqual(baseline["assistant_ids"], ["ts:2026-09-29T09:00:00Z"])

    def test_cancelled_run_cannot_be_overwritten_by_late_watcher_completion(self) -> None:
        from unittest.mock import patch

        _STATE.reset_turn("manual")
        run = self._new_run("manual response")
        entered_completion = threading.Event()
        release_completion = threading.Event()
        original = self.runner._completion_evidence

        def delayed_completion(*args, **kwargs):
            entered_completion.set()
            if not release_completion.wait(2):
                raise AssertionError("completion barrier timed out")
            return original(*args, **kwargs)

        original_request = self.manager.request

        def request_with_user_turn(path, *, method="GET", body=None, **kwargs):
            response = original_request(path, method=method, body=body, **kwargs)
            if path == f"/api/thread/{_STATE.thread_id}/message" and method == "POST":
                with _STATE.lock:
                    _STATE.messages.append({
                        "role": "user", "content": body["text"], "ts": "2026-09-29T10:00:00Z",
                    })
            return response

        with patch.object(self.manager, "request", side_effect=request_with_user_turn), patch.object(
            self.runner, "_completion_evidence", side_effect=delayed_completion,
        ):
            self.runner.start(run["run_id"], {"instruction": "manual response"})
            with _STATE.lock:
                _STATE.messages.extend([
                    {
                        "role": "assistant", "parts": [{"kind": "text", "text": "late answer"}],
                        "metrics": {}, "ts": "2026-09-29T10:00:01Z",
                    },
                ])
                _STATE.turn_state = "idle"
            self.assertTrue(entered_completion.wait(2), "watcher did not reach completion decision")
            cancel_result = self.runner.cancel(run["run_id"])
            self.assertEqual(cancel_result["status"], "cancelled")
            release_completion.set()
            deadline = time.time() + 2
            while time.time() < deadline and self.runner._threads.get(run["run_id"]) is not None:
                time.sleep(0.01)

        final = self.store.get(run["run_id"])
        self.assertEqual(final["status"], "cancelled", final)
        self.assertIsNone(final["result_summary"])

    def test_live_stream_is_not_committed_and_reasoning_is_not_result_text(self) -> None:
        from harness.freebuff_runner import _assistant_text, _is_committed

        live = {"role": "assistant", "streamSeq": 4, "parts": []}
        self.assertFalse(_is_committed(live))
        persisted = {
            "role": "assistant",
            "parts": [
                {"kind": "reasoning", "text": "private chain of thought"},
                {"kind": "text", "text": "FREEBUFF_RUNNER_OK"},
                {"kind": "ad"},
            ],
        }
        self.assertEqual(_assistant_text(persisted), "FREEBUFF_RUNNER_OK")
        self.assertEqual(_assistant_text({"role": "assistant", "parts": [{"kind": "reasoning", "text": "only thought"}]}), "")

    def test_freebuff_runner_cache_rebinds_only_for_a_different_store_key(self) -> None:
        from scripts import harness_bridge as bridge

        prior = {
            "store": bridge._AGENT_RUN_STORE,
            "runners": dict(bridge._AGENT_RUNNERS),
            "session_store": bridge._AGENT_SESSION_STORE,
            "manager": bridge._FREEBUFF_MANAGER,
            "runner": bridge._FREEBUFF_RUNNER,
            "runner_key": bridge._FREEBUFF_RUNNER_KEY,
            "overrides": list(bridge._FREEBUFF_MANAGER_OVERRIDES),
        }
        bridge._AGENT_RUN_STORE = None
        bridge._AGENT_RUNNERS.clear()
        bridge._FREEBUFF_RUNNER = None
        bridge._FREEBUFF_MANAGER = self.manager
        bridge._FREEBUFF_RUNNER_KEY = None
        bridge._FREEBUFF_MANAGER_OVERRIDES.clear()
        first = bridge._freebuff_runner(self.store, self.memory)
        self.assertIs(first.manager, self.manager)
        again = bridge._freebuff_runner(self.store, self.memory)
        self.assertIs(again, first)
        second_memory = self.root / "other-memory"
        second_memory.mkdir()
        second_store = AgentRunStore(default_agent_runs_file(second_memory))
        changed = bridge._freebuff_runner(second_store, second_memory)
        self.assertIsNot(changed, first)
        self.assertEqual(bridge._FREEBUFF_RUNNER_KEY, (str(second_memory.resolve()), str(second_store.registry_file.resolve())))
        bridge._FREEBUFF_RUNNER = prior["runner"]
        bridge._FREEBUFF_MANAGER = prior["manager"]
        bridge._FREEBUFF_RUNNER_KEY = prior["runner_key"]
        bridge._AGENT_RUN_STORE = prior["store"]
        bridge._AGENT_SESSION_STORE = prior["session_store"]
        bridge._AGENT_RUNNERS.clear()
        bridge._AGENT_RUNNERS.update(prior["runners"])
        bridge._FREEBUFF_MANAGER_OVERRIDES[:] = prior["overrides"]

    def test_http_and_malformed_failures(self) -> None:
        for mode in ("http_error", "malformed"):
            _STATE.reset_turn(mode)
            run = self._new_run()
            with self.assertRaises(Exception):
                self.runner.start(run["run_id"], {"instruction": "reply"})

    def test_session_authority_and_inactive_rejection(self) -> None:
        run = self._new_run()
        self.runner.start(run["run_id"], {"instruction": "reply", "workspace": "evil", "thread_id": str(uuid.uuid4())})
        completed = self._wait(run["run_id"])
        self.assertEqual(_STATE.project, str(self.workspace))
        self.assertEqual(completed["external_thread_id"], _STATE.thread_id)
        self.sessions.update(self.session["session_id"], status="broken")
        broken = self._new_run()
        with self.assertRaises(Exception):
            self.runner.start(broken["run_id"], {"instruction": "no"})

    def test_archived_and_awaiting_auth_sessions_are_refused(self) -> None:
        """Only status=active sessions may start a run. archived/awaiting_auth are
        refused at the runner boundary *and* at the bridge boundary, so a renderer
        cannot revive a stored session by naming it."""
        from scripts import harness_bridge as bridge

        for refused in ("archived", "awaiting_auth"):
            with self.subTest(status=refused):
                self.sessions.update(self.session["session_id"], status=refused)
                run = self._new_run()
                with self.assertRaises(Exception):
                    self.runner.start(run["run_id"], {"instruction": "no"})
                self.assertEqual(self.store.get(run["run_id"])["status"], "queued")
                self.assertEqual(
                    _STATE.message_count, 0,
                    "no message may reach a refused session",
                )

                bridge._AGENT_RUN_STORE = self.store
                bridge._AGENT_RUNNERS.clear()
                bridge._AGENT_SESSION_STORE = self.sessions
                bridge._FREEBUFF_RUNNER = None
                bridge._FREEBUFF_MANAGER = None
                bridge._FREEBUFF_MANAGER_OVERRIDES[:] = [self.manager]
                try:
                    answer = _agent_run_start(
                        self._client(self.memory, self.workspace), 91,
                        {"session_id": self.session["session_id"], "instruction": "no"},
                    )
                finally:
                    bridge._AGENT_RUNNERS.clear()
                    bridge._AGENT_RUN_STORE = None
                    bridge._AGENT_SESSION_STORE = None
                    bridge._FREEBUFF_RUNNER = None
                    bridge._FREEBUFF_MANAGER = None
                    bridge._FREEBUFF_MANAGER_OVERRIDES.clear()
                self.assertEqual(answer["status"], "error", answer)
                self.assertIn(refused, answer["error"])
        # Restore for the other tests in this case.
        self.sessions.update(self.session["session_id"], status="active")

    def test_bridge_session_authority_registry_result_and_shutdown_cleanup(self) -> None:
        from scripts import harness_bridge as bridge

        bridge._AGENT_RUN_STORE = AgentRunStore(default_agent_runs_file(self.memory))
        bridge._AGENT_RUNNERS.clear()
        bridge._AGENT_SESSION_STORE = None
        bridge._FREEBUFF_RUNNER = None
        bridge._FREEBUFF_MANAGER = None
        bridge._FREEBUFF_MANAGER_OVERRIDES[:] = [self.manager]
        try:
            started = _agent_run_start(self._client(self.memory, self.workspace), 70, {
                "agent_type": "codex",  # Freebuff session is authoritative.
                "session_id": self.session["session_id"],
                "instruction": "reply through bridge",
                "workspace": str(self.root / "evil"),
                "thread_id": str(uuid.uuid4()),
            })
            self.assertEqual(started["status"], "ok", f"{started} history={_STATE.auth_header_values!r} runner={getattr(bridge._FREEBUFF_RUNNER, 'manager', None)!r}")
            run_id = started["run"]["run_id"]
            self.assertEqual(started["run"]["agent_type"], "freebuff")
            self.assertEqual(started["run"]["repo_path"], str(self.workspace))
            result = None
            deadline = time.time() + 3
            while time.time() < deadline:
                result = _agent_run_result(self._client(self.memory, self.workspace), 71, {"run_id": run_id})
                if result["run"]["status"] == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(result["run"]["result_summary"], "FREEBUFF_RUNNER_OK")
            wrong = _agent_run_status(self._client(self.memory, self.workspace), 72, {
                "run_id": run_id, "agent_type": "codex",
            })
            self.assertEqual(wrong["run"]["status"], "completed")
            self.assertEqual(_STATE.project, str(self.workspace))

            output: list[str] = []
            lines = iter([json.dumps({"type": "shutdown", "id": 73}), ""])
            run_bridge(
                self._client(self.memory, self.workspace),
                lambda: next(lines),
                output.append,
            )
            self.assertEqual(json.loads(output[0])["status"], "ok")
            self.assertIsNone(self.manager._auth_state)
            with self.assertRaises(FreebuffOrchestratorError):
                self.manager.ensure_running()
        finally:
            bridge._AGENT_RUNNERS.clear()
            bridge._AGENT_RUN_STORE = None
            bridge._AGENT_SESSION_STORE = None
            bridge._FREEBUFF_RUNNER = None
            bridge._FREEBUFF_MANAGER = None
            bridge._FREEBUFF_MANAGER_OVERRIDES.clear()

    def test_unavailable_manager_fails_without_fallback(self) -> None:
        _STATE.reset_turn("success")
        dead_manager = FreebuffOrchestratorManager(
            _test_base_url="http://127.0.0.1:1", request_timeout_s=0.05
        )
        runner = FreebuffRunner(self.store, self.memory, session_store=self.sessions, manager=dead_manager,
                                poll_interval_s=0.02, completion_timeout_s=0.1)
        run = self._new_run()
        with self.assertRaises(Exception):
            runner.start(run["run_id"], {"instruction": "reply"})
        self.assertEqual(self.store.get(run["run_id"])["agent_type"], "freebuff")
        self.assertNotEqual(self.store.get(run["run_id"])["agent_type"], "codex")


if __name__ == "__main__":
    unittest.main()
