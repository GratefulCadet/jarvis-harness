from __future__ import annotations

"""Secret hygiene check for the FreebuffRunner milestone.

Reads the user's Freebuff auth token **in memory only** and reports whether it
appears anywhere in the repository (source, tests, artifacts, logs, git index).
The token value is never printed — only match counts and file names.

Also verifies:
  * no isolated/credential state.json was left inside the repository
  * the user's Freebuff state.json is still the only place the auth entry lives
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", ".worktrees", "node_modules"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".log", ".yaml", ".yml", ".cjs", ".js", ".toml"}


def read_token() -> str:
    try:
        raw = json.loads((Path.home() / ".config" / "freebuff-desktop" / "state.json").read_text(encoding="utf-8"))
        entry = (raw.get("authSessions") or {}).get("https://www.codebuff.com") or {}
        token = entry.get("token")
        return token if isinstance(token, str) else ""
    except Exception:
        return ""


def iter_files():
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield path


def main() -> int:
    token = read_token()
    print(f"token readable from user state: {bool(token)}")
    problems: list[str] = []

    hits = []
    if token:
        needles = {token, token[:24]}
        for path in iter_files():
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for needle in needles:
                if needle and needle in text:
                    hits.append(str(path.relative_to(REPO)))
                    break
    if hits:
        problems.append(f"credential material found in {len(hits)} file(s): {hits[:10]}")
    print(f"credential leaks in repo: {len(hits)}")

    # Any credential-bearing state file inside the repo?
    state_files = [
        str(p.relative_to(REPO))
        for p in REPO.rglob("state.json")
        if not any(part in SKIP_DIRS for part in p.parts)
    ]
    if state_files:
        problems.append(f"state.json left inside repo: {state_files}")
    print(f"state.json files in repo: {len(state_files)}")

    # The user's auth entry must still be exactly where it started.
    try:
        raw = json.loads((Path.home() / ".config" / "freebuff-desktop" / "state.json").read_text(encoding="utf-8"))
        hosts = sorted((raw.get("authSessions") or {}).keys())
        print(f"user state auth hosts: {hosts}")
        if hosts != ["https://www.codebuff.com"]:
            problems.append(f"unexpected auth hosts in user state: {hosts}")
        # recentProjects must not have grown because of our isolated orchestrator.
        print(f"user state recentProjects: {len(raw.get('recentProjects') or [])}")
    except Exception as exc:
        problems.append(f"could not re-read user state: {type(exc).__name__}")

    # No scratch E2E directory may have landed inside the repo.
    e2e_dirs = [str(p.relative_to(REPO)) for p in REPO.rglob("jarvis-freebuff-e2e-*")]
    if e2e_dirs:
        problems.append(f"E2E scratch inside repo: {e2e_dirs}")

    if problems:
        print("HYGIENE FAIL")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("HYGIENE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
