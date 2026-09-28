from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness.freebuff_orchestrator import (
    FREEBUFF_AUTH_HOST,
    FreebuffOrchestratorError,
    FreebuffOrchestratorManager,
    IsolatedAuthState,
    OrchestratorHandle,
    OrchestratorUnavailable,
    build_isolated_auth_state,
    remove_isolated_auth_state,
    sanitize_detail,
    validate_relative_path,
)

"""FreebuffOrchestratorManager 단위 테스트.

실제 Freebuff를 띄우지 않는다. 계층별로 나눠 결정적으로 검증한다:
- 경로/transport 표면(loopback 강제, 상대 경로 강제, header 부착 위치)
- auth-state 격리(복사 금지 항목, 사용자 state 불변, token 비노출)
- lifecycle(ensure_running 재사용, child death 감지, stop/cleanup)
- 실제 프로세스 기동은 fake orchestrator child process로 검증한다.
"""


_FAKE_CHILD_TEMPLATE = '''
import http.server, json, os, sys, threading, time

EXIT_AFTER = {exit_after!r}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_GET(self):
        if self.path.startswith("/healthz"):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{{"ok":true}}')
            return
        self.send_response(404)
        self.end_headers()

    do_POST = do_GET


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
print("[orchestrator-ready] " + json.dumps({{
    "launchId": os.environ.get("FREEBUFF_LAUNCH_ID"),
    "pid": os.getpid(),
    "port": server.server_address[1],
    "statePath": os.environ.get("FREEBUFF_DESKTOP_STATE_PATH"),
    "portEnv": os.environ.get("PORT"),
    "uiOrigin": os.environ.get("FREEBUFF_DESKTOP_UI_ORIGIN"),
}}), flush=True)
if EXIT_AFTER is None:
    while True:
        time.sleep(0.5)
else:
    time.sleep(EXIT_AFTER)
    sys.exit(3)
'''


def _fake_child_source(exit_after_s: float | None = None) -> str:
    """fake orchestrator — 실제 Freebuff와 같은 readiness/healthz 계약을 흉내 낸다.

    child는 실제로 기동·healthz 응답·종료까지 하므로 manager의 launch/death/restart
    경로가 진짜 프로세스로 검증된다.
    """
    return _FAKE_CHILD_TEMPLATE.format(exit_after=exit_after_s)


class _StubProcess:
    """Popen stand-in — manager가 요구하는 최소 surface."""

    def __init__(self, pid: int, alive: bool = True) -> None:
        self.pid = pid
        self.stdout = None
        self.returncode = None if alive else 9
        self._alive = alive

    def poll(self):
        return None if self._alive else self.returncode

    def wait(self, timeout=None):
        return self.returncode


def _stub_handle(manager: "FreebuffOrchestratorManager", base_url: str, launch_id: str = "l"):
    """manager에 직접 주입할 최소 handle (기동 없이 transport/health만 검증)."""
    handle = OrchestratorHandle(
        pid=1234, port=0, launch_id=launch_id,
        state_dir="<none>", started_at="2026-01-01T00:00:00+00:00",
    )
    handle.port = int(base_url.rsplit(":", 1)[1])
    handle.__dict__["_stub_base_url"] = base_url
    manager._handle = handle
    return handle



def _write_fake_user_state(directory: Path, *, with_auth: bool = True) -> Path:
    state = directory / "state.json"
    payload = {
        "analyticsId": "anon-1",
        "machineId": "machine-1",
        "recentProjects": ["/home/user/secret-project"],
        "uiPrefs": {"theme": "dark", "openTabs": ["/home/user/secret-project/src"]},
        "composerDrafts": ["private draft text"],
        "authSessions": {},
    }
    if with_auth:
        payload["authSessions"] = {
            FREEBUFF_AUTH_HOST: {"token": "TOKEN-ABCDEF0123456789-XYZ", "user": {"id": "u-1"}}
        }
    state.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


class RelativePathValidationTests(unittest.TestCase):
    def test_accepts_relative_paths(self):
        self.assertEqual(validate_relative_path("/api/thread/abc"), "/api/thread/abc")
        self.assertEqual(validate_relative_path("api/thread/abc"), "/api/thread/abc")

    def test_rejects_absolute_and_scheme_urls(self):
        for bad in ("http://evil.example/x", "https://evil.example/x", "//evil.example/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(FreebuffOrchestratorError):
                    validate_relative_path(bad)

    def test_rejects_traversal_backslash_and_control_chars(self):
        for bad in ("/api/../../etc/passwd", "/api\\thread", "/api\nx", "/api\x00x", ""):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(FreebuffOrchestratorError):
                    validate_relative_path(bad)


class SanitizeTests(unittest.TestCase):
    def test_redacts_long_secret_shaped_fragments(self):
        text = sanitize_detail("failed with eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefghijklmnop here")
        self.assertIn("<redacted>", text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefghijklmnop", text)

    def test_keeps_ordinary_detail_readable(self):
        self.assertIn("connection refused", sanitize_detail("connection refused"))

    def test_truncates(self):
        self.assertLessEqual(len(sanitize_detail("x" * 5000)), 300)


class AuthStateIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-auth-test-"))
        self.source = _write_fake_user_state(self.tmp)

    def _isolated(self, **kwargs):
        state = build_isolated_auth_state(source_path=self.source, **kwargs)
        self.addCleanup(remove_isolated_auth_state, state)
        return state

    def test_extracts_only_the_needed_auth_entry(self):
        state = self._isolated()
        payload = json.loads(state.state_file.read_text(encoding="utf-8"))
        self.assertTrue(state.has_auth)
        self.assertEqual(list(payload.keys()), ["authSessions"])
        entry = payload["authSessions"][FREEBUFF_AUTH_HOST]
        self.assertEqual(sorted(entry.keys()), ["token", "user"])

    def test_does_not_copy_recent_projects_tabs_or_uiprefs(self):
        state = self._isolated()
        raw = state.state_file.read_text(encoding="utf-8")
        for forbidden in ("recentProjects", "uiPrefs", "openTabs", "composerDrafts",
                          "secret-project", "private draft", "analyticsId", "machineId"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, raw)

    def test_user_source_state_is_never_modified(self):
        before = self.source.read_bytes()
        before_stat = self.source.stat().st_mtime_ns
        self._isolated()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.source.stat().st_mtime_ns, before_stat)

    def test_isolated_state_is_not_a_link_to_source(self):
        state = self._isolated()
        self.assertNotEqual(state.state_file.resolve(), self.source.resolve())
        # hardlink/symlink로 묶여 있지 않아야 한다.
        self.assertEqual(state.state_file.stat().st_nlink, 1)
        self.assertFalse(state.state_file.is_symlink())

    def test_state_lives_outside_the_repository(self):
        state = self._isolated()
        repo_root = Path(__file__).resolve().parents[1]
        self.assertNotIn(repo_root, state.state_dir.resolve().parents)

    def test_token_never_appears_in_handle_metadata(self):
        state = self._isolated()
        rendered = json.dumps(state.as_dict(), ensure_ascii=False)
        self.assertNotIn("TOKEN-ABCDEF", rendered)
        self.assertNotIn("token", rendered.lower())

    def test_file_permissions_are_owner_only_where_supported(self):
        if os.name == "nt":
            self.skipTest("POSIX 권한 비트만 해당")
        state = self._isolated()
        self.assertEqual(stat.S_IMODE(state.state_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(state.state_dir.stat().st_mode), 0o700)

    def test_missing_auth_entry_reports_honestly(self):
        noauth_dir = self.tmp / "noauth"
        noauth_dir.mkdir(exist_ok=True)
        source = _write_fake_user_state(noauth_dir, with_auth=False)
        state = build_isolated_auth_state(source_path=source)
        self.addCleanup(remove_isolated_auth_state, state)
        self.assertFalse(state.has_auth)
        payload = json.loads(state.state_file.read_text(encoding="utf-8"))
        self.assertEqual(payload, {})

    def test_unreadable_source_raises_without_leaking(self):
        broken = self.tmp / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with self.assertRaises(FreebuffOrchestratorError) as ctx:
            build_isolated_auth_state(source_path=broken)
        self.assertNotIn("TOKEN-ABCDEF", str(ctx.exception))

    def test_removal_deletes_credential_material(self):
        state = build_isolated_auth_state(source_path=self.source)
        self.assertTrue(state.state_file.exists())
        self.assertTrue(remove_isolated_auth_state(state))
        self.assertFalse(state.state_dir.exists())

    def test_remove_of_none_is_safe(self):
        self.assertFalse(remove_isolated_auth_state(None))


class _FakeHttpOrchestrator:
    """loopback 위의 최소 fake orchestrator HTTP 서버 (healthz/thread/stop)."""

    def __init__(self) -> None:
        import http.server
        import threading as _t

        self.requests: list[tuple[str, str, bytes]] = []
        self.launch_ids: list[str | None] = []
        self.status_override: dict[str, int] = {}
        self.raw_override: dict[str, bytes] = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _respond(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append((self.command, self.path, body))
                outer.launch_ids.append(self.headers.get("x-freebuff-launch-id"))
                status = outer.status_override.get(self.path, 200)
                if self.path in outer.raw_override:
                    payload = outer.raw_override[self.path]
                    ctype = "text/plain"
                else:
                    payload = json.dumps({"ok": True, "path": self.path}).encode("utf-8")
                    ctype = "application/json"
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _respond
            do_POST = _respond

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = _t.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def _install_fake_bun(tmp: Path, source: str) -> Path:
    return tmp / "unused.py"


class _InstallLayout:
    """Freebuff 설치 디렉터리 모양의 fake.

    manager는 `[bun, orchestrator.js]`를 cwd=orchestrator dir로 실행한다. 그래서
    fake bun은 실제 Python interpreter로, orchestrator.js는 실행할 python source로
    둔다 — child가 진짜 프로세스로 기동/응답/종료하므로 launch/death 경로가
    stub이 아닌 실제 subprocess로 검증된다.
    """

    def __init__(self, tmp: Path, child_source: str) -> None:
        self.home = tmp / "fb-home"
        self.orch_dir = self.home / "resources" / "orchestrator"
        self.orch_dir.mkdir(parents=True, exist_ok=True)
        (self.orch_dir / "orchestrator.js").write_text(child_source, encoding="utf-8")
        bun_dir = self.home / "resources" / "bun"
        bun_dir.mkdir(parents=True, exist_ok=True)
        (bun_dir / "bun.exe").write_bytes(b"MZ fake")
        self._saved = os.environ.get("FREEBUFF_BUN_PATH")
        os.environ["FREEBUFF_BUN_PATH"] = sys.executable


class ManagerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-mgr-test-"))
        self.source = _write_fake_user_state(self.tmp)
        self.http = _FakeHttpOrchestrator()
        self.addCleanup(self.http.stop)
        self._saved_test_seams = {
            name: os.environ.get(name)
            for name in ("JARVIS_FREEBUFF_ORCHESTRATOR_BASE_URL", "JARVIS_FREEBUFF_LAUNCH_ID")
        }
        os.environ["JARVIS_FREEBUFF_ORCHESTRATOR_BASE_URL"] = f"http://127.0.0.1:{self.http.port}"
        os.environ["JARVIS_FREEBUFF_LAUNCH_ID"] = "legacy-test-launch"
        self.addCleanup(self._restore_test_seams)

    def _restore_test_seams(self) -> None:
        for name, value in self._saved_test_seams.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _manager(self, install_home: Path | None = None, **kwargs) -> FreebuffOrchestratorManager:
        options = {"startup_timeout_s": 30.0}
        options.update(kwargs)
        if install_home is not None:
            options["install_home"] = install_home
        options.setdefault("_test_python_child", True)
        return FreebuffOrchestratorManager(
            user_state_path=self.source,
            log_dir=self.tmp / "logs",
            **options,
        )

    def _attached_manager(self, base_url: str | None = None, pid: int = 1, alive: bool = True):
        manager = self._manager()
        self.addCleanup(manager.cleanup)
        _stub_handle(manager, base_url or f"http://127.0.0.1:{self.http.port}")
        manager._proc = _StubProcess(pid=pid, alive=alive)
        return manager

    def test_ensure_running_reuses_one_process_across_calls(self):
        layout = _InstallLayout(self.tmp, _fake_child_source())
        self.addCleanup(self._restore_bun_env, layout)
        manager = self._manager(install_home=layout.home)
        self.addCleanup(manager.cleanup)
        first = manager.ensure_running()
        second = manager.ensure_running()
        self.assertEqual(first.pid, second.pid)
        self.assertEqual(first.launch_id, second.launch_id)
        self.assertEqual(manager.describe()["running"], True)
        # launch-id는 manager가 만든 것이고 renderer가 정할 수 없다.
        self.assertTrue(first.launch_id)
        self.assertEqual(first.capability_probe.get("healthz"), 200)

    def test_ensure_running_forces_ephemeral_port_and_isolated_state_path(self):
        layout = _InstallLayout(self.tmp, _fake_child_source())
        self.addCleanup(self._restore_bun_env, layout)
        os.environ["FREEBUFF_DESKTOP_UI_ORIGIN"] = "http://renderer-injected"
        self.addCleanup(os.environ.pop, "FREEBUFF_DESKTOP_UI_ORIGIN", None)
        manager = self._manager(install_home=layout.home)
        self.addCleanup(manager.cleanup)
        handle = manager.ensure_running()
        self.assertGreater(handle.port, 0)
        # 격리 state는 manager 소유 temp dir 안이어야 한다.
        self.assertTrue(handle.state_dir.startswith(str(Path(manager._auth_state.state_dir))))
        self.assertNotIn("renderer-injected", handle.state_dir)

    def test_launch_id_is_sent_on_every_request(self):
        manager = self._attached_manager()
        manager._handle.launch_id = "launch-abc"
        manager.request("/api/thread/x")
        manager.request("/api/thread/y", method="POST", body={"text": "hi"})
        self.assertEqual(self.http.launch_ids, ["launch-abc", "launch-abc"])
        self.assertEqual([r[0] for r in self.http.requests], ["GET", "POST"])
        self.assertEqual(json.loads(self.http.requests[1][2]), {"text": "hi"})

    def test_transport_rejects_non_loopback_base_url(self):
        manager = self._attached_manager()
        self.http.requests.clear()
        with self.assertRaises(FreebuffOrchestratorError):
            manager._http("http://example.com", "l", "/api/thread/x")
        self.assertEqual(self.http.requests, [])

    def test_request_rejects_absolute_url_paths(self):
        manager = self._attached_manager()
        self.http.requests.clear()
        with self.assertRaises(FreebuffOrchestratorError):
            manager.request("http://evil.example/steal")
        self.assertEqual(self.http.requests, [])

    def test_request_requires_running_process(self):
        manager = self._manager()
        self.addCleanup(manager.cleanup)
        with self.assertRaises(OrchestratorUnavailable):
            manager.request("/healthz")

    def test_malformed_json_response_degrades_instead_of_raising(self):
        self.http.raw_override["/api/thread/x"] = b"<html>not json</html>"
        manager = self._attached_manager()
        status, payload = manager.request("/api/thread/x")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, str)
        self.assertIn("not json", payload)

    def test_http_error_status_is_returned_not_raised(self):
        self.http.status_override["/api/thread/missing"] = 404
        manager = self._attached_manager()
        status, payload = manager.request("/api/thread/missing")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"ok": True, "path": "/api/thread/missing"})

    def test_health_reports_unavailable_when_child_died(self):
        manager = self._attached_manager(pid=2, alive=False)
        report = manager.health()
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["running"], False)
        self.assertIn("종료", report["detail"])

    def test_health_reports_available_on_200(self):
        manager = self._attached_manager(pid=3)
        report = manager.health()
        self.assertEqual(report["status"], "available")
        self.assertEqual(report["healthz"], 200)

    def test_unexpected_child_death_is_detected_and_restartable(self):
        layout = _InstallLayout(self.tmp, _fake_child_source(exit_after_s=0.6))
        self.addCleanup(self._restore_bun_env, layout)
        manager = self._manager(install_home=layout.home)
        self.addCleanup(manager.cleanup)
        handle = manager.ensure_running()
        deadline = time.monotonic() + 15
        while manager._proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(manager._proc.poll(), "fake child가 종료되어야 한다")
        report = manager.health()
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["running"], False)
        # 죽은 프로세스를 정직하게 치운 뒤 새 프로세스로 재시작할 수 있다.
        handle2 = manager.ensure_running()
        self.assertNotEqual(handle2.pid, handle.pid)
        self.assertEqual(manager.describe()["running"], True)

    def test_missing_install_is_reported_as_unavailable(self):
        self.assertNotIn("FREEBUFF_BUN_PATH", os.environ)
        manager = FreebuffOrchestratorManager(
            user_state_path=self.source, install_home=self.tmp / "nope"
        )
        self.addCleanup(manager.cleanup)
        with self.assertRaises(OrchestratorUnavailable):
            manager.ensure_running()

    def test_no_auth_refuses_to_launch_rather_than_running_anonymous(self):
        noauth_dir = self.tmp / "noauth"
        noauth_dir.mkdir(exist_ok=True)
        source = _write_fake_user_state(noauth_dir, with_auth=False)
        layout = _InstallLayout(self.tmp, _fake_child_source())
        self.addCleanup(self._restore_bun_env, layout)
        manager = FreebuffOrchestratorManager(
            user_state_path=source, install_home=layout.home, _test_python_child=True
        )
        self.addCleanup(manager.cleanup)
        with self.assertRaises(OrchestratorUnavailable) as ctx:
            manager.ensure_running()
        self.assertIn("로그인", str(ctx.exception))
        self.assertIsNone(manager._proc)

    def test_stop_kills_child_and_keeps_auth_state_for_reuse(self):
        layout = _InstallLayout(self.tmp, _fake_child_source())
        self.addCleanup(self._restore_bun_env, layout)
        manager = self._manager(install_home=layout.home)
        handle = manager.ensure_running()
        self.assertTrue(manager.stop())
        self.assertEqual(manager.describe()["running"], False)
        self.assertIsNone(manager._proc)
        self.assertIsNone(manager._handle)
        self.assertIsNotNone(handle.pid)
        # stop 후에도 credential state는 남아 재시작에 재사용된다.
        self.assertIsNotNone(manager._auth_state)
        self.assertTrue(manager._auth_state.has_auth)

    def test_cleanup_removes_process_and_credential_state(self):
        layout = _InstallLayout(self.tmp, _fake_child_source())
        self.addCleanup(self._restore_bun_env, layout)
        manager = self._manager(install_home=layout.home)
        manager.ensure_running()
        state_dir = manager._auth_state.state_dir
        result = manager.cleanup()
        self.assertTrue(result["process_stopped"])
        self.assertTrue(result["auth_state_removed"])
        self.assertFalse(state_dir.exists())
        self.assertIsNone(manager._auth_state)
        with self.assertRaises(OrchestratorUnavailable):
            manager.ensure_running()

    def test_readiness_must_match_launch_id_and_pid(self):
        """ready 줄의 launchId/pid가 다르면 신뢰하지 않는다."""
        source = '''
import json, os, time
print("[orchestrator-ready] " + json.dumps({
    "launchId": "someone-elses-launch",
    "pid": os.getpid(),
    "port": 8799,
}), flush=True)
time.sleep(600)
'''
        layout = _InstallLayout(self.tmp, source)
        self.addCleanup(self._restore_bun_env, layout)
        manager = self._manager(install_home=layout.home, startup_timeout_s=2.0)
        self.addCleanup(manager.cleanup)
        with self.assertRaises(OrchestratorUnavailable):
            manager.ensure_running()
        self.assertIsNone(manager._handle)

    def test_env_forces_ephemeral_port_and_strips_renderer_injectable_surface(self):
        env_dir = self.tmp / "env"
        env_dir.mkdir(exist_ok=True)
        source = _write_fake_user_state(env_dir, with_auth=True)
        for forbidden in (
            "FREEBUFF_DESKTOP_UI_ORIGIN", "FREEBUFF_CDP_BRIDGE_TOKEN",
            "FREEBUFF_MCP_CONSENT_TOKEN", "FREEBUFF_SIGNING_STATE",
            "FREEBUFF_SHELL_LIFETIME_PORT",
        ):
            os.environ[forbidden] = "renderer-injected"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in (
            "FREEBUFF_DESKTOP_UI_ORIGIN", "FREEBUFF_CDP_BRIDGE_TOKEN",
            "FREEBUFF_MCP_CONSENT_TOKEN", "FREEBUFF_SIGNING_STATE",
            "FREEBUFF_SHELL_LIFETIME_PORT")])
        state = IsolatedAuthState(
            state_dir=self.tmp, state_file=source,
            source_path=source, has_auth=True,
        )
        env = FreebuffOrchestratorManager()._build_env("LID", state.state_file, "0.0.151")
        self.assertEqual(env["PORT"], "0")
        self.assertEqual(env["FREEBUFF_LAUNCH_ID"], "LID")
        self.assertEqual(env["FREEBUFF_DESKTOP_STATE_PATH"], str(source))
        for forbidden in (
            "FREEBUFF_DESKTOP_UI_ORIGIN", "FREEBUFF_CDP_BRIDGE_TOKEN",
            "FREEBUFF_MCP_CONSENT_TOKEN", "FREEBUFF_SIGNING_STATE",
            "FREEBUFF_SHELL_LIFETIME_PORT",
        ):
            self.assertNotIn(forbidden, env)

    def _restore_bun_env(self, layout: "_InstallLayout") -> None:
        if layout._saved is None:
            os.environ.pop("FREEBUFF_BUN_PATH", None)
        else:
            os.environ["FREEBUFF_BUN_PATH"] = layout._saved


if __name__ == "__main__":
    unittest.main()
