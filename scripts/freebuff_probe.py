from __future__ import annotations

"""Probe: can we start an isolated authenticated Freebuff orchestrator right now?

Reports only non-secret facts (authed flag, healthz, versions). Never prints a token.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.freebuff_orchestrator import (  # noqa: E402
    FreebuffOrchestratorManager,
    default_user_state_path,
)

import os


def main() -> int:
    source = default_user_state_path()
    print("user state present:", source.is_file())
    if not source.is_file():
        print("BLOCKER: no user Freebuff state.json")
        return 2
    # Does an auth entry exist at all? Report only presence, never content.
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        sessions = payload.get("authSessions") or {}
        entry = sessions.get("https://www.codebuff.com") or {}
        has_token = isinstance(entry.get("token"), str) and bool(entry.get("token"))
        print("auth entry present:", has_token, "| user present:", bool(entry.get("user")))
    except Exception as exc:
        print("state parse error:", type(exc).__name__)
        return 2
    if not has_token:
        print("BLOCKER: no auth token in user Freebuff state")
        return 2

    log_dir = Path(os.environ.get("JARVIS_E2E_LOG_DIR") or (Path.home() / "AppData" / "Local" / "Temp" / "jarvis-e2e"))
    manager = FreebuffOrchestratorManager(log_dir=log_dir, startup_timeout_s=90.0)
    try:
        handle = manager.ensure_running()
        print("ensure_running OK | port:", handle.port, "| detected:", handle.detected_version)
        print("capability probe:", json.dumps(handle.capability_probe, ensure_ascii=False))
        report = manager.health()
        print("health:", report.get("status"), "| healthz:", report.get("healthz"))
        status, payload = manager.request("/api/auth/status")
        authed = None
        if isinstance(payload, dict):
            authed = payload.get("authed")
            if authed is None:
                authed = payload.get("token") is not None
        print("auth status http:", status, "| authed:", authed, "| body keys:",
              sorted(payload.keys())[:12] if isinstance(payload, dict) else type(payload).__name__)
        return 0 if (status == 200 and authed) else 3
    except Exception as exc:
        print("FAIL:", type(exc).__name__, str(exc)[:300])
        return 4
    finally:
        manager.cleanup()
        print("cleanup done")


if __name__ == "__main__":
    raise SystemExit(main())
