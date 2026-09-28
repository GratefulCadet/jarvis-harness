"""Freebuff Desktop orchestrator 클라이언트 — read-only validation 전용.

audit 결론(마일스톤 §0)에 따른다:
- freebuff CLI는 없다. Freebuff Desktop(0.0.151)의 내부 orchestrator가
  loopback HTTP API를 연다. 이 클라이언트는 그중 read-only 경로만 쓴다:
  healthz → project/open → thread GET.
- 절대 하지 않는 것(§20): auth token 읽기/복제, 사용자 state 수정, 메시지
  자동 전송, Freebuff DB 직접 조작. project/open은 orchestrator가 thread를
  재수화(rehydrate)하는 데 필요한 공식 GUI 경로이며, state.json의
  recentProjects 목록에 경로 기록 하나가 남는 것 외에는 관찰 가능한 부작용이
  없다 — 이것이 이 milestone이 허용하는 유일한 쓰기이다.

capability 모델(§10) — success를 가정하지 않는다:
  available        healthz + thread GET 성공
  unavailable      Freebuff 미설치/orchestrator에 연결 불가
  version_mismatch 검출된 버전이 검증된 버전과 다르고 probe도 실패한 상태
  thread_missing   orchestrator는 살아 있으나 thread를 찾지 못함
  awaiting_auth    thread는 있으나 Freebuff 인증이 필요하다는 notice 기록

버전 가드(§11): 감지 버전 / 검증 버전 / probe 결과를 분리한다. probe 성공이
버전 문자열보다 우선 — healthz → project/open → thread GET이 성공하면
usable이다. 설치 위치는 표준 경로에서 탐색하고 JARVIS_FREEBUFF_HOME으로
재정의한다. 테스트는 JARVIS_FREEBUFF_ORCHESTRATOR_BASE_URL로 가짜 서버를
주입한다(실제 Freebuff를 띄우지 않는다).

internal/undocumented contract이므로 모든 파싱은 방어적이다 — 형식이 바뀌면
예외가 아니라 unavailable/unknown capability로 떨어진다(§10 degraded mode).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# audit에서 empirical 검증한 버전. 현재 설치가 이 버전이어야 "검증됨"으로 표시.
VERIFIED_FREEBUFF_VERSION = "0.0.151"

# Windows 표준 설치 위치(audit §1). 다른 위치는 JARVIS_FREEBUFF_HOME으로.
_DEFAULT_INSTALL_DIR = (
    r"C:\Users\USER\AppData\Local\Programs\@codebufffreebuff-desktop"
)

THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# orchestrator가 지원하는 harness — thread의 harnessId가 이 중이면 Freebuff 측
# 위임(자체 codex/claude)으로 해석한다. JARVIS 스키마에는 영향 없다.
KNOWN_HARNESS_IDS = ("codebuff", "claude-code", "codex")


class FreebuffError(RuntimeError):
    """Freebuff 클라이언트 수준 오류 — capability 판정 전의 하드 실패."""


@dataclass
class FreebuffCapability:
    """freebuff_session_validate의 결과. 상태 어휘는 마일스톤 §10을 따른다."""

    status: str  # available | unavailable | version_mismatch | thread_missing | awaiting_auth
    detail: str | None = None
    detected_version: str | None = None
    verified_version: str = VERIFIED_FREEBUFF_VERSION
    thread: dict | None = None  # orchestrator가 반환한 thread metadata (authoritative)
    messages_count: int | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "detected_version": self.detected_version,
            "verified_version": self.verified_version,
            "thread": self.thread,
            "messages_count": self.messages_count,
        }


def detect_install() -> dict:
    """설치 탐지 — 표준 경로에서 exe/bun/orchestrator 존재만 확인 (read-only)."""
    home = Path(__import__("os").environ.get("JARVIS_FREEBUFF_HOME", "").strip()
                or _DEFAULT_INSTALL_DIR)
    exe = home / "Freebuff.exe"
    orchestrator = home / "resources" / "orchestrator" / "orchestrator.js"
    return {
        "installed": exe.is_file() and orchestrator.is_file(),
        "home": str(home),
        "exe": str(exe) if exe.is_file() else None,
        "orchestrator": str(orchestrator) if orchestrator.is_file() else None,
    }


def _request_json(base_url: str, path: str, *, method: str = "GET",
                  body: dict | None = None, token: str | None = None,
                  timeout_s: float = 6.0) -> tuple[int, dict | str | None]:
    url = base_url.rstrip("/") + path
    request = urllib.request.Request(url, method=method)
    request.add_header("accept", "application/json")
    if token:
        request.add_header("x-freebuff-launch-id", token)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, data=data, timeout=timeout_s) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read().decode("utf-8", "replace")
        except OSError:
            raw = ""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise FreebuffError(f"orchestrator 연결 실패: {exc}") from exc
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _extract_thread(payload: dict | str | None) -> dict | None:
    """GET /api/thread/:id 반환은 {thread, messages, items} 형태 — 방어적으로 파싱."""
    if not isinstance(payload, dict):
        return None
    thread = payload.get("thread")
    if isinstance(thread, dict):
        return thread
    # 형태가 바뀌어도 id/model 정도는 최상위에 있으면 받는다.
    if "id" in payload:
        return payload
    return None


def freebuff_orchestrator_base_url() -> str | None:
    """환경에서 orchestrator base URL을 얻는다.

    - JARVIS_FREEBUFF_ORCHESTRATOR_BASE_URL: 테스트 주입 지점(가짜 서버).
    - JARVIS_FREEBUFF_PORT: 실제 실행 중 orchestrator의 포트(앱 로그에서 확인).
    - 기본은 None — 자동 스캔은 하지 않는다(사용자 세션을 몰래 만지는 행위 방지).
    """
    import os

    explicit = os.environ.get("JARVIS_FREEBUFF_ORCHESTRATOR_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("JARVIS_FREEBUFF_PORT", "").strip()
    if port.isdigit() and 0 < int(port) < 65536:
        return f"http://127.0.0.1:{port}"
    return None


def validate_thread(workspace: str, external_session_id: str) -> FreebuffCapability:
    """기존 Freebuff thread의 read-only validation (§9 흐름 그대로).

    project/open → thread GET. orchestrator 미가용이면 unavailable — JARVIS 전체가
    실패하지 않는다(degraded). thread metadata는 사용자 입력보다 authoritative다.
    """
    import os

    install = detect_install()
    base_url = freebuff_orchestrator_base_url()
    if not base_url:
        return FreebuffCapability(
            status="unavailable",
            detail=(
                "orchestrator 주소를 알 수 없습니다 — Freebuff Desktop이 실행 중이면 "
                "JARVIS_FREEBUFF_PORT 환경변수로 포트를 지정하세요"
            ) + ("" if install["installed"] else " (Freebuff Desktop 설치를 찾지 못했습니다)"),
            detected_version=_detect_version(install),
        )

    token = os.environ.get("JARVIS_FREEBUFF_LAUNCH_ID", "").strip() or None
    try:
        status, payload = _request_json(base_url, "/healthz", token=token)
    except FreebuffError as exc:
        return FreebuffCapability(
            status="unavailable",
            detail=f"orchestrator에 연결할 수 없습니다 ({exc})",
            detected_version=_detect_version(install),
        )
    if status != 200:
        return FreebuffCapability(
            status="unavailable",
            detail=f"orchestrator healthz가 {status}를 반환했습니다",
            detected_version=_detect_version(install),
        )

    # 프로젝트를 먼저 열어야 restart 이후 thread가 rehydrate된다(audit probe5).
    try:
        status, _ = _request_json(
            base_url, "/api/project/open", method="POST",
            body={"path": workspace}, token=token,
        )
        if status != 200:
            return FreebuffCapability(
                status="unavailable",
                detail=f"project/open 실패 (HTTP {status}) — workspace 경로를 확인하세요",
                detected_version=_detect_version(install),
            )
        status, payload = _request_json(
            base_url, f"/api/thread/{external_session_id}", token=token
        )
    except FreebuffError as exc:
        return FreebuffCapability(
            status="unavailable",
            detail=f"orchestrator 요청 실패 ({exc})",
            detected_version=_detect_version(install),
        )

    thread = _extract_thread(payload)
    if status == 404 or (status == 200 and thread is None):
        return FreebuffCapability(
            status="thread_missing",
            detail=f"thread를 찾을 수 없습니다: {external_session_id}",
            detected_version=_detect_version(install),
        )
    if status != 200:
        return FreebuffCapability(
            status="unavailable",
            detail=f"thread 조회 실패 (HTTP {status})",
            detected_version=_detect_version(install),
        )

    messages = payload.get("messages") if isinstance(payload, dict) else None
    messages_count = len(messages) if isinstance(messages, list) else None

    # draft 상태나 auth notice는 capability에 반영한다 — fake success 금지(§10).
    if messages_count == 0:
        return FreebuffCapability(
            status="awaiting_auth",
            detail="thread가 비어 있습니다(zero-message draft) — persistence가 보장되지 않습니다",
            thread=thread,
            detected_version=_detect_version(install),
            messages_count=messages_count,
        )

    return FreebuffCapability(
        status="available",
        thread=thread,
        detected_version=_detect_version(install),
        messages_count=messages_count,
    )


def _detect_version(install: dict) -> str | None:
    """설치 버전 탐지 — exe 버전 리소스 읽기는 무겁고 win 전용이라 파일 타임스탬프
    대신 updater 기록을 참고한다. 실패해도 capability 판정에는 영향 없다."""
    try:
        updater_dir = Path(
            __import__("os").environ.get("LOCALAPPDATA", "")
        ) / "@codebufffreebuff-desktop-updater"
        pending = sorted(updater_dir.glob("pending/Freebuff-*-win-x64.exe"))
        if pending:
            match = re.search(r"Freebuff-([0-9.]+)-win-x64", pending[-1].name)
            if match:
                return match.group(1)
        state = Path.home() / ".config" / "freebuff-desktop"
        _ = install, state  # 표준 경로 탐지 결과는 detail용
    except (OSError, ValueError):
        return None
    return None
