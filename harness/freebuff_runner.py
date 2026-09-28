from __future__ import annotations

"""AgentRunner implementation that resumes an existing bound Freebuff thread."""

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from harness.agent_runs import AgentRunError, AgentRunner, TERMINAL_STATUSES
from harness.agent_sessions import AgentSessionStore
from harness.freebuff_orchestrator import (
    FreebuffOrchestratorError,
    FreebuffOrchestratorManager,
)
from harness.freebuff_client import THREAD_ID_RE

POLL_INTERVAL_S = 0.5
COMPLETION_TIMEOUT_S = 1800
CANCEL_CONFIRM_TIMEOUT_S = 5.0
MAX_RESULT_CHARS = 12000


class FreebuffRunnerError(AgentRunError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _message_identity(message: dict[str, Any]) -> str | None:
    # The installed Freebuff contract uses `ts` for committed messages; unlike live
    # snapshots, which carry streamSeq, committed messages may have no explicit ID.
    for key in ("id", "messageId", "message_id", "ts", "createdAt", "timestamp", "updatedAt"):
        value = message.get(key)
        if value is not None and str(value):
            return f"{key}:{value}"
    return None


def _is_assistant(message: dict[str, Any]) -> bool:
    role = str(message.get("role") or message.get("type") or "").lower()
    return role in {"assistant", "assistant_message", "assistantmessage"}


def _is_committed(message: dict[str, Any]) -> bool:
    if message.get("committed") is True or message.get("isCommitted") is True:
        return True
    status = str(message.get("status") or "").lower()
    if status in {"committed", "complete", "completed", "finished"}:
        return True
    # buildThreadSnapshot appends an uncommitted live turn with streamSeq and no
    # status/committed flag. Persisted messages omit streamSeq and carry ts/metrics.
    if "streamSeq" in message:
        return False
    # The validated orchestrator thread snapshot returns committed messages in messages.
    return not any(key in message for key in ("committed", "isCommitted", "status"))


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                kind = str(part.get("kind") or part.get("type") or "").lower()
                if kind and kind not in {"text", "output_text", "message_text"}:
                    continue
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    if isinstance(value, dict):
        for key in ("text", "content", "parts"):
            if key in value:
                text = _content_text(value[key])
                if text:
                    return text
    return ""


def _assistant_text(message: dict[str, Any]) -> str:
    for key in ("content", "text", "parts", "message"):
        text = _content_text(message.get(key))
        if text:
            return text
    return ""


def _thread_snapshot(payload: Any, thread_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise FreebuffRunnerError("Freebuff thread response has an unsupported format")
    thread = payload.get("thread")
    if not isinstance(thread, dict):
        raise FreebuffRunnerError("Freebuff thread metadata is missing")
    returned_id = thread.get("id") or thread.get("threadId")
    if returned_id and str(returned_id).lower() != thread_id.lower():
        raise FreebuffRunnerError("Freebuff returned a different thread than the bound session")
    messages = payload.get("messages")
    if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
        raise FreebuffRunnerError("Freebuff messages response is malformed")
    return thread, messages


class FreebuffRunner(AgentRunner):
    name = "freebuff"

    def __init__(
        self,
        store: Any,
        memory_dir: str,
        *,
        session_store: AgentSessionStore | None = None,
        manager: FreebuffOrchestratorManager | None = None,
        poll_interval_s: float = POLL_INTERVAL_S,
        completion_timeout_s: float = COMPLETION_TIMEOUT_S,
        cancel_confirm_timeout_s: float = CANCEL_CONFIRM_TIMEOUT_S,
    ) -> None:
        super().__init__(store, memory_dir)
        self.session_store = session_store
        self.manager = manager or FreebuffOrchestratorManager()
        self.poll_interval_s = float(poll_interval_s)
        self.completion_timeout_s = float(completion_timeout_s)
        self.cancel_confirm_timeout_s = float(cancel_confirm_timeout_s)
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._baselines: dict[str, dict[str, Any]] = {}
        self._operation_locks: dict[str, threading.Lock] = {}
        self._lock = threading.RLock()

    def bind_session_store(self, session_store: AgentSessionStore) -> None:
        self.session_store = session_store

    def _get_session(self, run_id: str) -> dict[str, Any]:
        run = self.store.get(run_id)
        if run is None:
            raise FreebuffRunnerError(f"알 수 없는 run: {run_id}")
        if not run.get("session_id"):
            raise FreebuffRunnerError("Freebuff run에는 AgentSession이 필요합니다")
        if self.session_store is None:
            raise FreebuffRunnerError("AgentSession store is unavailable")
        session = self.session_store.get(str(run["session_id"]))
        if session is None:
            raise FreebuffRunnerError("Freebuff AgentSession을 찾을 수 없습니다")
        if session.get("agent_type") != "freebuff":
            raise FreebuffRunnerError("AgentSession agent_type이 Freebuff가 아닙니다")
        if session.get("status") != "active":
            raise FreebuffRunnerError(f"{session.get('status') or 'inactive'} session에서는 run을 시작할 수 없습니다")
        thread_id = session.get("external_session_id")
        if not isinstance(thread_id, str) or not THREAD_ID_RE.fullmatch(thread_id.strip()):
            raise FreebuffRunnerError("Freebuff AgentSession의 existing thread UUID가 잘못되었습니다")
        workspace = session.get("workspace")
        if not isinstance(workspace, str) or not workspace.strip():
            raise FreebuffRunnerError("Freebuff AgentSession workspace가 없습니다")
        return session

    @staticmethod
    def _request_json(manager: FreebuffOrchestratorManager, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
        response = manager.request(path, method=method, body=body)
        if isinstance(response, tuple) and len(response) == 2:
            status, payload = response
        else:
            status, payload = 200, response
        if status != 200:
            # Path (not body) is reported so a failing call is diagnosable; bodies are
            # never included because they could carry sensitive detail.
            safe_path = path.split("?", 1)[0]
            raise FreebuffRunnerError(
                f"Freebuff orchestrator returned HTTP {status} for {method} {safe_path}"
            )
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                raise FreebuffRunnerError("Freebuff orchestrator returned malformed JSON") from None
        return payload

    @classmethod
    def _get_thread(cls, manager: FreebuffOrchestratorManager, thread_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return _thread_snapshot(cls._request_json(manager, f"/api/thread/{thread_id}"), thread_id)

    @staticmethod
    def _baseline(messages: list[dict[str, Any]]) -> dict[str, Any]:
        assistant = [message for message in messages if _is_assistant(message)]
        latest = assistant[-1] if assistant else None
        return {
            "message_count": len(messages),
            "latest_assistant_identity": _message_identity(latest) if latest else None,
            "latest_assistant_timestamp": (
                latest.get("ts") or latest.get("createdAt") or latest.get("timestamp")
            ) if latest else None,
            "assistant_ids": [identity for message in assistant if (identity := _message_identity(message))],
            "baseline_assistant_count": len(assistant),
            "completion_hint": False,
            "sse_connected": False,
        }

    def start(self, run_id: str, spec: dict[str, Any]) -> None:
        run = self.store.get(run_id)
        if run is None:
            raise FreebuffRunnerError(f"알 수 없는 run: {run_id}")
        session = self._get_session(run_id)
        instruction = spec.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise FreebuffRunnerError("instruction이 비어 있습니다")
        if len(instruction) > 20000:
            raise FreebuffRunnerError("instruction이 너무 깁니다 (최대 20000자)")
        thread_id = session["external_session_id"].strip()
        workspace = session["workspace"].strip()

        # Claim the queued run before any external side effect. This linearizes start
        # against cancel, and the per-run operation lock ensures stop cannot overtake
        # the instruction POST when the bridge receives concurrent requests.
        with self._lock:
            current = self.store.get(run_id)
            if current is None:
                raise FreebuffRunnerError(f"알 수 없는 run: {run_id}")
            if current.get("status") != "queued":
                raise FreebuffRunnerError("Freebuff run is no longer queued")
            event = threading.Event()
            operation_lock = threading.Lock()
            baseline_data = {
                "external_thread_id": thread_id,
                "workspace": workspace,
                "started_at": _now_iso(),
                "instruction_accepted": False,
            }
            self._cancel_events[run_id] = event
            self._operation_locks[run_id] = operation_lock
            self._baselines[run_id] = baseline_data
            self.store.update(
                run_id,
                status="running",
                started_at=baseline_data["started_at"],
                external_thread_id=thread_id,
                freebuff_baseline=baseline_data,
            )

        try:
            with operation_lock:
                if not event.is_set():
                    self.manager.ensure_running()
                    self._request_json(
                        self.manager, "/api/project/open", method="POST", body={"path": workspace},
                    )
                    thread, messages = self._get_thread(self.manager, thread_id)
                    if not messages:
                        raise FreebuffRunnerError(
                            "Freebuff thread has no persisted messages and cannot be resumed safely"
                        )
                    if str(thread.get("turnState") or "").lower() != "idle":
                        raise FreebuffRunnerError(
                            "Freebuff thread is not idle; refusing to overlap another conversation turn"
                        )
                    if any(
                        _is_assistant(message) and "streamSeq" in message
                        for message in messages
                    ):
                        raise FreebuffRunnerError(
                            "Freebuff thread has an uncommitted live turn; refusing to overlap it"
                        )
                    measured = self._baseline(messages)
                    measured["instruction_sha256"] = hashlib.sha256(
                        instruction.strip().encode("utf-8")
                    ).hexdigest()
                    measured.update({
                        "external_thread_id": thread_id,
                        "workspace": workspace,
                        "model": thread.get("model") or session.get("model"),
                        "reasoning_effort": thread.get("reasoningEffort") or session.get("reasoning_effort"),
                        "started_at": baseline_data["started_at"],
                        "instruction_accepted": False,
                    })
                    with self._lock:
                        if event.is_set() or (self.store.get(run_id) or {}).get("status") != "running":
                            return
                        self._baselines[run_id] = baseline_data = measured
                        self.store.update(
                            run_id,
                            started_message_count=measured["message_count"],
                            completed_message_count=measured["message_count"],
                            freebuff_baseline=measured,
                        )
                    if not event.is_set():
                        response = self._request_json(
                            self.manager,
                            f"/api/thread/{thread_id}/message",
                            method="POST",
                            body={"text": instruction.strip()},
                        )
                        if isinstance(response, dict) and response.get("error"):
                            raise FreebuffRunnerError("Freebuff rejected the run instruction")
                        with self._lock:
                            current = self.store.get(run_id) or {}
                            if current.get("status") == "running" and not event.is_set():
                                baseline_data["instruction_accepted"] = True
                                self.store.update(run_id, freebuff_baseline=baseline_data)
        except FreebuffRunnerError as exc:
            if not event.is_set():
                self._finish_if_active(
                    run_id, event, status="failed", finished_at=_now_iso(), error=str(exc),
                )
                with self._lock:
                    if self._threads.get(run_id) is None:
                        self._cancel_events.pop(run_id, None)
                        self._operation_locks.pop(run_id, None)
                        self._baselines.pop(run_id, None)
                raise
        except FreebuffOrchestratorError as exc:
            if not event.is_set():
                safe_error = str(exc)
                self._finish_if_active(
                    run_id, event, status="failed", finished_at=_now_iso(), error=safe_error,
                )
                with self._lock:
                    if self._threads.get(run_id) is None:
                        self._cancel_events.pop(run_id, None)
                        self._operation_locks.pop(run_id, None)
                        self._baselines.pop(run_id, None)
                raise FreebuffRunnerError(safe_error) from None
        except Exception:
            if not event.is_set():
                self._finish_if_active(
                    run_id, event, status="failed", finished_at=_now_iso(),
                    error="Freebuff run could not be started safely.",
                )
                with self._lock:
                    if self._threads.get(run_id) is None:
                        self._cancel_events.pop(run_id, None)
                        self._operation_locks.pop(run_id, None)
                        self._baselines.pop(run_id, None)
                raise FreebuffRunnerError("Freebuff run could not be started safely") from None

        with self._lock:
            current = self.store.get(run_id) or {}
            if event.is_set():
                # cancel() owns the terminal transition and will mark cancelled only
                # after POST /stop plus stable idle snapshots have been verified.
                return
            if current.get("status") != "running":
                self._cancel_events.pop(run_id, None)
                self._operation_locks.pop(run_id, None)
                self._baselines.pop(run_id, None)
                return
            watcher = threading.Thread(
                target=self._watch, args=(run_id,), daemon=True, name=f"freebuff-{run_id}",
            )
            self._threads[run_id] = watcher
        watcher.start()

    def _watch(self, run_id: str) -> None:
        run = self.store.get(run_id) or {}
        baseline = self._baselines.get(run_id) or run.get("freebuff_baseline") or {}
        deadline = time.monotonic() + self.completion_timeout_s
        last_signature: tuple[int, str | None] | None = None
        try:
            while time.monotonic() < deadline:
                event = self._cancel_events.get(run_id)
                if event is not None and event.is_set():
                    return
                thread, messages = self._get_thread(self.manager, str(baseline["external_thread_id"]))
                if event is not None and event.is_set():
                    return
                thread_state = str(thread.get("turnState") or "").lower()
                new_assistants = self._new_assistant_messages(messages, baseline)
                signature = (
                    len(messages),
                    _message_identity(new_assistants[-1]) if new_assistants else None,
                    _assistant_text(new_assistants[-1]) if new_assistants else None,
                    thread_state,
                )
                if new_assistants and signature != last_signature:
                    last_signature = signature
                    with self._lock:
                        if event is not None and event.is_set():
                            return
                        current = self.store.get(run_id) or {}
                        if current.get("status") not in {"queued", "running"}:
                            return
                        self.store.update(
                            run_id,
                            completed_message_count=len(messages),
                        )
                if new_assistants and self._completion_evidence(
                    messages, new_assistants[-1], baseline, thread_state=thread_state,
                ):
                    result = _assistant_text(new_assistants[-1]).strip()
                    if not result:
                        raise FreebuffRunnerError("Freebuff committed an empty assistant response")
                    with self._lock:
                        if event is not None and event.is_set():
                            return
                        current = self.store.get(run_id) or {}
                        if current.get("status") not in {"queued", "running"}:
                            return
                        self.store.update(
                            run_id,
                            status="completed",
                            finished_at=_now_iso(),
                            result_summary=result[:MAX_RESULT_CHARS],
                            completed_message_count=len(messages),
                            freebuff_baseline=baseline,
                            error=None,
                        )
                    return
                time.sleep(self.poll_interval_s)
            self._finish_if_active(
                run_id, event,
                status="failed",
                finished_at=_now_iso(),
                error="Freebuff run timed out before a committed assistant response was observed.",
                freebuff_baseline=baseline,
            )
        except FreebuffOrchestratorError as exc:
            self._finish_if_active(
                run_id, self._cancel_events.get(run_id), status="failed",
                finished_at=_now_iso(), error=str(exc),
            )
        except FreebuffRunnerError as exc:
            self._finish_if_active(
                run_id, self._cancel_events.get(run_id), status="failed",
                finished_at=_now_iso(), error=str(exc),
            )
        except Exception as exc:
            # Persist no exception payload: an unexpected transport/schema error might
            # contain server data. Keep the traceback only in the local test process.
            self._finish_if_active(
                run_id, self._cancel_events.get(run_id), status="failed", finished_at=_now_iso(),
                error=f"Freebuff run status could not be recovered safely ({type(exc).__name__}).",
            )
        finally:
            with self._lock:
                self._threads.pop(run_id, None)
                event = self._cancel_events.get(run_id)
                current = self.store.get(run_id) or {}
                # Keep the cancellation claim alive until cancel() verifies idle and
                # persists cancelled; otherwise a second observer could restart a
                # watcher or allow a late result to win during the confirmation gap.
                if event is None or not event.is_set() or current.get("status") in TERMINAL_STATUSES:
                    self._cancel_events.pop(run_id, None)
                    self._operation_locks.pop(run_id, None)
                    self._baselines.pop(run_id, None)

    def _finish_if_active(self, run_id: str, event: threading.Event | None, **fields: Any) -> dict[str, Any] | None:
        """Make watcher terminal updates conditional on still owning an active run."""
        with self._lock:
            if event is not None and event.is_set():
                return self.store.get(run_id)
            current = self.store.get(run_id)
            if current is None or current.get("status") not in {"queued", "running"}:
                return current
            return self.store.update(run_id, **fields)

    @staticmethod
    def _new_assistant_messages(messages: list[dict[str, Any]], baseline: dict[str, Any]) -> list[dict[str, Any]]:
        # The orchestrator preserves persisted message order and appends the active
        # stream after them. Use the immutable baseline boundary, not a mutable
        # observed count; Freebuff committed messages may have only `ts` and no ID.
        baseline_count = int(baseline.get("message_count") or 0)
        expected_instruction = baseline.get("instruction_sha256")
        if not isinstance(expected_instruction, str) or not expected_instruction:
            return []
        # Bind the result to this exact appended user instruction. This prevents an
        # unrelated concurrent/manual turn in the same existing thread from being
        # mistaken for this AgentRun's result.
        matched_user_index = None
        for index in range(baseline_count, len(messages)):
            message = messages[index]
            if not _is_assistant(message):
                role = str(message.get("role") or message.get("type") or "").lower()
                if role in {"user", "user_message", "usermessage"}:
                    content = _content_text(message.get("content") or message.get("text") or message.get("parts") or message.get("message"))
                    digest = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
                    if digest == expected_instruction:
                        matched_user_index = index
        if matched_user_index is None:
            return []

        baseline_ids = set(baseline.get("assistant_ids") or [])
        output = []
        for index, message in enumerate(messages[matched_user_index + 1 :], matched_user_index + 1):
            role = str(message.get("role") or message.get("type") or "").lower()
            if role in {"user", "user_message", "usermessage"}:
                # A second user turn began before this assistant response committed;
                # do not attribute its response to this AgentRun.
                break
            if not _is_assistant(message):
                continue
            identity = _message_identity(message)
            if index < baseline_count or (identity and identity in baseline_ids):
                continue
            output.append(message)
        return [message for message in output if _is_committed(message)]

    @staticmethod
    def _completion_evidence(
        messages: list[dict[str, Any]], newest: dict[str, Any], baseline: dict[str, Any], *,
        thread_state: str | None = None,
    ) -> bool:
        # In this orchestrator contract, thread.turnState becomes idle only after the
        # persisted assistant message is committed. A live snapshot can be empty or
        # partial and is marked streamSeq, so neither its content nor stability suffices.
        return (
            thread_state == "idle"
            and _is_assistant(newest)
            and _is_committed(newest)
            and len(messages) > int(baseline.get("message_count") or 0)
        )

    def status(self, run_id: str) -> dict[str, Any]:
        return super().status(run_id)

    def collect_result(self, run_id: str) -> dict[str, Any]:
        run = super().collect_result(run_id)
        if run.get("status") == "completed" and not run.get("result_summary"):
            raise FreebuffRunnerError("Freebuff run completed without a result")
        return run

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self.store.get(run_id)
            if run is None:
                raise FreebuffRunnerError(f"알 수 없는 run: {run_id}")
            if run.get("status") in TERMINAL_STATUSES or run.get("status") == "interrupted":
                return run
            if run.get("status") == "queued":
                return self.store.update(
                    run_id, status="cancelled", finished_at=_now_iso(),
                    error="사용자가 실행을 중단했습니다.",
                )
            baseline = self._baselines.get(run_id) or run.get("freebuff_baseline") or {}
            thread_id = baseline.get("external_thread_id") or run.get("external_thread_id")
            if not thread_id:
                raise FreebuffRunnerError("Freebuff run has no bound thread metadata")
            event = self._cancel_events.setdefault(run_id, threading.Event())
            event.set()  # watcher completion/errors can no longer win after this point
            operation_lock = self._operation_locks.setdefault(run_id, threading.Lock())

        # Wait for a concurrent start POST to finish before stopping the thread, so
        # cancellation cannot be overtaken by a late instruction request.
        with operation_lock:
            try:
                response = self._request_json(
                    self.manager, f"/api/thread/{thread_id}/stop", method="POST", body={},
                )
                if isinstance(response, dict) and response.get("error"):
                    raise FreebuffRunnerError("Freebuff rejected the stop request")
            except (FreebuffOrchestratorError, FreebuffRunnerError) as exc:
                safe_error = str(exc) if isinstance(exc, FreebuffRunnerError) else str(exc)
                with self._lock:
                    current = self.store.get(run_id) or run
                    if current.get("status") in {"queued", "running"}:
                        self.store.update(
                            run_id, status="failed", finished_at=_now_iso(),
                            error=f"Freebuff stop failed: {safe_error}",
                        )
                raise FreebuffRunnerError(f"Freebuff stop failed: {safe_error}") from None

        deadline = time.monotonic() + self.cancel_confirm_timeout_s
        previous: tuple[int, str | None, str | None, str] | None = None
        stable_observations = 0
        while time.monotonic() < deadline:
            try:
                thread, messages = self._get_thread(self.manager, str(thread_id))
            except (FreebuffOrchestratorError, FreebuffRunnerError) as exc:
                with self._lock:
                    current = self.store.get(run_id) or run
                    if current.get("status") in {"queued", "running"}:
                        self.store.update(
                            run_id, status="failed", finished_at=_now_iso(),
                            error="Freebuff cancellation could not be verified safely.",
                        )
                raise FreebuffRunnerError("Freebuff cancellation could not be verified safely") from None
            new_assistants = self._new_assistant_messages(messages, baseline)
            latest = new_assistants[-1] if new_assistants else None
            thread_state = str(thread.get("turnState") or "").lower()
            live_assistant = any(_is_assistant(message) and "streamSeq" in message for message in messages)
            signature = (
                len(messages),
                _message_identity(latest) if latest else None,
                _assistant_text(latest) if latest else None,
                thread_state,
            )
            if thread_state != "idle" or live_assistant:
                previous = None
                stable_observations = 0
            elif signature == previous:
                stable_observations += 1
            else:
                previous = signature
                stable_observations = 0
            if stable_observations >= 2:
                with self._lock:
                    current = self.store.get(run_id) or run
                    if current.get("status") in TERMINAL_STATUSES:
                        return current
                    if not event.is_set():
                        return current
                    cancelled = self.store.update(
                        run_id,
                        status="cancelled",
                        finished_at=_now_iso(),
                        completed_message_count=len(messages),
                        error="사용자가 실행을 중단했습니다.",
                    )
                    if self._threads.get(run_id) is None:
                        self._cancel_events.pop(run_id, None)
                        self._operation_locks.pop(run_id, None)
                        self._baselines.pop(run_id, None)
                    return cancelled
            time.sleep(self.poll_interval_s)

        with self._lock:
            current = self.store.get(run_id) or run
            if current.get("status") in {"queued", "running"}:
                self.store.update(
                    run_id, status="failed", finished_at=_now_iso(),
                    error="Freebuff stop was requested but output cessation could not be confirmed.",
                )
            if self._threads.get(run_id) is None:
                self._cancel_events.pop(run_id, None)
                self._operation_locks.pop(run_id, None)
                self._baselines.pop(run_id, None)
        raise FreebuffRunnerError("Freebuff stop was requested but output cessation could not be confirmed")


    def cancel_all(self) -> None:
        for run in self.store.list_runs(limit=100):
            if run.get("agent_type") == "freebuff" and run.get("status") in {"queued", "running"}:
                try:
                    self.cancel(run["run_id"])
                except AgentRunError:
                    pass
        self.manager.cleanup()
