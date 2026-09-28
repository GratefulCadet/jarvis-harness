# FreebuffRunner V1 — Harness completion and Electron handoff

**Status:** Harness V1 DoD verified in the local checkout; Electron implementation intentionally deferred.

## Delivered Harness boundary

- `FreebuffOrchestratorManager` uses the installed Freebuff Desktop orchestrator with a minimal, private copy of only the authenticated `https://www.codebuff.com` auth entry. It does not modify the user's source settings; it launches on an ephemeral port, verifies readiness by launch ID/PID and health, sends the internal launch ID on loopback requests, reuses one child, detects child death, restarts, and cleans up child/temp auth state.
- `FreebuffRunner` resolves a run through its persisted `session_id` to an active Freebuff `AgentSession`; the session's workspace and existing external thread are authoritative. It refuses empty, non-idle, live-stream, inactive, archived, broken, or awaiting-auth sessions. Renderer-supplied workspace/thread/model fields do not choose the target.
- A run's result is bound to its newly appended user instruction, waits for an idle snapshot containing a new committed assistant message, and extracts only user-visible text parts. Reasoning/ad parts are excluded; empty committed results fail. Existing Codex runner behavior remains available.
- Cancellation is serialized against the instruction POST, calls Freebuff stop, waits for stable idle snapshots, then writes `cancelled`. A watcher cannot overwrite a cancel claim or a terminal run.
- The bridge dispatches start/status/result/cancel from persisted run agent type and tears down the manager on bridge shutdown.

## Verification evidence

- Deterministic focused suites: manager, runner, AgentRun, and AgentSession tests pass (104 tests; one platform-specific permission test skipped in the final focused run).
- Authenticated scratch E2E: `python -m scripts.freebuff_e2e` passed exact `FREEBUFF_RUNNER_OK` completion and a separate harmless long-turn cancellation; verified output stopped, the scratch thread stayed usable, and cleanup removed the manager's temporary credential state. Scratch memory/workspace/thread were isolated; no existing user thread was selected.
- Full suite and gate were run during this milestone; final clean verification after the last code/test edits is recorded in the delivery transcript. Gate warnings about large pre-existing/shared diffs and untracked checkout content are informational, not failures.
- User Freebuff `state.json` was read-only; its SHA256 remained `ad67f5fffc683e5b3937b5e39a3eb55574c799d7ef0952491c11e5f7951ee80a` across E2E runs. No token was printed. Temporary E2E scratch roots were removed.

## NEXT — separate Electron checkout

1. Inspect the current Electron bridge manager/IPC contract and existing UI surfaces before editing; do not copy Harness Python into the app repository.
2. Add the minimal IPC/protocol support for session-bound `agent_run_start`, `agent_run_status`, `agent_run_result`, and `agent_run_cancel`; keep Python Harness as the runner authority.
3. Integrate Freebuff selection and existing AgentSession binding without renderer-supplied thread/workspace/model authority. Preserve current Codex path and present generic AgentRun state/result/cancel semantics.
4. Add GUI acceptance for existing-thread selection, a successful scratch response, cancel while actively generating, and recovery/cleanup. Use only a scratch thread/workspace and verify no normal conversation is touched.
5. Run the Electron app's build/lint/tests and GUI acceptance in its own checkout; update both sides only when the bridge protocol needs coordinated changes.

## LATER — orchestration expansion (not part of V1)

- Manual multi-agent workflows and explicit agent recommendations.
- A stable capability schema and controlled routing (no automatic routing by default).
- Planner/Reviewer roles, structured review handoff, and context-aware orchestration.
- Durable overnight jobs, progress/resume controls, and policy-bound long-running execution.

No Electron code, automatic multi-agent routing, Planner/Reviewer flow, or overnight job system was implemented in this Harness milestone.
