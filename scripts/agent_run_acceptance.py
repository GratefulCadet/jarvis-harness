# -*- coding: utf-8 -*-
"""Agent Delegation V1 — Harness JSONL 수준 acceptance (A/B/C/D/E).

production source를 건드리지 않는다:
- repo는 임시 scratch git repo (acceptance 중 생성, 끝나고 폐기)
- state는 임시 JARVIS_STATE_DIR
- 실제 Codex(acceptance A)는 작은 deterministic task(AGENT_RUN_SMOKE.txt)만 시킨다

검증 항목:
  A. 성공 run   — 실제 codex exec가 worktree에 파일을 만들고, changed_files에
                  잡히고, status=completed, main tree는 변경 없음 (D 겸용)
  B. 취소 run   — fake long binary 시작 → cancel → 프로세스 실제 종료
                  (orphan check: tasklist에서 pid 소멸), status=cancelled
  C. 실패 run   — non-zero exit → status=failed, exit code 가시
  E. 검증       — completed run에 verify op → run.verification에
                  {command,status,exit_code,duration} 저장

사용: python -m scripts.agent_run_acceptance [--skip-real-codex]
exit 0 = 모든 acceptance PASS.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from harness.agent_runs import AgentRunStore, default_agent_runs_file  # noqa: E402
from harness.config import HarnessConfig  # noqa: E402
from scripts.harness_bridge import (  # noqa: E402
    _agent_run_cancel,
    _agent_run_start,
    _agent_run_status,
    _agent_run_verify,
    _resolve_repo_path,
    mark_agent_runs_interrupted,
)


def make_scratch_repo(base: Path, name: str) -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("scratch repo for agent run acceptance\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@t", "-c", "user.name=a", "commit", "-qm", "seed"],
        cwd=repo, check=True,
    )
    # 사용자의 dirty tree를 흉내 — acceptance 내내 변하지 않아야 한다
    (repo / "USER_DIRTY.txt").write_text("user's uncommitted work\n", encoding="utf-8")
    return repo


def wait_terminal(store: AgentRunStore, run_id: str, timeout: float = 600.0) -> dict:
    deadline = time.time() + timeout
    last = store.get(run_id) or {}
    while time.time() < deadline:
        last = store.get(run_id) or {}
        if last.get("status") not in ("queued", "running"):
            return last
        time.sleep(1.0)
    raise AssertionError(f"run {run_id} 가 {timeout}s 안에 끝나지 않았다: {last}")


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        probe = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in probe.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Ctx:
    def __init__(self, memory_dir: Path, repo: Path) -> None:
        self.config = HarnessConfig(
            runtime="mock", memory_dir=memory_dir, trace_dir=memory_dir / "traces"
        )
        self.config.file_roots = {"scratch": str(repo)}
        self.repo = repo


def acceptance_success_real_codex(base: Path) -> None:
    """A + D — 실제 Codex, 실제 파일, 실제 isolation."""
    memory = base / "stateA"
    memory.mkdir(parents=True)
    repo = make_scratch_repo(base, "repoA")
    ctx = Ctx(memory, repo)

    started = _agent_run_start(ctx, "a1", {
        "agent_type": "codex",
        "instruction": (
            "이 저장소 작업 루트에 AGENT_RUN_SMOKE.txt 파일을 만들고, "
            "파일 내용을 정확히 'agent run smoke test' (따옴표 제외)로 작성해라. "
            "다른 파일은 만지지 마라."
        ),
        "repo_path": str(repo),
    })
    assert started["status"] == "ok", f"A 실패 — start: {started}"
    run = started["run"]
    assert run["status"] in ("queued", "running"), run

    store = AgentRunStore(default_agent_runs_file(memory))
    finished = wait_terminal(store, run["run_id"], timeout=600)
    assert finished["status"] == "completed", f"A 실패 — 최종 상태: {finished}"
    assert finished["exit_code"] == 0, finished

    worktree = Path(finished["worktree_path"])
    artifact = worktree / "AGENT_RUN_SMOKE.txt"
    assert artifact.exists(), f"A 실패 — 산출물 없음: {worktree}"
    content = artifact.read_text(encoding="utf-8").strip().strip('"').strip("'")
    assert content == "agent run smoke test", f"A 실패 — 내용 불일치: {content!r}"

    assert "AGENT_RUN_SMOKE.txt" in (finished["changed_files"] or []), finished

    # D — main tree는 그대로 (dirty 파일 포함, 새 파일 없음)
    assert (repo / "USER_DIRTY.txt").read_text(encoding="utf-8") == "user's uncommitted work\n"
    assert not (repo / "AGENT_RUN_SMOKE.txt").exists(), "D 실패 — main tree 오염"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == "?? USER_DIRTY.txt", f"D 실패 — main status: {status.stdout!r}"
    # 브랜치가 main에 체크아웃되지 않았다
    head_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_branch != finished["branch"], "D 실패 — main이 run 브랜치로 바뀌었다"
    print(f"  [A+D] PASS — completed, artifact ok, changed_files ok, main tree untouched (branch {finished['branch']})")

    # E — verification은 별도 post-run action
    response = _agent_run_verify(ctx, "a2", {"run_id": run["run_id"], "command": "verify:fast"})
    assert response["status"] == "ok", f"E 시작 실패: {response}"
    deadline = time.time() + 900
    while time.time() < deadline:
        verification = store.get(run["run_id"]).get("verification") or {}
        if verification.get("status") not in (None, "running"):
            break
        time.sleep(2.0)
    verification = store.get(run["run_id"]).get("verification") or {}
    assert verification.get("command") == "verify:fast", verification
    assert verification.get("status") in ("completed", "failed"), verification
    assert verification.get("exit_code") is not None, verification
    assert isinstance(verification.get("duration_ms"), int), verification
    # fresh worktree에는 node_modules가 없는 게 정상 — 환경 실패도 정직히 기록된 것이 요구다
    expected = "completed" if verification.get("exit_code") == 0 else "failed"
    assert verification["status"] == expected
    print(f"  [E] PASS — verification stored: {verification['status']} exit={verification['exit_code']} duration={verification['duration_ms']}ms")

    return run["run_id"]


def write_fake_codex(base: Path, mode: str) -> Path:
    script = f'''import sys, time, os, json
mode = {mode!r}
args = sys.argv[1:]
root = args[args.index("-C") + 1] if "-C" in args else "."
sys.stdout.write(json.dumps({{"type": "item.completed", "mode": mode}}))
sys.stdout.flush()
instruction = sys.stdin.read()
if mode == "fail":
    sys.stderr.write("intentional failure\\n")
    sys.exit(3)
print("working", flush=True)
deadline = time.time() + 300
while time.time() < deadline:
    time.sleep(0.25)
sys.exit(0)
'''
    path = base / f"fake_codex_{mode}.py"
    path.write_text(script, encoding="utf-8")
    return path


def acceptance_cancel(base: Path) -> None:
    """B — 실제 subprocess 수준 취소 + orphan check."""
    memory = base / "stateB"
    memory.mkdir(parents=True)
    repo = make_scratch_repo(base, "repoB")
    ctx = Ctx(memory, repo)
    fake = write_fake_codex(base, "sleep")
    os.environ["JARVIS_CODEX_BIN"] = f"{sys.executable} {fake}"
    try:
        started = _agent_run_start(ctx, "b1", {
            "agent_type": "codex",
            "instruction": "long running work",
            "repo_path": str(repo),
        })
        assert started["status"] == "ok", started
        run_id = started["run"]["run_id"]
        store = AgentRunStore(default_agent_runs_file(memory))
        deadline = time.time() + 30
        while time.time() < deadline:
            if (store.get(run_id) or {}).get("status") == "running":
                break
            time.sleep(0.2)
        run = store.get(run_id)
        assert run["status"] == "running", run
        pid = run["pid"]
        assert pid_alive(pid), "B 전제 실패 — 자식이 살아 있어야 한다"

        cancelled = _agent_run_cancel(ctx, "b2", {"run_id": run_id})
        assert cancelled["status"] == "ok", cancelled

        deadline = time.time() + 30
        while time.time() < deadline:
            current = store.get(run_id)
            if current["status"] not in ("queued", "running"):
                break
            time.sleep(0.2)
        current = store.get(run_id)
        assert current["status"] == "cancelled", f"B 실패 — 상태: {current}"
        # orphan check — 프로세스가 실제로 죽었다
        deadline = time.time() + 10
        alive = True
        while time.time() < deadline:
            alive = pid_alive(pid)
            if not alive:
                break
            time.sleep(0.5)
        assert not alive, f"B 실패 — orphan process {pid} 생존"
        print(f"  [B] PASS — cancelled, pid {pid} terminated (no orphan)")
    finally:
        os.environ.pop("JARVIS_CODEX_BIN", None)


def acceptance_failure(base: Path) -> None:
    """C — non-zero exit이 failed + exit code로 가시."""
    memory = base / "stateC"
    memory.mkdir(parents=True)
    repo = make_scratch_repo(base, "repoC")
    ctx = Ctx(memory, repo)
    fake = write_fake_codex(base, "fail")
    os.environ["JARVIS_CODEX_BIN"] = f"{sys.executable} {fake}"
    try:
        started = _agent_run_start(ctx, "c1", {
            "agent_type": "codex",
            "instruction": "doomed work",
            "repo_path": str(repo),
        })
        assert started["status"] == "ok", started
        store = AgentRunStore(default_agent_runs_file(memory))
        finished = wait_terminal(store, started["run"]["run_id"], timeout=60)
        assert finished["status"] == "failed", finished
        assert finished["exit_code"] == 3, finished
        assert "exit code 3" in (finished["error"] or ""), finished
        print(f"  [C] PASS — failed, exit code {finished['exit_code']} visible")
    finally:
        os.environ.pop("JARVIS_CODEX_BIN", None)


def acceptance_restart_marks_interrupted(base: Path) -> None:
    """§13 — restart 후 running은 정직하게 interrupted."""
    memory = base / "stateD"
    memory.mkdir(parents=True)
    repo = make_scratch_repo(base, "repoD")
    ctx = Ctx(memory, repo)
    store = AgentRunStore(default_agent_runs_file(memory))
    run = store.create_run(agent_type="codex", instruction="x", repo_path=str(repo))
    store.update(run["run_id"], status="running", pid=999999999)
    marked = mark_agent_runs_interrupted(ctx)
    assert len(marked) == 1, marked
    assert store.get(run["run_id"])["status"] == "interrupted"
    print("  [Persistence] PASS — running → interrupted after restart")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-real-codex", action="store_true")
    args = parser.parse_args(argv)

    base = Path(tempfile.mkdtemp(prefix="agent_run_acceptance_"))
    print(f"Agent Delegation V1 acceptance — scratch: {base}")
    try:
        if args.skip_real_codex:
            print("  [A+D/E] SKIP — --skip-real-codex")
        else:
            acceptance_success_real_codex(base)
        acceptance_cancel(base)
        acceptance_failure(base)
        acceptance_restart_marks_interrupted(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("AGENT RUN ACCEPTANCE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
