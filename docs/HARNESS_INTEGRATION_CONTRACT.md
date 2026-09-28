# Integration contract: Electron app and Python Harness are two checkouts

**Status:** Normative for both repositories. Written because the two projects have been
confused for a directory copy at least once.

## The one rule

The Electron app **does not contain** the Python Harness. It runs it from a separate
checkout on disk, resolved at runtime.

**Never copy `harness/`, `scripts/`, `configs/`, or `tests/test_*.py` from the Harness
repo into this app repo.** Those directories do not belong here.

## How the app finds the Harness

`electron/bridge-ipc.cjs` → `resolveHarnessHome()`:

1. `process.env.JARVIS_HARNESS_HOME` when set, otherwise
2. `path.join(os.homedir(), 'Desktop', 'FB_Soap_LocalLLM')`

`electron/bridge-manager.cjs` then spawns the child bridge process with
`cwd: this.harnessHome`, running `python -m scripts.harness_bridge`. The JSONL protocol
between the two is documented in `bridge-manager.cjs` and `harness_bridge.py`.

The same fallback path is duplicated in the app's test scripts
(`tests/audit_ux_clickthrough.cjs`, `verify_approve_link.cjs`, `verify_file_access.cjs`,
`verify_ux_continuity.cjs`, `verify_ux_drawer_actions.cjs`, `verify_ux_fixes.cjs`,
`test_gui_link_flow.cjs`, and others). Setting `JARVIS_HARNESS_HOME` overrides all of them.

The only Python in this repo is `tests/test_stt_worker_*.py`, which tests the in-process
STT worker. That is not the Harness.

## Why the boundary is enforced by structure

The app repo has no `harness/` and no `configs/`. The Harness repo has no `electron/`,
no `package.json`, and no `src/`. Each side can be built and reasoned about on its own.

Copying Harness code in would create a second, divergent copy of canonical logic. The
app would silently run one copy while the acceptance tests spawned the other, and a fix
applied in the real Harness repo would appear to have no effect.

## Which side owns what

| Concern | Owner |
|---|---|
| Conversation UI, PiP, approval screens, native capabilities | App repo (`src/`, `electron/`) |
| Model adapter, tool registry, tool execution, trace | Harness repo (`harness/`) |
| Local state (projects, tasks, pages, file refs) | Harness canonical store, via bridge |
| IPC transport and its protocol | Both sides, must stay in sync |

When a capability needs work on both sides, change each side in its own repo and commit
it there. Do not merge the checkouts, vendor the directories, or add a submodule.

## Where to start when the two disagree

This repo is authoritative for app behavior and the rendered UI. The Harness repo is
authoritative for model, tool, and state behavior. Neither is authoritative for the
other's half of the bridge protocol; if the protocol changes, both sides change in the
same pass.

## Working with a scratch state directory

For isolated acceptance runs, point the Harness at a scratch state directory rather than
copying anything. `JARVIS_BRIDGE_MEMORY_DIR` overrides the memory directory, and the app's
E2E tests pass `JARVIS_HARNESS_HOME` through to the spawned bridge. See
`electron/bridge-ipc.cjs` for the resolution order.
