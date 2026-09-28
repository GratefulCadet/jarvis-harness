# Deferred Milestone: PiP ↔ Main Signature Transition

**Status:** Deferred; preserve as a separate milestone after Workspace Explorer, path awareness, PiP drag, and file-creation destination UX are complete.

**Scope of this document:** Record the intended experience, constraints, diagnostics required before implementation, and acceptance criteria. This document does not authorize implementation in the current milestone.

**Authority:** The executable Electron app is authoritative for implementation. `jarvisSourceFiles/` is a historical/prototype reference snapshot only and must not be treated as current production source.

## North star

The transition should feel like one JARVIS Core changing presence, not like switching between two unrelated apps:

> “The JARVIS beside me unfolded into my workspace,” and later, “JARVIS condensed back into a small presence.”

This is the signature 10% of the `90% restrained + 10% signature interaction` principle. It is not merely native-window resizing or a fade.

```text
PiP → Core opens space → Main / Command Center
Main → workspace gathers around Core → PiP
```

## 1. Motion intent and hierarchy

### Opening: PiP → Main

1. User activates the PiP Core or its explicit Main-open affordance.
2. Capture the current valid PiP bounds/position before changing native window mode.
3. Change the native Electron window to the target workArea once.
4. Render the visual Core at the former PiP screen position within the new renderer surface.
5. Move the Core toward the Main anchor while the background reveals outward from the Core.
6. Let workspace content settle only after the Core has established the motion; finish in a fully interactive Main state.

Desired hierarchy:

```text
Core movement > workspace reveal > content appearance
```

The Core must not look as if it pops into place before the workspace. Content must not pop out ahead of the Core.

### Closing: Main → PiP

1. Start from the central Core as the primary affordance; Escape may remain a secondary shortcut.
2. Stabilize or withdraw interactive content first so controls do not jump away under the pointer.
3. Gather the workspace/background toward the Core.
4. Move the visual Core toward the most recently saved, valid PiP target position.
5. Only after the visual closing phase, switch native window bounds/mode to PiP.
6. Settle/fade the PiP surface into its normal interactive state.

The central Core remains the primary Main → PiP control. Do not add a corner/header PiP button as the main path. Provide discoverability with an appropriate cursor, subtle hover/luminosity/scale response, and/or a concise hint without allowing the Core hit area to cover Composer, Runtime, File Editor, or other controls.

### Reference timings (starting points, not hard contracts)

```text
Opening phase                 ≈ 500 ms
Core transform                ≈ 750 ms
Circular background reveal    ≈ 2300 ms

Closing preparation           ≈ 240 ms
Closing Core movement         ≈ 420 ms
PiP settle/fade               ≈ 160 ms

Easing                        cubic-bezier(0.22, 1, 0.36, 1)
```

Tune from the real current layout and visual evidence. Preserve the hierarchy and sense of continuity rather than forcing these exact durations.

## 2. Native-window and visual-motion boundary

Keep native state changes separate from frame-by-frame visual animation:

```text
Native window state change ≠ visual animation
```

Preferred approach:

- **Opening:** capture PiP bounds → perform one native change to the target workArea → invert the renderer Core's visual position to the captured PiP screen position → animate Core and reveal using renderer transforms/clip-path/opacity.
- **Closing:** capture/resolve the valid PiP target → animate the renderer workspace and Core toward that target → when visual motion is complete, perform one native change to PiP bounds → settle the PiP surface.

Do not animate Electron native bounds every frame. Avoid introducing a second, elaborate `transitionWindow` choreography layer if a small explicit phase flow and renderer motion can own the experience.

## 3. PiP drag, saved position, and display changes

A user-dragged PiP location is the preferred return target:

```text
user moves PiP
→ persist latest valid PiP bounds
→ open Main from that location
→ close Main toward that location
```

On use, validate/correct the saved bounds against the currently visible workArea. Handle monitor removal, resolution/workArea changes, and taskbar/dock changes by clamping or selecting a visible fallback position; PiP must never reopen off-screen. Respect display origins and multi-monitor coordinate spaces, including negative coordinates where applicable.

Drag and transitions must coexist:

- Keep PiP draggable regions distinct from interactive controls.
- PiP inputs, buttons, timer, and approval controls must retain explicit no-drag behavior.
- Core activation must still work reliably and must not be swallowed as a drag.
- Persist the latest **valid** dragged bounds, not a stale initial/default position.

## 4. Core as the transition anchor

Main and PiP should share one visual identity and a clear spatial anchor: the Core. The visual system may contain multiple rings/orbits, but they should read as one coordinated object, not as unrelated animation systems.

Keep the Core hit target usable throughout normal states and ensure transition visuals do not cause controls to shift their hit areas unexpectedly, disappear while focused, or overlap Composer/Runtime/File Editor controls. Keyboard activation and Escape behavior should remain predictable.

## 5. Avoid

- Moving native Electron bounds on every animation frame.
- Building a separate complex native-window choreography subsystem without demonstrated need.
- Animating each ring with an unrelated timing/easing system.
- Giving Core, background, and contents unrelated easings that destroy a shared motion language.
- Adding blur, glow, particles, or other decoration to hide a broken spatial relationship.
- Moving or removing button hit areas as a side effect of the transition.
- Making a separate corner/header PiP control the primary return path.
- Copying legacy reference code or appearance wholesale.
- Treating reference timings as fixed requirements.

## 6. Required diagnosis before implementation

Do not begin by editing CSS. On the authoritative Electron checkout, first inspect and report:

### A. Current transition

Trace the real PiP → Main and Main → PiP user paths, phase/state ownership, failure/abort behavior, and the user-visible surfaces. Distinguish actual current behavior from the legacy JSX snapshot and prior reports.

### B. Native window state

Identify exactly where Electron window bounds, mode switching, workArea selection, and persisted PiP position are owned (main process, preload bridge, renderer, or state store). Establish whether a dragged PiP location is actually saved and restored, and how display changes are handled.

### C. Core geometry

Measure actual PiP Core and Main Core size and screen coordinates, including the relation between Electron window bounds, renderer viewport, display scale factor, and workArea. Capture at least one representative monitor configuration. Do not infer screen coordinates from CSS viewport offsets alone.

### D. Existing motion

Inventory reusable transition phases, CSS transforms/transitions/clip-path/opacity, Core/ring animation ownership, content entrances, reduced-motion behavior, and drag/no-drag regions. Identify any collision with Command Center entrance choreography.

### E. References

Before implementing, inspect `jarvisSourceFiles/` and `motionReferenceSources/` for useful timing, spatial relationships, hierarchy, easing, and transition ideas. Also read `JARVIS_UI_INTERACTION_DESIGN_PRINCIPLES.md` if it exists. These are design references, not production source; extract principles and adapt them.

### F. Minimal plan

Report the smallest implementation plan that separates native state changes from renderer visual motion, preserves the user's PiP return position, coordinates Core/background/content, and keeps controls interactive. Only then implement.

## 7. Reference inspection already performed in this checkout

The following is **provisional evidence from the attached legacy/prototype snapshot**, not confirmation of the current executable Electron implementation:

- `jarvisSourceFiles/App.jsx` contains a small explicit view/phase reducer and renderer orchestration for PiP/Main intent. It calls `window.jarvisWindow.prepareCommandCenter()`, `expandCommandCenter()`, and `collapseToPip()`, and derives `--pip-offset-x/y` from returned geometry for the opening/closing visuals.
- `jarvisSourceFiles/App.css` contains a FLIP-like visual approach: Core translation/scale and circular `clip-path` reveal, plus phased closing and PiP fade. The snapshot has starting timings close to the references above and uses the stated cubic-bezier for major motions.
- `jarvisSourceFiles/CommandCenter.jsx` explicitly separates its internal content entrance choreography from the PiP/fullscreen transition, and its anime.js scope checks `prefers-reduced-motion` before running that entrance.
- The snapshot does not include the Electron main/preload implementation. Therefore it does **not** establish native bounds ownership, whether the native window changes exactly once, workArea/display handling, actual screen coordinates, whether the latest PiP drag position is persisted and restored, or whether bridge errors are caught elsewhere.
- **Additional sequence detail:** `showFullscreenSurface` requires both Main view and `phase === idle`. Opening therefore mounts the Main surface only after the 500 ms JS wait, whereas the Core transform is authored for 750 ms and the circular reveal for 2300 ms. `CommandCenter` then begins its own entrance choreography (orbit guide 760 ms, panel timeline defaults 520 ms with staggered labels), so content entrance can overlap the tail of Core movement and the much longer reveal. This is a measurable source-level ordering risk against the intended hierarchy, not a visually confirmed defect.
- **Closing sequence detail:** entering `closing-prep` immediately makes `showFullscreenSurface` false and unmounts the Command Center before the 240 ms prep and 420 ms Core/reveal motion. This is a concrete lifecycle behavior in the snapshot and a likely perceived discontinuity candidate; only actual app screenshots/video can establish how severe it looks.
- **Error recovery detail:** `prepareCommandCenter()` false/null returns before a phase change; false results from `expandCommandCenter()` / `collapseToPip()` dispatch abort events. Rejected promises are not caught in the visible renderer functions, so the state may remain non-idle after a rejection. Bridge rejection semantics and any outer error boundary are unknown.
- **Reduced-motion detail:** the snapshot disables `.core-orbit` animation and the Command Center anime.js entrance under reduced motion, but does not visibly disable the PiP/Main phase transforms and circular reveal. Treat as a possible inconsistency to verify in the executable app.
- **Core and interaction geometry:** `.jarvis-shell` centers a 260×260px `.pip-interaction-zone`; `.core-outer-ring` is 240×240 CSS px, scaled to nominally 180×180 CSS px in PiP. The center `.core-trigger` is 96×96 CSS px before ancestor scaling; outer/middle ring regions are drag-enabled and the button/Quick PiP controls are no-drag. These dimensions are CSS declarations only; they do not provide actual screen coordinates, device-pixel bounds, or proof of native click/drag hit-testing.
- **Structural note:** the snapshot places the phase machine/orchestration in `App.jsx` (802 lines), while one `App.css` file (7,746 lines in this checkout) owns page layout, Core/rings, transition phases, PiP controls, and other surfaces. This is only a maintainability observation about the legacy snapshot; do not copy its file boundaries as production architecture.
- **Minimal future plan if these patterns still exist in the authoritative app:** keep native window switching and geometry in the existing native bridge; derive the visual origin from validated current PiP bounds; let one renderer phase owner coordinate Core movement, workspace reveal, and content staging; align phase completion with the intended motion hierarchy; preserve content/hit targets until their planned withdrawal; and explicitly recover phase state for false/rejected native operations and reduced motion. Confirm or revise only after real-source inspection and visual evidence.

These observations are code-level facts or risks in the legacy snapshot, not a claim that the current application exhibits the same behavior or defects. The opening timer/class handoff deserves particular inspection: the 500 ms phase wait is shorter than both CSS motion durations, but removing the phase class may alter/cancel the in-flight reveal, so do not assume the 2300 ms reveal continues or judge it without frame evidence.

Reference limitation: directory listing confirmed general motion-reference filenames, but direct reads of selected reference components were blocked in this environment. Their detailed implementations were not reviewed; inspect them in the authoritative checkout if accessible. The interaction-principles file was not found.

### A–F diagnostic status from available evidence

| Area | Finding in this checkout | Confidence / limitation |
|---|---|---|
| A. Current transition | Renderer phases, intents, timing calls, false-result recovery, and Escape/Core affordances are visible in `jarvisSourceFiles/App.jsx`. | Confirmed only for the legacy snapshot; not the executable app. Rejected native calls and visual result remain unverified. |
| B. Window state | Renderer calls a `window.jarvisWindow` bridge for preparation/expand/collapse and consumes offset geometry. | Main/preload source is absent; bounds, workArea, drag persistence, monitor correction, and actual native calls cannot be traced. |
| C. Core geometry | CSS declares a 240px outer Core, 0.75 PiP scale, a 96px trigger, and 260px interaction zone. | CSS pixels only; no measured screen coordinates, bounds, or device scale factor. |
| D. Existing motion | CSS owns Core transform/reveal phases; CommandCenter owns a separate anime.js entrance. Reduced-motion handling for the major transition is not apparent in the snapshot. | Source-level inventory only; overlap/order requires actual Electron frames. |
| E. References | The motion directory lists general UI motion patterns. The principles document was not found; selected reference contents could not be read here. | No direct transition reference reviewed. Recheck in the authoritative checkout. |
| F. Minimal plan | Retain the existing native bridge as the single window-state owner; use validated PiP bounds as renderer's visual origin/return target; coordinate Core, reveal, and staged content with one explicit transition phase owner; recover bridge failures and honor reduced motion. | Provisional plan only; revise after A–E against current app and visible evidence. |

This diagnostic makes the limit explicit: the recommended task has been carried as far as the available checkout permits, but it is **not** a production A–F sign-off. The authoritative Electron source and a real Electron session are required before implementation decisions or visual claims. Do not carry snapshot assumptions forward as facts about production.

## 8. Newer source diagnosis reported from the authoritative Electron checkout

The following findings were supplied by a separate chat after it inspected the Electron app source. They are **reported source-level diagnostics, not independently reproduced in this Harness checkout**. The stated target was `C:\Users\USER\Documents\JARVIS\jarvis-app-v3-harness`; the report says no app source or milestone files were modified during diagnosis.

### Reported A–B: transition and native bounds

- PiP → Main: the `PresenceOrb` button calls `openCommandCenter()`. `prepareCommandCenter()` returns a PiP-relative offset, renderer stores it and enters `opening-start`, waits two frames, calls `expandCommandCenter()`, then keeps `opening` for about 500 ms before Main idle.
- Main → PiP: the central Main Core is primary and Escape secondary. Escape first closes an open file editor/context panel. Phases are `closing-prep` (~240 ms), `closing-moving` (~420 ms), `closing-swap`, native `collapseToPip()`, then `pip-fade` (~160 ms).
- Reported false bridge results dispatch abort phases; rejected bridge promises lack explicit catch/finally in the inspected renderer and may leave a transition phase stuck. Null `prepareCommandCenter()` returns before phase transition.
- Electron main is reported to own 280×280 PiP bounds, workArea selection/clamping, mode changes, and display-change correction. `expandCommandCenter()` and `collapseToPip()` each call `setBounds()` once. Preload forwards the three window IPC functions and does not own position state.
- `savedPipBounds` is reported as in-memory main-process state, debounced from the native window `moved` event and restored on collapse. Disk persistence across app restart was not established. Display metrics changes and removed-display events reportedly trigger clamping.

### Reported C–E: geometry, motion, references

- CSS values reported for the current app: PiP window 280×280 CSS px; V4 PiP orb 84×84 with a 92×92 click trigger; Main outer Core 240px scaled to 0.78; final Core trigger 136×136px. The Main anchor may shift with context/file state and responsive rules at ≤1000px. These are CSS declarations, not physical measurements.
- Actual PiP/Main screen coordinates, Electron bounds-to-viewport relationship, workArea, and display scale factor were **not measured**. No Electron process was running. A GUI acceptance attempt reportedly captured only the default Main screenshot before failing at Workspace Explorer fixture setup; this does not validate transition visuals or geometry.
- Existing renderer reducer/CSS phases provide Core transform and circular reveal. PiP drag regions and no-drag control regions exist. Current V4 rendered choreography and interaction were not confirmed in a live session. The report found no clear current workspace-content choreographer tied to transition phase; inspect the actual V4 components and frame sequence before deciding this.
- Reduced-motion handling reportedly covers decorative Core/PiP motion but not the main transition phases/timings. Verify the effective cascade and rendered behavior in the real app.
- The requested milestone document was absent from the Electron checkout and was not copied during diagnosis. The separate Harness copy is design intent, not app evidence.
- `motionReferenceSources/` dialog/toolbar/spotlight examples were read in that session; they are general motion patterns, not direct PiP↔Main transition precedents. `JARVIS_UI_INTERACTION_DESIGN_PRINCIPLES.md` was not found in the app checkout or the two checked parent locations. `JARVIS_V4_PROJECT_INSTRUCTIONS.md` exists but is a different document.

### Reported F: minimal plan and remaining gate

The reported plan is to keep Electron main as the single bounds/workArea owner; pass validated PiP bounds and a measured Main Core target to the renderer; after one native mode change, coordinate Core travel, reveal, and staged content under one transition phase owner; on close, withdraw content without losing focus/hit targets, animate toward the latest valid PiP position, then perform one native collapse. Explicitly handle false and rejected bridge results and reduced motion.

This is still **not a complete diagnostic sign-off**: the report explicitly leaves live Core geometry and rendered choreography unobserved. Do not implement against CSS guesses. Required next evidence is a working isolated Electron fixture/session, actual screen/window/Core coordinate measurements, and opening/closing frame captures. The report stated the working tree remained at its pre-investigation state; recheck `git status` in the app checkout before any subsequent work.

## 9. Implementation and visual acceptance

Use the real Electron app and inspect the visual sequence, not only build results or DOM phase flags. Save frame/screenshot evidence (or video) for at least:

```text
PiP idle
Opening start
Opening midpoint
Main settled
Closing start
Closing midpoint
PiP settled
```

### PiP → Main

- Activate from the real PiP Core (and any intended Main-open affordance).
- Native mode changes without a visible resize artifact masquerading as the animation.
- The Core appears to continue from its actual prior screen location and moves naturally to the Main anchor.
- Background reveal reads as Core-originated space opening.
- Composer/workspace content appears in the intended order, after the Core establishes motion.
- At completion all controls are visible, focused state is sane, and each control remains clickable.

### Main → PiP

- Central Core initiates the primary return path; Escape works as a secondary path.
- Content withdraws without clipping or jumping under an active pointer/focus.
- Workspace gathers toward the Core; the Core appears to travel to the user's latest PiP position.
- Native collapse happens after renderer closing motion, with no visible discontinuity.
- PiP returns wholly inside the current visible workArea, including after a monitor/workArea change.
- PiP input, buttons, drag, and approval controls remain usable and do not conflict.

### Repetition and recovery

Repeat PiP → Main → PiP several times, including a PiP drag before opening and an Escape return. Verify:

- no position or scale drift;
- no stuck opacity, clip-path, or phase class;
- no duplicated/missing Core;
- no stale transition phase after native API failure/abort;
- no lost input focus or unusable hit targets;
- the latest valid PiP position is restored each time.

The milestone is not complete on build/lint/DOM assertions alone. Perceived continuity must be judged from actual Electron screenshots/video and interaction.

## 9. Scope boundary and completion

This milestone begins only after the current Workspace Explorer, path awareness, PiP drag, and file-creation destination work is complete. The current milestone's scope remains stable PiP movement, working Core/controls, and reliable mode switching; it does not include implementing this signature animation.

Future implementation should first deliver diagnosis A–F, then the minimal plan, then the renderer/native work and real-Electron visual acceptance. Keep the change focused; do not use this milestone to redesign unrelated Workspace or execution flows.
