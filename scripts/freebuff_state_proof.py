from __future__ import annotations

"""Prove the user's Freebuff state.json is byte-identical across an authenticated E2E run.

Only hashes and mtimes are reported; contents are never printed.
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

STATE = Path.home() / ".config" / "freebuff-desktop" / "state.json"
ROOT = Path(os.environ.get("TEMP", "/tmp")) / "jarvis-freebuff-e2e-f30djeez"


def snapshot() -> tuple[str, int, int]:
    data = STATE.read_bytes()
    return (
        hashlib.sha256(data).hexdigest()[:16],
        STATE.stat().st_mtime_ns,
        len(data),
    )


def main() -> int:
    if not STATE.is_file():
        print("BLOCKER: user state.json missing")
        return 2
    before = snapshot()
    print(f"BEFORE hash={before[0]} mtime_ns={before[1]} bytes={before[2]}")

    env = dict(os.environ)
    env["JARVIS_E2E_ROOT"] = str(ROOT)
    proc = subprocess.run(
        [sys.executable, "-u", "-m", "scripts.freebuff_e2e"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=560,
    )
    tail = [line for line in (proc.stdout or "").splitlines() if line.startswith("[e2e]")]
    for line in tail:
        print(line)
    if proc.stderr:
        for line in proc.stderr.splitlines()[-3:]:
            print(f"  stderr: {line}")
    print(f"E2E_RC={proc.returncode}")

    after = snapshot()
    print(f"AFTER  hash={after[0]} mtime_ns={after[1]} bytes={after[2]}")
    unchanged = before == after
    print(f"USER_STATE_UNCHANGED={'YES' if unchanged else 'NO'}")
    return 0 if (proc.returncode == 0 and unchanged) else 1


if __name__ == "__main__":
    raise SystemExit(main())
