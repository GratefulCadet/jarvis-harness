"""Freebuff Desktop orchestrator 수명 주기/격리 계층 — JARVIS 전용 내부 구현.

역할 경계 (마일스톤 책임 경계):
- harness/agent_runs.py    : generic AgentRun/store/runner contract + registry.
                             Freebuff API/auth/process 코드는 여기에 넣지 않는다.
- harness/freebuff_orchestrator.py (이 파일): auth-state 격리, Freebuff 프로세스
                             lifecycle, launch-id/port 추적, loopback HTTP transport,
                             cleanup. renderer에게 노출되는 API surface는 없다.
- harness/freebuff_runner.py: AgentSession 기반 실행/완료/result/cancel 판단.

empirical로 확정된 Freebuff Desktop 내부 계약 (재발굴하지 않는다):
- 실행: packaged `resources/bun/bun.exe` + `resources/orchestrator/orchestrator.js`,
  cwd=orchestrator dir. `FREEBUFF_BUN_PATH`가 있으면 그 바이너리를 쓴다.
- env: PORT=0(ephemeral), FREEBUFF_LAUNCH_ID, FREEBUFF_DESKTOP_STATE_PATH.
  FREEBUFF_SHELL_LIFETIME_PORT는 Electron 전용이며 **없어도** 동작한다
  (`SHELL_MANAGED = process.env.FREEBUFF_SHELL_LIFETIME_PORT !== undefined`).
- readiness: stdout에 `[orchestrator-ready] {json}` 한 줄. launchId/pid/port가
  모두 기대값과 일치할 때만 신뢰한다(다른 프로세스의.ready 줄 무시).
- 모든 /api 요청은 `x-freebuff-launch-id: <launchId>` 헤더가 필요하다.
- state.json: `authSessions["https://www.codebuff.com"] = {token, user}`.
  사용자 파일의 다른 키(recentProjects/uiPrefs/tabs/...)는 절대 옮기지 않는다.

보안 원칙 (이 파일의 존재 이유):
- 사용자 state.json은 read-only로 파싱만 한다. 쓰기/이동/hardlink/symlink 금지.
- 필요한 auth entry 하나만 추출해 JARVIS 소유 private temp state.json으로 만든다.
- token은 로그/예외/trace/반환값에 절대 나타나지 않는다. 예외 문자열은 정화한다.
- transport는 loopback만. renderer/model이 URL·header·launch-id를 지정할 수 없다.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from harness.freebuff_client import VERIFIED_FREEBUFF_VERSION, detect_install
from harness.freebuff_client import _detect_version as _detect_installed_version

# Freebuff가 읽는 prod auth host (orchestrator의 PROD_API_HOST와 동일).
FREEBUFF_AUTH_HOST = "https://www.codebuff.com"
# Compatibility alias consumed by FreebuffRunner and its loopback test transport.
AUTH_HOST = FREEBUFF_AUTH_HOST

# readiness를 기다리는 기본 한도. Freebuff 기동은 수 초 걸리지만, 크래시 루프에서
# bridge 스레드를 붙잡고 있지 않도록 유한하게 둔다.
DEFAULT_STARTUP_TIMEOUT_S = 90.0
DEFAULT_REQUEST_TIMEOUT_S = 15.0

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# orchestrator 자식이 console에 남기는 readiness 표식.
_READY_PREFIX = "[orchestrator-ready] "


class FreebuffOrchestratorError(RuntimeError):
    """orchestrator 계층 하드 실패 — bridge가 status=error로 되돌려준다."""


class OrchestratorUnavailable(FreebuffOrchestratorError):
    """지금은 쓸 수 없다(미설치/죽음/비-healthz). '일시 가용'과 '영구 실패'를 구분."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_detail(value: Any, limit: int = 300) -> str:
    """에러/상태 문자열에서 credential 형태를 제거한다.

    token은 예외 문자열·log·bridge 응답·run metadata 어디에도 남으면 안 된다.
    원본 예외를 그대로 노출하지 않고, 모양만 남긴다.
    """
    text = str(value or "")
    # JSON-ish 또는 header-ish 형태의 긴 secret을 generic描述으로 대체한다.
    redacted = []
    for token_str in text.replace('"', " ").replace("'", " ").replace(",", " ").split():
        if len(token_str) >= 24 and _looks_secret(token_str):
            redacted.append("<redacted>")
        else:
            redacted.append(token_str)
    out = " ".join(redacted)
    return out[:limit] if len(out) > limit else out


def _looks_secret(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in ("eyj", "eyjh", "sk-", "bearer")):
        return True
    # hex/base64가 그 길이면 JWT payload/base64 세그먼트로 본다.
    if all(c.isalnum() or c in "-_=." for c in value):
        return True
    return False


def _is_loopback_url(url: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def validate_relative_path(path: str) -> str:
    """orchestrator 요청 경로는 반드시 relative이어야 한다.

    renderer/model이 절대 URL이나 다른 host를 지정해 사용자 브라우저 API를
    호출하거나 token을 엉뚱한 곳으로 흘리는 것을 구조적으로 막는다.
    """
    if not isinstance(path, str) or not path.strip():
        raise FreebuffOrchestratorError("orchestrator 경로가 비어 있습니다")
    candidate = path.strip()
    if "://" in candidate or candidate.startswith("//"):
        raise FreebuffOrchestratorError("외부 endpoint URL은 사용할 수 없습니다 (loopback만 허용)")
    if not candidate.startswith("/"):
        candidate = "/" + candidate
    if "\\" in candidate:
        raise FreebuffOrchestratorError("orchestrator 경로에 역슬래시를 쓸 수 없습니다")
    if ".." in candidate.split("/"):
        raise FreebuffOrchestratorError("orchestrator 경로에 상위 디렉터리 이동이 포함될 수 없습니다")
    if "\n" in candidate or "\r" in candidate or "\x00" in candidate:
        raise FreebuffOrchestratorError("orchestrator 경로에 제어 문자가 포함될 수 없습니다")
    return candidate


# ---------- auth-state 격리 ----------

@dataclass
class IsolatedAuthState:
    """JARVIS 소유 private temp state. token은 이 객체 밖으로 나가지 않는다."""

    state_dir: Path
    state_file: Path
    source_path: Path
    auth_host: str = FREEBUFF_AUTH_HOST
    has_auth: bool = False
    created_at: str = field(default_factory=_now_iso)

    def as_dict(self) -> dict[str, Any]:
        """bridge/run metadata용 — token을 절대 포함하지 않는다."""
        return {
            "state_dir": str(self.state_dir),
            "source": "user-state-copy" if self.has_auth else "none",
            "auth_host": self.auth_host,
            "has_auth": self.has_auth,
            "created_at": self.created_at,
        }


def default_user_state_path() -> Path:
    """Freebuff Desktop의 사용자 state.json (읽기 전용 소스).

    override: JARVIS_FREEBUFF_USER_STATE_PATH
    """
    override = os.environ.get("JARVIS_FREEBUFF_USER_STATE_PATH", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".config" / "freebuff-desktop" / "state.json"


def _restrict_permissions(path: Path, mode: int) -> None:
    """가능한 경우 현재 사용자만 접근 가능하게 만든다 (Windows는 best-effort)."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def build_isolated_auth_state(
    *,
    source_path: Path | str | None = None,
    auth_host: str = FREEBUFF_AUTH_HOST,
    state_dir: Path | str | None = None,
) -> IsolatedAuthState:
    """사용자 state.json → minimal private state.json.

    허용되는 것: authSessions[auth_host]의 필요한 entry만 추출해 새 파일로 기록.
    금지되는 것: hardlink/symlink, 전체 복사, recentProjects/tabs/uiPrefs 복사,
    사용자 state 수정.

    token은 이 함수 안에서만 다룬다 — 예외 메시지에는 절대 넣지 않는다.
    """
    source = Path(source_path) if source_path is not None else default_user_state_path()
    if state_dir is None:
        # JARVIS 소유 private temp dir. repo/workspace 밖(시스템 temp)이어야 한다.
        state_dir = Path(tempfile.mkdtemp(prefix="jarvis-freebuff-state-"))
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _restrict_permissions(directory, stat.S_IRWXU)  # 0o700

    state_file = directory / "state.json"
    entry: dict[str, Any] | None = None
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = None
    except (OSError, json.JSONDecodeError) as exc:
        raise FreebuffOrchestratorError(
            f"Freebuff 사용자 state.json을 읽을 수 없습니다 ({sanitize_detail(exc)})"
        ) from None
    if isinstance(raw, dict):
        sessions = raw.get("authSessions")
        if isinstance(sessions, dict):
            candidate = sessions.get(auth_host)
            if isinstance(candidate, dict):
                # 필요한 entry만 — authSession 스키마는 {token, user}.
                entry = {
                    key: candidate[key]
                    for key in ("token", "user")
                    if key in candidate and candidate[key] is not None
                }
                token = entry.get("token")
                if not isinstance(token, str) or not token.strip():
                    entry = None

    payload: dict[str, Any] = {}
    if entry:
        # 다른 모든 키(recentProjects, uiPrefs, composerDrafts, ...)는 의도적으로
        # 비워둔다 — 사용자 workspace 목록이 격리본에 새는 것을 막는다.
        payload["authSessions"] = {auth_host: entry}
    _restrict_permissions(state_file.parent, stat.S_IRWXU)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix="state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        os.replace(tmp_name, state_file)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return IsolatedAuthState(
        state_dir=directory,
        state_file=state_file,
        source_path=source,
        auth_host=auth_host,
        has_auth=bool(entry),
    )


def remove_isolated_auth_state(state: IsolatedAuthState | None) -> bool:
    """credential을 담은 temp state를 삭제한다. 실패해도 bridge shutdown을 막지 않는다."""
    if state is None:
        return False
    removed = False
    try:
        if state.state_dir.is_dir():
            shutil.rmtree(state.state_dir, ignore_errors=True)
            removed = not state.state_dir.exists()
    except OSError:
        removed = False
    return removed


# ---------- launch handle ----------

@dataclass
class OrchestratorHandle:
    """살아 있는 orchestrator 1개의 관측 상태. token은 포함하지 않는다."""

    pid: int
    port: int
    launch_id: str
    state_dir: str
    started_at: str
    detected_version: str | None = None
    verified_contract_version: str = VERIFIED_FREEBUFF_VERSION
    capability_probe: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "port": self.port,
            "base_url": self.base_url,
            "state_dir": self.state_dir,
            "started_at": self.started_at,
            "detected_version": self.detected_version,
            "verified_contract_version": self.verified_contract_version,
            "capability_probe": dict(self.capability_probe),
        }


def _terminate_process(proc: subprocess.Popen, *, timeout_s: float = 10.0) -> int | None:
    """orchestrator 프로세스 트리를 정리한다.

    Windows는 taskkill /T로 bun 아래 harness 자식까지, POSIX는 프로세스 그룹에
    SIGTERM. UI만 cancelled로 바꾸고 자식을 남겨두면 안 된다.
    """
    if proc.poll() is not None:
        return proc.returncode
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            return proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None


# ---------- manager ----------

class FreebuffOrchestratorManager:
    """bridge 프로세스당 orchestrator 하나를 띄우고 여러 run에서 재사용한다.

    계약:
    - ensure_running() : 없으면 기동, 있으면 재사용. 기동 중이면 대기.
    - health()         : 현재 가용성 + 추적 메타. 죽은 자식은 정직하게 보고.
    - request()        : relative path만 받는 loopback JSON transport.
    - stop()           : 프로세스 정리(credential temp state는 cleanup이 담당).
    - cleanup()        : 프로세스 + credential temp state 전부 제거.
    """

    def __init__(
        self,
        *,
        install_home: Path | str | None = None,
        user_state_path: Path | str | None = None,
        startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        log_dir: Path | str | None = None,
        auth_host: str = FREEBUFF_AUTH_HOST,
        _test_base_url: str | None = None,
        _test_launch_id: str | None = None,
        _test_state_dir: Path | str | None = None,
        _test_python_child: bool = False,
    ) -> None:
        self.startup_timeout_s = float(startup_timeout_s)
        self.request_timeout_s = float(request_timeout_s)
        self.auth_host = auth_host
        self._install_home = Path(install_home) if install_home else None
        self._user_state_path = (
            Path(user_state_path) if user_state_path else default_user_state_path()
        )
        self._log_dir = Path(log_dir) if log_dir else None
        self._temp_root = Path(tempfile.gettempdir())
        self._test_base_url = _test_base_url
        self._test_launch_id = _test_launch_id or f"test-{uuid.uuid4()}"
        self._test_state_dir = Path(_test_state_dir) if _test_state_dir is not None else None
        self._test_python_child = bool(_test_python_child)
        if self._test_base_url is not None and not _is_loopback_url(self._test_base_url):
            raise FreebuffOrchestratorError("테스트 endpoint도 loopback만 허용됩니다")
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._output_queue: queue.Queue[Any] | None = None
        self._output_thread: threading.Thread | None = None
        self._handle: OrchestratorHandle | None = None
        self._auth_state: IsolatedAuthState | None = None
        self._log_handle = None
        self._stopped = False

    # ---- 관측 ----

    def describe(self) -> dict[str, Any]:
        """UI/활동 로그용 — credential이 절대 없는 스냅샷."""
        with self._lock:
            alive = self._proc is not None and self._proc.poll() is None
            return {
                "running": alive,
                "pid": self._handle.pid if self._handle else None,
                "port": self._handle.port if self._handle else None,
                "launch_id_present": bool(self._handle and self._handle.launch_id),
                "state_dir": str(self._auth_state.state_dir) if self._auth_state else None,
                "started_at": self._handle.started_at if self._handle else None,
                "detected_version": self._handle.detected_version if self._handle else None,
                "verified_contract_version": VERIFIED_FREEBUFF_VERSION,
                "has_auth": bool(self._auth_state and self._auth_state.has_auth),
            }

    # ---- launch ----

    def _resolve_binaries(self) -> tuple[Path, Path, Path]:
        """(bun, orchestrator_js, orchestrator_dir). 미설치면 정직하게 실패."""
        install = detect_install()
        home = self._install_home or Path(install["home"])
        orchestrator_dir = home / "resources" / "orchestrator"
        orchestrator_js = orchestrator_dir / "orchestrator.js"
        override = os.environ.get("FREEBUFF_BUN_PATH", "").strip()
        if override:
            bun = Path(os.path.expanduser(override))
        else:
            bun = home / "resources" / "bun" / "bun.exe"
            if not bun.is_file():
                alt = home / "resources" / "bun" / "bun"
                if alt.is_file():
                    bun = alt
        if not orchestrator_js.is_file():
            raise OrchestratorUnavailable(
                f"Freebuff Desktop orchestrator를 찾을 수 없습니다 ({sanitize_detail(orchestrator_dir)})"
            )
        if not bun.is_file():
            raise OrchestratorUnavailable(
                f"Freebuff Desktop Bun runtime을 찾을 수 없습니다 ({sanitize_detail(bun)})"
            )
        return bun, orchestrator_js, orchestrator_dir

    def _build_env(self, launch_id: str, state_file: Path, detected_version: str | None) -> dict[str, str]:
        env = dict(os.environ)
        env["PORT"] = "0"  # ephemeral — 사용자가 열려 있는 포트를 건드리지 않는다
        env["FREEBUFF_LAUNCH_ID"] = launch_id
        env["FREEBUFF_DESKTOP_STATE_PATH"] = str(state_file)
        env["FREEBUFF_PROFILE_LOCK_WAIT_MS"] = "10000"
        if detected_version:
            env["FREEBUFF_APP_VERSION"] = detected_version
        # renderer/model이 주입할 수 있는 식별/브리지 surface를 명시적으로 제거한다.
        for forbidden in (
            "FREEBUFF_DESKTOP_UI_ORIGIN",
            "FREEBUFF_CDP_BRIDGE_PORT",
            "FREEBUFF_CDP_BRIDGE_TOKEN",
            "FREEBUFF_MCP_CONSENT_PORT",
            "FREEBUFF_MCP_CONSENT_TOKEN",
            "FREEBUFF_SIGNING_STATE",
            "FREEBUFF_SIGNING_TEAM",
            "FREEBUFF_SHELL_LIFETIME_PORT",
            "FREEBUFF_BUN_PATH",
            "JARVIS_FREEBUFF_ORCHESTRATOR_BASE_URL",
            "JARVIS_FREEBUFF_LAUNCH_ID",
        ):
            env.pop(forbidden, None)
        return env

    def _launch(
        self,
        bun: Path,
        orchestrator_js: Path,
        orchestrator_dir: Path,
        env: dict[str, str],
    ) -> tuple[subprocess.Popen, dict[str, Any]]:
        log_path = None
        log_handle = None
        if self._log_dir is not None:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self._log_dir / "freebuff-orchestrator.log"
            log_handle = log_path.open("a", encoding="utf-8", errors="replace")
            self._log_handle = log_handle
        try:
            command = [str(bun), str(orchestrator_js)]
            if Path(bun).resolve() == Path(sys.executable).resolve():
                command = [str(bun), "-u", str(orchestrator_js)]
            proc = subprocess.Popen(
                command,
                cwd=str(orchestrator_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                   else {"start_new_session": True}),
            )
        except OSError as exc:
            self._close_log()
            raise OrchestratorUnavailable(
                f"Freebuff orchestrator를 실행할 수 없습니다 ({sanitize_detail(exc)})"
            ) from None
        self._output_queue = queue.Queue()
        self._output_thread = None
        if proc.stdout is not None:
            self._output_thread = self._start_output_pump(proc.stdout, log_handle, self._output_queue)
        return proc, {"log_path": str(log_path) if log_path else None}

    @staticmethod
    def _start_output_pump(stream: Any, sink: Any, lines: queue.Queue[Any]) -> threading.Thread:
        def pump() -> None:
            try:
                for chunk in iter(lambda: stream.readline(), b""):
                    if not chunk:
                        break
                    line = chunk.decode("utf-8", "replace")
                    if sink is not None:
                        try:
                            sink.write(line)
                            sink.flush()
                        except (OSError, ValueError):
                            pass
                    lines.put(line)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)

        thread = threading.Thread(target=pump, daemon=True, name="freebuff-orchestrator-output")
        thread.start()
        return thread

    def _await_ready(
        self, proc: subprocess.Popen, launch_id: str, stdout: Any
    ) -> dict[str, Any]:
        """`[orchestrator-ready] {json}` 한 줄을 기다린다 (pid/launchId 검증 필수)."""
        deadline = time.monotonic() + self.startup_timeout_s
        if stdout is None:
            raise OrchestratorUnavailable(
                "orchestrator readiness를 관찰할 수 없습니다 (stdout 없음)"
            )
        # stdout.readline()은 출력이 없으면 무한정 블로킹해 기동 timeout을 지난다.
        # 읽기는 tee thread가 하고, 여기서는 queue를 deadline까지 폴링한다.
        lines = self._output_queue
        if lines is None:
            raise OrchestratorUnavailable(
                "orchestrator readiness를 관찰할 수 없습니다 (stdout 없음)"
            )

        buffered = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise OrchestratorUnavailable(
                    "Freebuff orchestrator가 준비되기 전에 종료되었습니다 "
                    f"(exit {proc.returncode})"
                )
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:  # EOF — 프로세스가 조용히 죽었다
                if proc.poll() is not None:
                    raise OrchestratorUnavailable(
                        "Freebuff orchestrator가 준비되기 전에 종료되었습니다 "
                        f"(exit {proc.returncode})"
                    )
                continue
            buffered = (buffered + line)[-8192:]
            for candidate in buffered.splitlines():
                candidate = candidate.strip()
                if not candidate.startswith(_READY_PREFIX):
                    continue
                try:
                    announcement = json.loads(candidate[len(_READY_PREFIX):])
                except json.JSONDecodeError:
                    continue
                if not isinstance(announcement, dict):
                    continue
                if announcement.get("launchId") != launch_id:
                    continue  # 다른 프로세스의 ready 줄
                if announcement.get("pid") != proc.pid:
                    continue
                port = announcement.get("port")
                if not isinstance(port, int) or not (0 < port < 65536):
                    continue
                return announcement
        raise OrchestratorUnavailable(
            f"Freebuff orchestrator가 {int(self.startup_timeout_s)}초 안에 준비되지 않았습니다"
        )

    def _wait_for_server(self, handle: OrchestratorHandle, proc: subprocess.Popen) -> None:
        """ready 표식만 믿지 않고 실제 /healthz 응답까지 확인한다."""
        deadline = time.monotonic() + self.startup_timeout_s
        last_detail = "healthz 응답 없음"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise OrchestratorUnavailable(
                    f"Freebuff orchestrator가 healthz 이전에 종료되었습니다 (exit {proc.returncode})"
                )
            try:
                status, _ = self._http(
                    handle.base_url, handle.launch_id, "/healthz",
                    method="GET", timeout_s=2.0,
                )
            except FreebuffOrchestratorError as exc:
                last_detail = sanitize_detail(exc)
                time.sleep(0.25)
                continue
            if status == 200:
                return
            last_detail = f"healthz가 {status}를 반환했습니다"
            time.sleep(0.25)
        raise OrchestratorUnavailable(
            f"Freebuff orchestrator가 응답하지 않습니다 ({last_detail})"
        )

    def ensure_running(self) -> OrchestratorHandle:
        """orchestrator를 기동하거나 이미 있는 것을 재사용한다.

        여러 run이 동시에 호출해도 프로세스는 하나만 생긴다.
        """
        with self._lock:
            if self._stopped:
                raise OrchestratorUnavailable(
                    "orchestrator manager가 정리되었습니다 — 새 브리지 프로세스가 필요합니다"
                )
            if self._proc is not None and self._proc.poll() is None and self._handle is not None:
                return self._handle
            if self._test_base_url is not None:
                if self._test_state_dir is not None and self._auth_state is None:
                    self._auth_state = build_isolated_auth_state(
                        source_path=self._user_state_path,
                        auth_host=self.auth_host,
                        state_dir=self._test_state_dir,
                    )
                self._proc = _InjectedProcess()
                port = int(self._test_base_url.rsplit(":", 1)[1])
                self._handle = OrchestratorHandle(
                    pid=0,
                    port=port,
                    launch_id=self._test_launch_id,
                    state_dir=str(self._auth_state.state_dir) if self._auth_state else "<test-transport>",
                    started_at=_now_iso(),
                    capability_probe={"healthz": True},
                )
                return self._handle
            # 죽은 자식/이전 실패 상태를 정직하게 치운다.
            if self._proc is not None:
                self._mark_child_dead()
            if self._auth_state is None:
                self._auth_state = build_isolated_auth_state(
                    source_path=self._user_state_path, auth_host=self.auth_host
                )
            if not self._auth_state.has_auth:
                remove_isolated_auth_state(self._auth_state)
                self._auth_state = None
                raise OrchestratorUnavailable(
                    "Freebuff 로그인 세션을 찾을 수 없습니다 — Freebuff Desktop에서 로그인해 주세요"
                )
            try:
                bun, orchestrator_js, orchestrator_dir = self._resolve_binaries()
            except FreebuffOrchestratorError:
                remove_isolated_auth_state(self._auth_state)
                self._auth_state = None
                raise
            launch_id = str(uuid.uuid4())
            install = detect_install()
            detected_version = _detect_installed_version(install)
            env = self._build_env(launch_id, self._auth_state.state_file, detected_version)
            proc, _meta = self._launch(bun, orchestrator_js, orchestrator_dir, env)
            self._proc = proc
            try:
                announcement = self._await_ready(proc, launch_id, proc.stdout)
            except FreebuffOrchestratorError:
                _terminate_process(proc)
                self._discard_process_state()
                raise
            handle = OrchestratorHandle(
                pid=proc.pid,
                port=int(announcement["port"]),
                launch_id=launch_id,
                state_dir=str(self._auth_state.state_dir) if self._auth_state else "<test-transport>",
                started_at=_now_iso(),
                detected_version=detected_version,
            )
            self._handle = handle
            try:
                self._wait_for_server(handle, proc)
            except FreebuffOrchestratorError:
                _terminate_process(proc)
                self._discard_process_state()
                raise
            handle.capability_probe = self._probe_capabilities(handle)
            return handle

    def _probe_capabilities(self, handle: OrchestratorHandle) -> dict[str, Any]:
        """검증된 계약 버전에 대한 capability probe (healthz + auth/status).

        probe 성공이 버전 문자열보다 우선이다 — 미탐지 버전이어도 실제로
        healthz/auth가 응답하면 그 사실을 그대로 기록한다.
        """
        probe: dict[str, Any] = {"checked_at": _now_iso()}
        try:
            status, _ = self._http(handle.base_url, handle.launch_id, "/healthz", timeout_s=5.0)
            probe["healthz"] = status
        except FreebuffOrchestratorError as exc:
            probe["healthz"] = None
            probe["error"] = sanitize_detail(exc)
            return probe
        try:
            status, payload = self._http(
                handle.base_url, handle.launch_id, "/api/auth/status", timeout_s=8.0
            )
            probe["auth_status_http"] = status
            # authed 여부만 기록한다 — token/user 본문은 여기에 남기지 않는다.
            if isinstance(payload, dict):
                probe["authed"] = bool(payload.get("token") is not None or payload.get("authed"))
            else:
                probe["authed"] = None
        except FreebuffOrchestratorError as exc:
            probe["auth_status_http"] = None
            probe["auth_probe_error"] = sanitize_detail(exc)
        return probe

    # ---- health ----

    def health(self) -> dict[str, Any]:
        """현재 상태를 정직하게 보고한다. 죽은 자식은 'running'으로 가장하지 않는다."""
        child_exited = self._proc is not None and self._proc.poll() is not None
        if child_exited:
            self._mark_child_dead()
        snapshot = self.describe()
        if not snapshot["running"]:
            snapshot["status"] = "unavailable"
            snapshot["detail"] = (
                "Freebuff orchestrator 프로세스가 종료되었습니다"
                if child_exited
                else "Freebuff orchestrator가 실행 중이 아닙니다"
            )
            return snapshot
        try:
            status, _ = self._http(
                self._handle.base_url, self._handle.launch_id, "/healthz", timeout_s=3.0
            )
        except FreebuffOrchestratorError as exc:
            if self._proc is not None and self._proc.poll() is not None:
                self._discard_process_state()
                return {
                    **self.describe(),
                    "status": "unavailable",
                    "detail": "Freebuff orchestrator 프로세스가 예상치 않게 종료되었습니다",
                }
            snapshot["status"] = "unavailable"
            snapshot["detail"] = sanitize_detail(exc)
            return snapshot
        snapshot["healthz"] = status
        snapshot["status"] = "available" if status == 200 else "unavailable"
        if status != 200:
            snapshot["detail"] = f"orchestrator healthz가 {status}를 반환했습니다"
        return snapshot

    # ---- transport ----

    def _http(
        self,
        base_url: str,
        launch_id: str,
        path: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        timeout_s: float | None = None,
    ) -> tuple[int, Any]:
        """loopback JSON transport. launch-id 헤더는 여기서만 부착한다."""
        import urllib.error
        import urllib.request

        if not _is_loopback_url(base_url):
            raise FreebuffOrchestratorError("loopback이 아닌 endpoint는 사용할 수 없습니다")
        safe_path = validate_relative_path(path)
        url = base_url.rstrip("/") + safe_path
        request = urllib.request.Request(url, method=method)
        request.add_header("accept", "application/json")
        request.add_header("x-freebuff-launch-id", launch_id)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(
                request, data=data, timeout=timeout_s or self.request_timeout_s
            ) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read().decode("utf-8", "replace")
            except OSError:
                raw = ""
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise FreebuffOrchestratorError(
                f"orchestrator 연결 실패: {sanitize_detail(exc)}"
            ) from None
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        timeout_s: float | None = None,
    ) -> tuple[int, Any] | Any:
        """기동된 orchestrator에 relative path 요청을 보낸다.

        renderer/model은 path와 constrained body만 줄 수 있다. URL·header·
        launch-id를 지정하는 표면은 이 API에 존재하지 않는다.
        """
        with self._lock:
            if self._test_base_url is not None:
                if self._proc is None or self._proc.poll() is not None or self._handle is None:
                    if self._proc is not None:
                        self._mark_child_dead()
                    raise OrchestratorUnavailable("Freebuff orchestrator가 실행 중이 아닙니다")
                handle = self._handle
            else:
                if self._proc is None or self._proc.poll() is not None or self._handle is None:
                    raise OrchestratorUnavailable("Freebuff orchestrator가 실행 중이 아닙니다")
                handle = self._handle
        base_url = self._test_base_url or handle.base_url
        launch_id = self._test_launch_id if self._test_base_url is not None else handle.launch_id
        return self._http(
            base_url, launch_id, path,
            method=method, body=body, timeout_s=timeout_s,
        )

    def stream_events(self, *, timeout_s: float = 60.0) -> Iterator[dict[str, Any]]:
        """`/api/events` SSE 라인을 파싱해 dict로 yield 한다 (힌트용).

        canonical 소스는 GET thread polling이다 — SSE는 확인을 앞당길 뿐이다.
        """
        import urllib.error
        import urllib.request

        with self._lock:
            if self._proc is None or self._proc.poll() is not None or self._handle is None:
                if self._test_base_url is not None and self._proc is not None:
                    self._mark_child_dead()
                raise OrchestratorUnavailable("Freebuff orchestrator가 실행 중이 아닙니다")
            handle = self._handle
        request = urllib.request.Request(handle.base_url + "/api/events", method="GET")
        request.add_header("accept", "text/event-stream")
        request.add_header("x-freebuff-launch-id", handle.launch_id)
        try:
            response = urllib.request.urlopen(request, timeout=timeout_s)
        except (urllib.error.URLError, OSError) as exc:  # type: ignore[attr-defined]
            raise FreebuffOrchestratorError(
                f"SSE 연결 실패: {sanitize_detail(exc)}"
            ) from None
        deadline = time.monotonic() + timeout_s
        try:
            for raw_line in response:
                if time.monotonic() > deadline:
                    return
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue  # ': ping' heartbeat 등은 무시
                try:
                    event = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
        except (OSError, ValueError):
            return

    # ---- teardown ----

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None

    def _discard_process_state(self) -> None:
        proc = self._proc
        output_thread = self._output_thread
        self._proc = None
        self._output_queue = None
        self._output_thread = None
        self._handle = None
        if output_thread is not None and output_thread is not threading.current_thread():
            output_thread.join(timeout=1.0)
        if proc is not None and getattr(proc, "stdout", None) is not None:
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass
        self._close_log()

    def stop(self) -> bool:
        """orchestrator 프로세스를 정리한다. credential temp state는 남긴다
        (재시작 경로에서 재사용할 수 있으므로)."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return False
            if self._test_base_url is not None:
                proc.dead = True
                self._proc = None
                self._output_queue = None
                self._output_thread = None
                self._handle = None
            else:
                _terminate_process(proc)
                self._discard_process_state()
            return True

    def cleanup(self) -> dict[str, Any]:
        """bridge shutdown 경로 — 프로세스와 credential temp state를 모두 지운다.

        고아로 남은 credential 파일을 남겨두지 않는 것이 목적이다.
        """
        with self._lock:
            stopped = self.stop()
            state_removed = remove_isolated_auth_state(self._auth_state)
            self._auth_state = None
            self._stopped = True
            return {"process_stopped": stopped, "auth_state_removed": state_removed}

    def __enter__(self) -> "FreebuffOrchestratorManager":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.cleanup()

    def _mark_child_dead(self) -> None:
        proc = self._proc
        self._discard_process_state()
        if proc is not None:
            try:
                proc.wait(timeout=0)
            except Exception:
                pass
        if self._auth_state is not None:
            remove_isolated_auth_state(self._auth_state)
            self._auth_state = None


class _InjectedProcess:
    """Liveness sentinel used only for the explicit loopback test transport."""
    pid = 0

    def __init__(self):
        self.dead = False

    def poll(self):
        return 1 if self.dead else None

    def wait(self, timeout=None):
        self.dead = True
        return 0
