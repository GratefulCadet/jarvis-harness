# JARVIS V4 Product Direction

**Status:** Binding product direction for the current JARVIS V4 interaction / UX redesign phase.

**Scope:** Product interaction, UX priorities, and development decisions. This document does not replace proven backend identity, runtime, filesystem, or permission contracts.

**Authority:** For JARVIS product decisions, this current contract supersedes historical UI assumptions, dated roadmap snapshots, and prior milestone reports. Actual current code remains authoritative about what is implemented.

## 1. Product identity

JARVIS itself is the product.

Task Manager, File Explorer, Project hierarchy, Knowledge, Pages, Timer, Checklist, Focus, and other surfaces are capabilities and context exposed by JARVIS. Users should not have to choose a subsystem before they can begin useful interaction.

The intended default flow is:

```text
user speaks or types
→ JARVIS resolves intent
→ JARVIS resolves relevant context
→ JARVIS reads, acts, or surfaces information
→ supporting UI appears only when useful
```

A user should be able to begin useful work without understanding JARVIS's internal ontology.

## 2. Assistant-first

The primary entry is JARVIS itself. Fullscreen input, PiP, voice, keyboard input, and a future global invocation are invocation methods for the **same runtime**.

Do not create separate reasoning systems for voice, chat, PiP, or command modes. Typed and spoken input should converge on the same JARVIS command path as early as practical.

Do not require users to manually select ASK, ACT, NAVIGATE, FOCUS, or equivalent modes unless later evidence proves that explicit mode selection is beneficial. Such distinctions may exist internally in Harness intent and policy.

SYSTEM, FILES, KNOWLEDGE, and EXECUTION must not become mandatory first-step mode choices. Context browsers may exist, but natural-language interaction must not require navigating them first.

## 3. Workspace-grounded

Real filesystem and Workspace context are primary grounding sources. Users should be able to work directly with their real Workspace without first navigating a Project hierarchy.

Preserve the binding distinction:

```text
Project != Folder
```

WorkspaceRoot and FileRef remain stable backend infrastructure. Workspace-grounded does **not** mean File-Explorer-first; JARVIS remains the primary interaction layer.

A future human-readable `JARVIS.md` may describe:

- workspace purpose
- important paths
- conventions
- agent behavior

`JARVIS.md` is a design candidate, not a mandatory filesystem standard in V4.

## 4. Focus-on-demand

EXECUTION is not assumed to remain a permanent top-level fullscreen surface. Treat execution primarily as a state.

```text
normal JARVIS interaction
→ user begins concrete work
→ Focus Session activates
→ work completes or pauses
→ return to normal JARVIS interaction
```

A Focus Session may contain current work, next action, timer, checklist, and related resources. Do not create a new persistent Focus ontology unless product evidence requires it.

## 5. Current UI interpretation

The current SYSTEM / EXECUTION split is a prototype, not binding product architecture. TreePrototype is a prototype, not a permanent product home. CommandCenter is not assumed to remain a separate EXECUTION destination.

CommandCenter currently combines two conceptually different concerns:

### Core JARVIS interaction

- Qwen interaction
- text command input
- Voice / STT
- TTS
- runtime status
- approval and permission
- result and Activity presentation

### Historical focus / execution prototype

- Objective
- Next Action
- Timer
- Checklist
- local execution session state

Do not automatically keep or remove CommandCenter as one indivisible unit. The core assistant interaction is a candidate to become the main JARVIS surface. Focus and execution elements are candidates to become transient Focus Session UI.

## 6. User-facing ontology

Do **not** treat the following as a binding user-facing hierarchy:

```text
Goal → Project → Task → Next Action → Execution
```

It is a historical product hypothesis under reevaluation.

Preferred current interpretation:

- **Workspace:** real working context and filesystem scope.
- **Project:** optional semantic grouping.
- **Task:** canonical actionable entity that may relate to Workspace, Files, or Pages.
- **Goal:** optional higher-level semantic concept under reevaluation.
- **Next Action:** preferably transient or derived until persistence proves useful.
- **Focus:** current execution state, not necessarily a persistent entity.

Do not invent persistent Goal, Next Action, or Focus schemas without explicit user approval. Do not remove existing stable Task or Project infrastructure merely because the UI hierarchy changes.

## 7. Context visibility

JARVIS should make relevant context inspectable. When useful, the UI should expose:

- active Workspace
- active Task
- relevant or selected Files
- relevant Pages
- actions taken
- pending approvals

The user should be able to answer:

- What is JARVIS looking at?
- What is JARVIS about to do?
- What changed?

Important system state must not be hidden behind conversational prose alone.

## 8. PiP

Keep PiP as a core product capability. Its likely V4 roles are:

- JARVIS presence
- summon and voice entry
- listening, thinking, and acting state
- current focus
- approval
- important interruption

Do not assume PiP should continuously rotate ambient dashboard content. Silence and visual minimalism are valid default states when nothing useful needs attention.

## 9. Voice

Voice is a first-class modality of the same JARVIS runtime.

```text
user speaks
→ JARVIS resolves intent and context
→ JARVIS acts or proposes an action
→ concise spoken response
→ detailed state remains visual when appropriate
```

TTS must not simply read all visible UI text. Voice responses should normally be shorter than detailed visual responses. Voice must preserve the same runtime safety and permission behavior as typed input.

## 10. Proactivity

Use conservative proactivity:

- **SILENT:** no interruption without a useful reason.
- **CONTEXTUAL:** a suggestion directly tied to current work.
- **INTERRUPTIVE:** only important deadlines, failures, approvals, or similarly high-value conditions.

Do not create activity or noise merely to make JARVIS appear alive.

## 11. Permission model

Preserve the current Permission Gate principles. Conceptual risk classes are:

- **READ:** list, search, and inspect; normally low risk.
- **SAFE LOCAL METADATA:** Task or ResourceLink changes; may be deterministic when directly requested where appropriate.
- **FILE WRITE:** future capability must show intended changes clearly.
- **DESTRUCTIVE:** delete or overwrite; explicit approval.
- **EXTERNAL ACTION:** send, submit, or authenticated network action; explicit permission appropriate to risk.

Do not broaden permissions during the V4 UX redesign. Do not allow voice or a new surface to silently bypass the runtime permission model.

## 12. Stable infrastructure to preserve

Unless a concrete contradiction is proven, preserve:

- Qwen → Harness → Tools boundary
- replaceable runtime model
- Harness validation, policy, and tracing
- STT and TTS
- Permission Gate principles
- WorkspaceRoot
- stable FileRef identity
- stable Task identity
- stable Page identity
- canonical Task CRUD
- ResourceLink
- Project Primary Workspace
- filesystem read and search
- Markdown as Page content source
- semantic identity separated from path and title

The interaction redesign should sit on top of proven backend infrastructure. Do not rewrite stable backend infrastructure merely to simplify renderer code.

## 13. Filesystem reality & evolution

The filesystem and contextual action capabilities are categorized into three distinct operational states:

### A. IMPLEMENTED AND VERIFIED
- **Human manual Workspace file editing:** Verified read/write editor surface for human-driven file viewing and manual edits (`FileEditor.jsx`) with revision-based conflict detection.
- **Active File context:** Verified single-source transient active file tracking in Electron (`App.jsx`) and bridge metadata passing (`harness_bridge.py`), grounding the model with single-line active file context (`client.py`).
- **Read-only contextual quick actions:** Verified one-click shortcuts (Summarize, Explain, Review) in the Assistant surface (`CommandCenter.jsx`). These follow the standard read-only Assistant submit path:
  ```text
  Quick Action
  → Assistant submit path
  → Active File metadata
  → Qwen
  → existing read_file capability if needed
  → answer
  ```
  These actions are strictly read-only analysis and do NOT mutate the filesystem or require gate approval.
- **Resume Briefing (read-only):** Verified one-utterance work resume (`"계속하자"` → `resume_briefing` read tool). Deterministically assembles project resolution, open/completed tasks, linked resources, and last-session activity from canonical sources, plus a **derived, transient** next-action proposal (no Goal/Next Action persistence). The `[시작]` handoff only sets transient focus/active-file UI state.
- **Explicit Task↔File linking:** Verified user-approved `ResourceLink` creation from both the context tree popover and the Assistant's active-file surface ("이 작업에 연결"), persisted canonically (`resource_links.json`); resume briefings attribute linked files per task and never surface unlinked files. AI auto-linking remains prohibited (§13-C).

### B. ALLOWED FUTURE DIRECTION
- **AI-proposed file edits:** Contextual suggestions for code/file diffs, subject to explicit human review.
- **Additional linking workflows:** Explicit linking between Tasks, Pages, and Workspace Files.
- **Progressive retrieval:** Lexical and structured context recovery across connected roots.

### C. NOT YET IMPLEMENTED / REQUIRES ITS OWN MILESTONE
- **AI filesystem mutation:** Qwen directly modifying or overwriting files on the filesystem.
- **Create / rename / move / delete:** Autonomous file or directory creation, renaming, moving, or deletion.
- **Automatic persistent relationship creation:** Implicit background generation of permanent ResourceLinks without explicit user approval.

Future autonomous file mutations require their own dedicated design, verification, and deliberate permission gating (§11).

## 14. Legacy code and history

Historical prototype code must not silently become product architecture. Verify current imports and usages before reusing or removing known historical candidates, including:

- `useTaskTree.js`
- `treePrototypeData.js`
- old renderer-local tree assumptions
- localStorage execution prototype assumptions

Current code is authoritative. Do not delete legacy code solely from memory or old reports.

## 15. Active & Future Milestones

With baseline runtime stability, active file tracking, and read-only Quick Actions established, the following capabilities represent authorized future directions for iterative development:
- **Broader Resource Linkage:** Page↔File and Page↔Task linking workflows, relation curation (Task↔File explicit linking is now implemented — §13-A).
- **Progressive Search & Retrieval:** Progressive lexical and structured context recovery across connected roots.
- **Gated AI File Edits (Future Milestone):** Gated, human-approved AI file modifications when explicitly requested.

The following remain carefully gated and out of scope:
- Unattended autonomous filesystem writes (must remain confirmed/gated).
- Broad unconstrained ontology expansion without demonstrated user need.
- Slash-command grammars or keyboard action palettes prior to dedicated milestones.

## 16. Current development phase and priority

**Current phase:** JARVIS V4 INTERACTION / UX REDESIGN.

Priority order:

1. inspect actual current interaction
2. define assistant-first flows
3. prototype the smallest useful JARVIS surface
4. integrate existing Qwen, Voice, and context capabilities
5. run the real Electron app
6. use it for realistic tasks
7. record friction
8. classify failures as UI, workflow / Harness, context / retrieval, prompt / schema, or repeated model judgment
9. only later reconsider LoRA

Do not automatically select the next product milestone after completing a task.

## 17. LoRA readiness principle

Do not train behavior before the desired behavior stabilizes.

Before LoRA, stabilize:

- interaction patterns
- tool schemas
- permissions
- context supplied to Qwen
- Harness retry and error behavior
- desired response style
- acceptable autonomous behavior

Actual user corrections and interaction logs may later become valuable training data, subject to deliberate curation and authorization. Do not solve UI or workflow problems with LoRA.

## 18. Development working rules

For JARVIS development:

- inspect relevant current files before editing
- current user request outranks historical roadmap
- current code outranks historical reports
- use one bounded hypothesis per implementation pass
- stop when the requested acceptance criteria are satisfied
- stop when a new product decision is required
- do not automatically continue paused milestones
- do not perform unrelated cleanup
- do not run a generic whole-project audit unless explicitly requested
- do not run a Four-Dimension audit unless explicitly requested
- preserve stable backend contracts
- use scratch or copied state for mutating automated tests
- do not mutate real user data during acceptance tests
- no push, PR, or merge unless explicitly requested
- no destructive Git operations

## 19. Source-of-truth order

When instructions or implementation history conflict, use this order:

1. explicit current user request
2. this current JARVIS V4 product contract
3. actual current code
4. stable architecture contracts
5. current verified tests
6. historical implementation documents
7. prior reports and chat summaries

Never let an old roadmap override current product direction. Never infer current code solely from historical documentation.

## 20. Architecture boundary

JARVIS runtime responsibilities remain:

```text
User
↓
Electron / Voice UI
↓
Qwen
↓
Harness
↓
Tools / State / Files
```

Freebuff is the development agent only. It may inspect, edit, test, and make local checkpoints, but it must never become the JARVIS runtime reasoning agent, tool-selection agent, permission judge, or autonomous product-decision authority.

## 21. Current versus target state

This document defines current product direction, not proof that every capability exists. Always distinguish:

- **Verified current state:** behavior directly established by current code and tests.
- **Target direction:** behavior this contract asks future V4 work to pursue.
- **Open decision:** behavior requiring explicit product choice or further evidence.

The contract does not authorize implementation of paused milestones, filesystem writes, new persistent ontology schemas, or unrelated backend expansion by itself.
