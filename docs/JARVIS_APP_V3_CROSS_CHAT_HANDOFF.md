# Cross-chat handoff: JARVIS Electron app

Use this note when continuing work in the separate Electron application checkout. It consolidates prior-thread context so the next agent does not mistake the current Python Harness checkout for the app or redo completed work blindly.

## Target and authority

- Target app checkout previously identified: `C:\Users\USER\Documents\JARVIS\jarvis-app-v3-harness`.
- This document lives in the separate Python Harness repository. It is not the app source tree.
- In the app checkout, inspect `git status --short --branch` before edits. Existing modifications are shared work; preserve them and do not overwrite/stage unrelated changes.
- Actual current Electron source and a real Electron session are authoritative. `jarvisSourceFiles/` in the Harness repo is only a legacy design/code snapshot.

## Immediate task to finish: narrow-width Workspace Explorer playtest

Mission: exercise the real Explorer and confirm workspace-root path, selected folder path, active-file path, and linked Task remain readable/discoverable at narrower widths. Make a minimal fix and real-surface regression assertion only if a concrete failure is visually confirmed. Otherwise leave product code unchanged. If product code changes, rerun Electron acceptance, build, and lint.

### Prior work reported in the earlier Electron thread (historical; recheck current checkout)

- A scoped CSS fix was made in `src/App.css`: restore the Explorer overview inside `.v4-context-drawer` with `display: block`, overriding a narrow-width rule that hid `.tree-prototype-overview`. The V4 Explorer is mounted inside that overview.
- A real Electron acceptance script was added as `tests/verify_workspace_explorer_pip.cjs` (reported untracked at the time). It exercises an isolated fixture through Electron/Vite/IPC/files/writes/PiP drag and captures screenshots in `tests/artifacts/workspace-explorer-pip/`.
- The script was extended to inspect renderer viewport widths **900×800, 760×800, and 600×800** through CDP `Emulation.setDeviceMetricsOverride`. These are emulated renderer viewports, **not native BrowserWindow resizing**.
- The last reported Electron run passed with `WORKSPACE EXPLORER + PIP ACCEPTANCE PASS`. Geometry checks found nonzero path rectangles at all three widths. Reported path heights: at 900px, root/folder about 88px and active path about 80px; at 760px and 600px, root/folder about 64.5px and active path about 69.2px. Path text was 10px at 760/600.
- The linked Task was scrolled into the list viewport at all tested widths; the test recorded that it was reachable after list scroll. This does not establish that it is initially visible without scrolling.
- Screenshot review noted small absolute-path/metadata text in the narrow drawer but did not establish clipping or unusability. DOM geometry alone cannot establish legibility.
- Newer progress reported in the other chat: initial `node tests/verify_workspace_explorer_pip.cjs` failed. It passed when run with `JARVIS_E2E_VERBOSE=1` and `JARVIS_HARNESS_HOME='C:\\Users\\USER\\Desktop\\FB_Soap_LocalLLM'`. The latest pasted transcript then showed the screenshot preview still being explored; it did not include a clear final visual judgment or final build/lint result. Recheck the script output and confirm whether screenshots correspond to that passing run before treating visual review as complete.
- Earlier build/lint reportedly passed before a small formatting cleanup to the acceptance script. After cleanup, `node --check tests/verify_workspace_explorer_pip.cjs` and a targeted `git diff --check` reportedly passed. Do not assume a complete post-cleanup build/lint has passed unless the other chat has now run them.
- The other chat also inspected `tests/verify_file_access.cjs`, `tests/verify_approve_link.cjs`, `electron/bridge-ipc.cjs`, `src/useJarvisRuntime.js`, and `electron/bridge-manager.cjs` while investigating fixture setup. No result in the pasted transcript establishes that these other acceptance scripts were run.
- A temporary `tests/artifacts/workspace-explorer-pip/visual-review.html` was created in that other chat for screenshot preview. If it was only review scaffolding, remove it at the end after confirming it is not user-owned or needed; do not delete blindly if ownership is unclear.
- Native resize attempts were unsuccessful (CDP native-window API unsupported; PowerShell `SetWindowPos` helper timed out). Continue to describe the widths accurately as renderer viewport emulation unless native window resizing is independently achieved.

### Safe next steps in the app checkout

1. Inspect current branch/status and the exact files/artifacts. Do not assume the historical working tree is unchanged.
2. Visually inspect the three narrow-width screenshots. Judge truncation, text contrast/size, full-path discoverability, Task-link discoverability, and whether scrolling is apparent/usable.
3. Rerun `node tests/verify_workspace_explorer_pip.cjs`.
4. Rerun `npm run build` and `npm run lint` if the checkout/package scripts still match the prior notes.
5. If no concrete visual failure exists, make no product change; report the tested/emulated dimensions and residual small-text friction honestly. If a concrete failure exists, implement the smallest scoped fix, add/adjust a real-surface assertion, and rerun all three checks.
6. Preserve shared/unrelated changes; the historical app checkout had several modified files beyond Explorer, so inspect before editing.

## Deferred milestone and newer app-checkout diagnosis

The separate Harness repo contains `docs/PIP_MAIN_SIGNATURE_TRANSITION_MILESTONE.md`. It preserves the PiP ↔ Main signature-transition intent, constraints, and real-Electron visual acceptance. Do not treat it as current app evidence.

A separate chat has since reported source inspection against the authoritative Electron checkout (not independently reproduced here):

- PiP → Main: `PresenceOrb` → `openCommandCenter()` → `prepareCommandCenter()` offset → `opening-start` → two frames → `expandCommandCenter()` → `opening` for ~500ms → Main idle.
- Main → PiP: center Core or Escape (Escape first closes editor/context overlays) → `closing-prep` ~240ms → `closing-moving` ~420ms → `closing-swap` → `collapseToPip()` → PiP fade ~160ms.
- Electron main reportedly owns 280×280 PiP bounds, workArea/clamping and display-change correction. A `savedPipBounds` memory value is debounced from native `moved` and restored on collapse; persistence across restarts was not established. Main expand/collapse each reportedly perform one `setBounds()`.
- The inspected renderer reportedly handles false native results but lacks visible catch/finally for rejected bridge calls, so stuck phases remain a candidate risk.
- Reported CSS dimensions: PiP orb 84×84 with 92×92 trigger; Main outer Core 240px at scale .78; final trigger 136×136px. Main anchor can vary with context/file and responsive layout. **Actual screen geometry, workArea and scale factor were not measured.**
- The attempted GUI acceptance reportedly stopped after a default Main screenshot and failed at Workspace Explorer fixture setup; no opening/closing frames or rendered transition check resulted. A live visual diagnosis is still pending.
- The app checkout reportedly did not contain the milestone document. General dialog/toolbar/spotlight references were inspected; no direct transition reference or `JARVIS_UI_INTERACTION_DESIGN_PRINCIPLES.md` was found. These are reports from another chat, not a fresh status check.

The milestone document in this Harness repo has been updated with these findings clearly labeled as reported and independently unverified. Before implementing the signature animation, obtain an isolated working Electron fixture, measure real window/Core screen coordinates, capture transition frames, then refine the smallest plan. Do not implement against CSS guesses. Do not add transition work to the Workspace Explorer pass.

The latest reported diagnosis left the app worktree unchanged, but recheck `git status --short --branch` in the app checkout before any work.


## Transfer these notes into the JARVIS app checkout

This Harness workspace cannot write into the separate Electron checkout. The app checkout previously identified is `C:\Users\USER\Documents\JARVIS\jarvis-app-v3-harness`. To transfer only the documents created for this effort, first check whether destination files already exist; do not overwrite them. From a PowerShell session with access to both repositories, use `Copy-Item -LiteralPath ... -Destination ... -ErrorAction Stop` only for destination files confirmed absent:

- Source: `C:\Users\USER\Desktop\FB_Soap_LocalLLM\docs\PIP_MAIN_SIGNATURE_TRANSITION_MILESTONE.md`
- Destination: `C:\Users\USER\Documents\JARVIS\jarvis-app-v3-harness\docs\PIP_MAIN_SIGNATURE_TRANSITION_MILESTONE.md`
- Source: `C:\Users\USER\Desktop\FB_Soap_LocalLLM\docs\JARVIS_APP_V3_CROSS_CHAT_HANDOFF.md`
- Destination: `C:\Users\USER\Documents\JARVIS\jarvis-app-v3-harness\docs\JARVIS_APP_V3_CROSS_CHAT_HANDOFF.md`

If a destination exists, compare it and merge the relevant handoff information instead of replacing it. Do not copy the Python Harness source, generated artifacts, or unrelated dirty files into the app repo.

When starting/continuing the app chat, paste:

> Read `docs/JARVIS_APP_V3_CROSS_CHAT_HANDOFF.md` and `docs/PIP_MAIN_SIGNATURE_TRANSITION_MILESTONE.md` first. Continue the narrow-width real-Electron Workspace Explorer visual review and finish the outstanding acceptance/build/lint checks. Recheck the app worktree and preserve shared changes. Treat historical results and diagnostics from other chats as reported context, not fresh verification. Do not implement the separate PiP ↔ Main signature-transition milestone during this Workspace Explorer pass.
