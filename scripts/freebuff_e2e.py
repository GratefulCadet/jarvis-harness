from __future__ import annotations

"""Authenticated FreebuffRunner V1 E2E — scratch workspace + scratch thread only.

This is the Definition of Done end-to-end check. It drives the *real* bridge
dispatch path (registry -> FreebuffRunner -> isolated orchestrator -> authenticated
Freebuff thread) against a scratch project, and asserts:

  success : AgentRun queued -> running -> completed -> result == FREEBUFF_RUNNER_OK
  cancel  : a long scratch turn is started, confirmed running, then stopped; the run
            must become cancelled only after output growth actually stops, and the
            thread must remain usable afterwards.

Safety rules encoded here:
  * scratch memory dir + scratch workspace, both under the system temp dir, never
    inside this repository and never a user project.
  * a freshly created scratch thread (client-chosen UUID) is used; no existing user
    conversation is ever sent a message.
  * auth material is used only inside harness.freebuff_orchestrator's isolated state;
    this script never reads, prints or persists a token.

Usage:
    python -m scripts.freebuff_e2e
"""

import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.config import HarnessConfig  # noqa: E402
from harness.freebuff_orchestrator import FreebuffOrchestratorManager  # noqa: E402
from harness.freebuff_client import THREAD_ID_RE  # noqa: E402

SUCCESS_INSTRUCTION = "Reply with exactly: FREEBUFF_RUNNER_OK"
SEED_INSTRUCTION = "Reply with exactly: FREEBUFF_E2E_SEEDED"
LONG_INSTRUCTION = (
    "Write a long, detailed, multi-paragraph essay about the history of "
    "soldering irons and workshop tooling. Keep writing for as long as you can."
)

POLL_INTERVAL_S = 0.5
START_TIMEOUT_S = 60
RUN_TIMEOUT_S = 120
STOP_CONFIRM_TIMEOUT_S = 15.0


class E2EFailure(RuntimeError):
    pass


class E2E:
    def __init__(self, root: Path, manager: FreebuffOrchestratorManager) -> None:
        self.root = root
        self.workspace = root / "workspace"
        self.memory = root / "memory"
        self.manager = manager
        self.thread_id = str(uuid.uuid4())
        self.notes: list[str] = []

    # ---- orchestrator plumbing ----

    def request(self, path: str, *, method: str = "GET", body=None):
        status, payload = self.manager.request(path, method=method, body=body)
        if status != 200:
            raise E2EFailure(
                f"{method} {path} -> HTTP {status}: "
                f"{str(payload)[:200] if isinstance(payload, str) else type(payload).__name__}"
            )
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                raise E2EFailure(f"{method} {path} returned malformed JSON") from None
        return payload

    def thread_snapshot(self):
        payload = self.request(f"/api/thread/{self.thread_id}")
        thread = payload.get("thread") if isinstance(payload, dict) else None
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(thread, dict) or not isinstance(messages, list):
            raise E2EFailure("thread snapshot has an unexpected shape")
        if str(thread.get("id") or "").lower() != self.thread_id.lower():
            raise E2EFailure("orchestrator returned a different thread than the scratch one")
        return thread, messages

    def turn_is_running(self) -> bool:
        thread, _ = self.thread_snapshot()
        return str(thread.get("turnState") or "").lower() == "running"

    # ---- setup ----

    def bootstrap_scratch_thread(self) -> None:
        self.request("/api/project/open", method="POST", body={"path": str(self.workspace)})
        if not THREAD_ID_RE.fullmatch(self.thread_id):
            raise E2EFailure("scratch thread id is not a valid UUID")
        status, payload = self.manager.request(
            "/api/threads",
            method="POST",
            body={"projectPath": str(self.workspace), "id": self.thread_id},
        )
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {"raw": "non-json"}
        error = payload.get("error") if isinstance(payload, dict) else None
        if status == 200 and not error:
            self.notes.append(f"scratch thread created: {self.thread_id}")
            return
        # A pinned root re-creates this process' own thread on retry; that is ours to
        # reuse, never an existing user conversation.
        if error == "thread already exists" or status == 409:
            self.notes.append(f"scratch thread already present: {self.thread_id}")
            return
        raise E2EFailure(
            f"scratch thread creation failed (HTTP {status}): {error or payload}"
        )

    def seed_thread(self) -> int:
        """A brand new thread has zero persisted messages and V1 refuses to resume a
        zero-message draft (its persistence is not guaranteed). Send one harmless
        scratch message so the thread has a real baseline."""
        before_thread, messages_before = self.thread_snapshot()
        if messages_before:
            self.notes.append(
                f"thread already seeded ({len(messages_before)} messages)"
            )
            return len(messages_before)
        if not isinstance(before_thread, dict):
            raise E2EFailure("scratch thread metadata missing before seeding")
        self.request(
            f"/api/thread/{self.thread_id}/message",
            method="POST",
            body={"text": SEED_INSTRUCTION, "projectPath": str(self.workspace)},
        )
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while time.monotonic() < deadline:
            thread, messages = self.thread_snapshot()
            assistants = [m for m in messages if str(m.get("role") or "").lower() == "assistant"]
            if assistants and str(thread.get("turnState") or "").lower() != "running":
                self.notes.append(f"thread seeded with {len(messages)} messages")
                return len(messages)
            time.sleep(POLL_INTERVAL_S)
        raise E2EFailure("scratch thread could not be seeded")

    # ---- bridge integration ----

    def make_session(self) -> dict:
        from scripts.harness_bridge import _agent_session_create

        answer = _agent_session_create(self.client(), 900, {
            "agent_type": "freebuff",
            "workspace": str(self.workspace),
            "external_session_id": self.thread_id,
            "purpose": "Authenticated FreebuffRunner V1 E2E (scratch)",
        })
        if answer.get("status") != "ok":
            raise E2EFailure(f"session create failed: {answer.get('error')}")
        return answer["session"]

    def client(self):
        from scripts.harness_bridge import build_client

        return build_client(self.config())

    def config(self) -> HarnessConfig:
        from scripts.harness_bridge import build_config

        return build_config()

    def start_run(self, session_id: str, instruction: str) -> str:
        from scripts.harness_bridge import _agent_run_start

        answer = _agent_run_start(self.client(), 901, {
            "session_id": session_id,
            "instruction": instruction,
            # renderer-controlled values that must NOT win over the session:
            "workspace": str(self.root / "evil-workspace"),
            "thread_id": str(uuid.uuid4()),
        })
        if answer.get("status") != "ok":
            raise E2EFailure(
                f"agent_run_start failed: {answer.get('error')}"
            )
        return answer["run"]["run_id"]

    def diagnose_runner_path(self, session: dict) -> None:
        """Replay the exact request sequence start() performs and print each status.

        Never prints a response body — only method, path and status — because thread
        bodies can contain conversation text.
        """
        thread_id = str(session.get("external_session_id") or "")
        workspace = str(session.get("workspace") or "")
        print(f"[diag] e2e.manager id={id(self.manager)} state={json.dumps(self.manager.describe(), ensure_ascii=False)}")
        try:
            from scripts import harness_bridge as bridge

            print(f"[diag] bridge manager id={id(getattr(bridge, '_FREEBUFF_MANAGER', None))}")
            runner = getattr(bridge, "_FREEBUFF_RUNNER", None)
            print(f"[diag] bridge runner id={id(runner)} runner.manager id={id(runner.manager) if runner else None}")
            if runner is not None and runner.manager is not self.manager:
                print(f"[diag] SPLIT-MANAGER detail={json.dumps(runner.manager.describe(), ensure_ascii=False)}")
            if runner is not None:
                print(f"[diag] runner.session id={id(runner.session_store)} bridge session id={id(getattr(bridge, '_AGENT_SESSION_STORE', None))}")
        except Exception as exc:  # noqa: BLE001
            print(f"[diag] bridge introspection failed: {type(exc).__name__}")

        steps = [
            ("POST", "/api/project/open", {"path": workspace}),
            ("GET", f"/api/thread/{thread_id}", None),
        ]
        for method, path, body in steps:
            try:
                status, _ = self.manager.request(path, method=method, body=body)
            except Exception as exc:  # noqa: BLE001
                print(f"[diag] {method} {path} -> {type(exc).__name__}: {str(exc)[:160]}")
                continue
            print(f"[diag] {method} {path} -> HTTP {status}")
        # And the same call made through the runner, isolated from bridge dispatch.
        try:
            from harness.agent_runs import AgentRunStore, default_agent_runs_file
            from harness.agent_sessions import AgentSessionStore, default_agent_sessions_file
            from harness.freebuff_runner import FreebuffRunner

            print(f"[diag] manager state: {json.dumps(self.manager.describe(), ensure_ascii=False)}")

            store = AgentRunStore(default_agent_runs_file(self.memory))
            sessions = AgentSessionStore(default_agent_sessions_file(self.memory))
            runner = FreebuffRunner(store, str(self.memory), session_store=sessions,
                                    manager=self.manager, poll_interval_s=0.2,
                                    completion_timeout_s=5)
            probe_run = store.create_run(
                agent_type="freebuff", instruction="diagnostic",
                repo_path=workspace, session_id=session["session_id"],
            )
            runner.start(probe_run["run_id"], {"instruction": "diagnostic"})
            print("[diag] direct runner.start OK")
        except Exception as exc:  # noqa: BLE001
            print(f"[diag] direct runner.start -> {type(exc).__name__}: {str(exc)[:300]}")

    def run_status(self, run_id: str) -> dict:
        from scripts.harness_bridge import _agent_run_status

        answer = _agent_run_status(self.client(), 902, {"run_id": run_id})
        if answer.get("status") != "ok":
            raise E2EFailure(f"agent_run_status failed: {answer.get('error')}")
        return answer["run"]

    def cancel_run(self, run_id: str) -> dict:
        from scripts.harness_bridge import _agent_run_cancel

        answer = _agent_run_cancel(self.client(), 903, {"run_id": run_id})
        if answer.get("status") != "ok":
            raise E2EFailure(f"agent_run_cancel failed: {answer.get('error')}")
        return answer["run"]

    def wait_terminal(self, run_id: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            last = self.run_status(run_id)
            if last.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                return last
            time.sleep(POLL_INTERVAL_S)
        raise E2EFailure(f"run {run_id} did not reach a terminal state: {last.get('status')}")

    # ---- cases ----

    def case_success(self, session_id: str) -> dict:
        run_id = self.start_run(session_id, SUCCESS_INSTRUCTION)
        saw_running = False
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            status = self.run_status(run_id)
            if status.get("status") == "running":
                saw_running = True
                break
            if status.get("status") in {"failed", "cancelled", "interrupted"}:
                raise E2EFailure(f"run failed before running: {status.get('error')}")
            time.sleep(POLL_INTERVAL_S)
        if not saw_running:
            raise E2EFailure("run never reached running")
        self.notes.append("run reached running")

        finished = self.wait_terminal(run_id, RUN_TIMEOUT_S)
        if finished.get("status") != "completed":
            raise E2EFailure(
                f"expected completed, got {finished.get('status')}: {finished.get('error')}"
            )
        result = finished.get("result_summary")
        if result != "FREEBUFF_RUNNER_OK":
            raise E2EFailure(f"result mismatch: {result!r}")
        if not finished.get("external_thread_id"):
            raise E2EFailure("run did not record its bound external thread")
        self.notes.append("success E2E: completed with exact result")
        return finished

    def case_cancel(self, session_id: str) -> dict:
        run_id = self.start_run(session_id, LONG_INSTRUCTION)
        # Confirm the turn is actually running before cancelling.
        confirmed = False
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            status = self.run_status(run_id)
            if status.get("status") == "running":
                confirmed = True
                break
            if status.get("status") in {"failed", "cancelled", "interrupted"}:
                raise E2EFailure(f"long run ended early: {status.get('status')} {status.get('error')}")
            time.sleep(POLL_INTERVAL_S)
        if not confirmed:
            raise E2EFailure("long run never reached running")
        # Give the model a moment to actually produce output so cancellation is real.
        time.sleep(3.0)
        _, messages_before_cancel = self.thread_snapshot()
        self.notes.append(f"cancel requested at {len(messages_before_cancel)} messages")

        cancelled = self.cancel_run(run_id)
        if cancelled.get("status") != "cancelled":
            raise E2EFailure(f"cancel did not produce cancelled: {cancelled.get('status')}")

        # The stop must actually stop output growth.
        _, after = self.thread_snapshot()
        settled = time.monotonic() + STOP_CONFIRM_TIMEOUT_S
        stable = 0
        previous = (len(after), str(after[-1]) if after else "")
        while time.monotonic() < settled:
            time.sleep(1.0)
            _, now = self.thread_snapshot()
            signature = (len(now), str(now[-1]) if now else "")
            if signature == previous:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
                previous = signature
            time.sleep(POLL_INTERVAL_S)
        else:
            raise E2EFailure("thread output kept growing after stop")
        self.notes.append("cancel E2E: output growth stopped after stop")

        # The thread must still be usable (a stop, not a delete).
        thread, _ = self.thread_snapshot()
        if not isinstance(thread, dict):
            raise E2EFailure("thread unusable after stop")
        self.notes.append(f"thread usable after stop (status={thread.get('status')})")

        final = self.run_status(run_id)
        if final.get("status") != "cancelled":
            raise E2EFailure(f"run drifted from cancelled to {final.get('status')}")
        return final


def main() -> int:
    # A pinned root makes retries cheap: the scratch thread and its baseline survive,
    # so a failed attempt does not spend another real model turn re-seeding.
    pinned = os.environ.get("JARVIS_E2E_ROOT", "").strip()
    root = Path(pinned) if pinned else Path(tempfile.mkdtemp(prefix="jarvis-freebuff-e2e-"))
    workspace = root / "workspace"
    memory = root / "memory"
    workspace.mkdir(parents=True, exist_ok=True)
    memory.mkdir(parents=True, exist_ok=True)
    os.environ["JARVIS_E2E_ROOT"] = str(root)
    print(f"[e2e] scratch root: {root}")
    thread_file = root / "scratch_thread.txt"

    # Route the bridge at the scratch state dir and register the scratch workspace as an
    # approved root so _resolve_repo_path accepts it.
    os.environ["JARVIS_BRIDGE_MEMORY_DIR"] = str(memory)
    os.environ["JARVIS_STATE_DIR"] = str(memory)
    os.environ["JARVIS_FILE_ROOTS"] = f"e2escratch={workspace}"

    log_dir = root / "logs"
    manager = FreebuffOrchestratorManager(
        log_dir=log_dir,
        startup_timeout_s=60.0,
        _test_state_dir=root / "freebuff-state",
    )

    e2e = E2E(root, manager)
    if thread_file.is_file():
        recorded = thread_file.read_text(encoding="utf-8").strip()
        if THREAD_ID_RE.fullmatch(recorded):
            e2e.thread_id = recorded
            e2e.notes.append(f"reusing recorded scratch thread {recorded}")
    results: dict[str, str] = {}
    session = None
    scratch_state_dir = None
    try:
        handle = manager.ensure_running()
        scratch_state_dir = handle.state_dir
        print(f"[e2e] orchestrator up on port {handle.port} (detected {handle.detected_version})")
        print(f"[e2e] capability probe: {json.dumps(handle.capability_probe, ensure_ascii=False)}")

        # Make the bridge use *this* manager so registry dispatch reuses one process.
        from scripts import harness_bridge as bridge

        bridge._FREEBUFF_MANAGER = manager
        bridge._FREEBUFF_RUNNER = None
        bridge._FREEBUFF_MANAGER_OVERRIDES.clear()

        # The session create path validates the thread through freebuff_client, which
        # honours this sanctioned test-injection seam for base URL.
        os.environ["JARVIS_FREEBUFF_ORCHESTRATOR_BASE_URL"] = f"http://127.0.0.1:{handle.port}"

        e2e.bootstrap_scratch_thread()
        thread_file.write_text(e2e.thread_id + "\n", encoding="utf-8")
        e2e.seed_thread()

        session = e2e.make_session()
        print(f"[e2e] session {session['session_id']} bound to scratch thread")
        if session.get("status") != "active":
            raise E2EFailure(f"session not active: {session.get('status')}")
        if session.get("external_session_id") != e2e.thread_id:
            raise E2EFailure("session did not store the scratch thread id")

        finished = e2e.case_success(session["session_id"])
        results["success"] = (
            f"PASS status={finished['status']} result={finished['result_summary']!r} "
            f"thread={finished['external_thread_id']}"
        )
        print(f"[e2e] SUCCESS -> {results['success']}")

        cancelled = e2e.case_cancel(session["session_id"])
        results["cancel"] = f"PASS status={cancelled['status']}"
        print(f"[e2e] CANCEL  -> {results['cancel']}")

        failed = [k for k, v in results.items() if not v.startswith("PASS")]
        if failed:
            print(f"[e2e] FAILED: {failed}")
            return 1
        print("[e2e] ALL AUTHENTICATED CASES PASS")
        for note in e2e.notes:
            print(f"       - {note}")
        return 0
    except Exception as exc:
        print(f"[e2e] FAILED: {type(exc).__name__}: {exc}")
        for note in e2e.notes:
            print(f"       - {note}")
        if session is not None:
            try:
                e2e.diagnose_runner_path(session)
            except Exception as diag_exc:  # noqa: BLE001
                print(f"[diag] raised {type(diag_exc).__name__}: {diag_exc}")
        return 1
    finally:
        os.environ.pop("JARVIS_FREEBUFF_ORCHESTRATOR_BASE_URL", None)
        try:
            bridge._FREEBUFF_MANAGER = None
            bridge._FREEBUFF_RUNNER = None
        except Exception:
            pass
        manager.cleanup()
        print("[e2e] orchestrator cleaned up (process + credential temp state)")
        if scratch_state_dir and Path(scratch_state_dir).exists():
            print("[e2e] WARNING: credential temp state survived cleanup")
        # Default is to drop the scratch root: it holds conversation text from a throwaway
        # thread. JARVIS_E2E_KEEP=1 preserves it so a failed attempt can be retried without
        # spending another real model turn re-seeding.
        keep = os.environ.get("JARVIS_E2E_KEEP", "0") == "1"
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"[e2e] scratch kept at {root}")


if __name__ == "__main__":
    raise SystemExit(main())
