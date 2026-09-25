# JARVIS IMPLEMENTATION CONTEXT

> Long-term implementation contract for ChatGPT, Freebuff, and other development agents working on JARVIS.
>
> Read this document before making architectural or product changes.
>
> Always distinguish:
>
> - **VERIFIED CURRENT STATE**
> - **TARGET DIRECTION**
> - **OPEN DESIGN DECISIONS**
>
> Never describe a target or proposal as if it is already implemented.

---

# 1. PRODUCT PURPOSE

JARVIS is not limited to being a chatbot.

It is intended to become a **Personal AI Layer** that connects two major systems:

## A. Execution

```text
Goal
→ Project
→ Task
→ Next Action
→ Execution
```

## B. Knowledge / Context

```text
Projects
↔ Pages
↔ Files
↔ Conversations
↔ Results
```

JARVIS should connect both sides:

```text
                 JARVIS
                /      \
               /        \
        KNOWLEDGE      EXECUTION
            ↓              ↓
     What do I know?   What should I do?
               \        /
                \      /
               NEXT ACTION
```

The objective is not to accumulate features.

The objective is to reduce the user's cost of:

- recovering project context
- remembering unfinished work
- finding relevant information
- deciding what to do next
- moving between tools
- executing routine actions safely
- maintaining personal knowledge over time

Core product values:

- continuity
- context recovery
- low management overhead
- safe execution
- persistent structure
- local-first operation
- observable behavior

---

# 2. RUNTIME RESPONSIBILITY BOUNDARY

The intended runtime boundary is:

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

## Qwen

Qwen is the current runtime reasoning agent.

Responsibilities:

- understand user intent
- decide whether tools are needed
- select tools
- generate tool arguments
- interpret tool results
- ask clarifying questions
- produce the final user-facing response

Qwen must not directly mutate application state or the filesystem.

The current runtime model may change in the future.
Do not hard-couple architecture to one specific model.

## Harness

The Python Harness owns:

- orchestration
- validation
- permission enforcement
- tool execution
- state access
- tracing / observability
- runtime safety boundaries

Execution belongs to deterministic application code, not to the LLM itself.

## Electron

Electron / React is the product UI.

Preferred boundary:

```text
Renderer
→ preload/main IPC
→ managed Harness process
→ Qwen / Tools / State
```

Do not duplicate Harness business logic in JavaScript merely for convenience.

## Freebuff

Freebuff is a **development agent only**.

Freebuff may:

- inspect code
- implement features
- run tests
- fix failures
- create local checkpoint commits

Freebuff must not become:

- the JARVIS runtime reasoning agent
- the runtime tool-selection agent
- the runtime permission judge
- an autonomous product-decision authority inside JARVIS

Runtime decisions belong to Qwen + Harness.

---

# 3. DATA OWNERSHIP PRINCIPLE

Do **not** interpret "single source of truth" as "one giant database/store for everything."

The correct principle is:

> Each concept should have one canonical source, and all other representations should be derived from it.

Examples:

```text
Project / Task
→ canonical project/task state

Markdown Page
→ canonical Markdown content + hierarchy metadata

File
→ actual filesystem

Semantic search
→ derived/rebuildable index

Current Action
→ execution state or derived selection
```

A unified UI may combine these domains:

```text
                    SYSTEM MAP
                        ↑
                 unified read model
             /          |          \
        Projects       Pages       Files
           ↑             ↑           ↑
      Task store      Markdown    Filesystem
```

The system must avoid **competing writable copies** of the same fact.

Allowed:

- adapters
- caches
- projections
- indexes
- renderer view models

Not allowed:

- independent task state for Qwen and the SYSTEM MAP
- separate authoritative copies of the same page hierarchy
- UI-only persistence pretending to be canonical state

---

# 4. VERIFIED CURRENT STATE

This section is a snapshot, not a permanent architectural rule.

## Current verified structured model

The currently verified persisted structured model is:

```text
Project
└─ Task
```

Real persisted Goal and Next Action entities are **not yet verified**.

## Current tree convergence

The previous disconnected tree flow was:

```text
hardcoded TreePrototype data
→ localStorage tree state
→ SYSTEM MAP
```

The newer read path is:

```text
projects.md + tasks.md
↓
MemoryContextReader / TaskStore
↓
Harness tree_snapshot
↓
BridgeManager
↓
Electron IPC
↓
useJarvisTree
↓
SYSTEM MAP
```

## Current development data

The runtime resolves its memory directory in this order:

1. `JARVIS_STATE_DIR` / `JARVIS_BRIDGE_MEMORY_DIR` — explicit override
2. `JARVIS_USE_SCRATCH=1` — forces isolated scratch (tests/smoke only)
3. default: the canonical **user** state directory
   - Windows: `%APPDATA%/jarvis-app/memory`
   - Linux/macOS: `~/.config/jarvis-app/memory` (or `XDG_CONFIG_HOME`)

`data/electron_scratch/` is **SCRATCH / DEVELOPMENT DATA** and is no longer
the runtime default. It exists for isolated testing and as a first-run
migration source only.

On first run against the canonical directory, existing scratch state is copied
in **copy-if-absent** fashion (`migrate_scratch_to_user_state`): `projects.md`,
`tasks.md`, `workspace_roots.json`, `project_workspaces.json`,
`resource_links.json`, `file_refs.json`, `page_identity.json`, and the `pages/`
tree. Files already present in the user directory are never overwritten.

## Relevant checkpoints

Electron:

- `c6f6eeb` — voice UX / interaction checkpoint
- `d12d9ec` — preserve full final response after PiP completion auto-hide
- `e511716` — voice transcript appends instead of destroying typed text
- `4ab5687` — SYSTEM MAP reads Harness tree snapshot

Harness:

- `df8145b` — read-only `tree_snapshot`

Always inspect the actual repository instead of relying only on these commit summaries.

---

# 5. TARGET EXECUTION MODEL

The long-term execution concept remains:

```text
Goal
→ Project
→ Task
→ Next Action
→ Execution
```

However:

- Goal does not yet have a finalized persistence representation.
- Next Action does not yet have a finalized persistence representation.
- Do not invent Goal or Next Action nodes merely to make the UI look complete.

In particular, **Next Action is not assumed to be a permanent child entity of Task**.

It may eventually be:

- a persisted entity
- task metadata
- a selected action
- execution state
- a derived recommendation

This remains an open modeling decision.

---

# 6. KNOWLEDGE / MARKDOWN DIRECTION

JARVIS should support a Notion-like personal knowledge structure using Markdown.

Desired capability:

```text
Knowledge
├─ AI
│  ├─ LoRA
│  └─ Agents
├─ Music
│  └─ Post-tonal
└─ C++
   └─ OOP
```

And project-linked views such as:

```text
Graduation Project
├─ Tasks
└─ Notes
   ├─ Research
   ├─ Experiments
   │  └─ LPIPS Results
   └─ Meeting Notes
```

## Important: Notes do not have to be owned by one Project

Knowledge may exist independently and be linked from multiple places.

Target relationship model should allow concepts such as:

```text
Project ──references──> Page
Task    ──references──> Page
Page    ──links───────> Page
```

Therefore:

> "Project owns every Note" is NOT a permanent rule.

## Markdown persistence

Markdown should remain human-readable, portable, and local-first.

The exact hierarchy mechanism is **not yet fixed**.

Potential approaches include:

### A. Filesystem hierarchy

```text
notes/
└─ graduation/
   └─ experiments/
      └─ lpips.md
```

### B. Frontmatter hierarchy

```markdown
---
id: note-lpips
parent: experiments
---
```

### C. Folder + metadata hybrid

A future implementation should compare these approaches using actual repository constraints.

Do not prematurely force frontmatter, a manifest, or folder-only hierarchy.

Required long-term capabilities:

- stable identity
- recursive nesting
- human-readable Markdown
- safe move/rename behavior
- links between pages
- project/task references
- local-first storage

---

# 7. FILE ACCESS DIRECTION

JARVIS should eventually understand and work with user-approved files.

Qwen must not receive unrestricted filesystem access.

Preferred boundary:

```text
Qwen
↓
Harness Tool
↓
path / permission validation
↓
Filesystem
↓
Tool result
↓
Qwen
```

Initial read-only tools may conceptually include:

- `list_files`
- `search_files`
- `read_file`

Mutating tools may later include:

- `create_file`
- `write_file`
- `move_file`
- `delete_file`

Access should be scoped to user-approved roots.

Do not silently expose:

- `.ssh`
- credentials
- `.env`
- browser profiles
- system directories
- unrelated private directories

Derived indexes are allowed when useful.

For example, semantic search may use a local embedding/vector index as long as:

- it is not treated as canonical content
- it can be rebuilt from source files
- it does not silently ingest unrelated private data

---

# 8. PERMISSION POLICY

Do not encode this rule:

```text
every state-changing operation
→ always require confirmation forever
```

That would create unnecessary management friction.

Instead, permission should eventually depend on:

- explicit user intent
- risk
- scope
- reversibility
- external side effects
- autonomy level

Conceptually:

```text
DIRECT USER INTENT
+ low-risk
+ scoped
→ may execute directly

AI-INITIATED / AMBIGUOUS
→ confirmation

DESTRUCTIVE / EXTERNAL / HIGH-RISK
→ confirmation

BACKGROUND / AUTONOMOUS ACTION
→ policy-dependent confirmation
```

## Current safety baseline

The existing `create_task` flow uses:

```text
Qwen proposes tool call
↓
Harness validates
↓
AWAITING_CONFIRMATION
↓
Approve / Reject
↓
exact approved call executes once
```

This remains a good **current safety baseline**.

Do not confuse it with the final permission policy.

## Important distinction

A human clicking a direct UI action is not automatically the same as an AI proposing an action.

Avoid redundant confirmation when the user has already made an explicit low-risk direct action, unless the operation itself is risky.

Voice must not silently bypass the permission model.

---

# 9. TREE WRITE MODEL

The SYSTEM MAP must represent real JARVIS structure, not decorative prototype data.

Current progress is read convergence.

Future writes should follow the canonical domain owner.

For project/task state, the desired concept is:

```text
Tree UI
↓
canonical Harness/tool write path
↓
state change
↓
fresh snapshot
↓
Tree refresh
```

Avoid:

```text
Tree UI
→ local-only edit
→ separate persistence
```

Do not treat current in-memory Tree Add/Edit/Delete behavior as canonical persistence unless it is actually connected to the real write path.

---

# 10. VOICE ARCHITECTURE

Voice is a modality, not a separate brain.

Input convergence:

```text
Typed text ─┐
            ├→ same JARVIS runtime
Voice → STT ┘
```

After transcription, typed and voice input should converge into the same conceptual command path.

Current voice principles:

- hold-to-talk is acceptable for the current stage
- no wake word required yet
- no global hotkey required yet
- STT worker should stay long-lived
- Korean support matters
- transcript should remain editable before Send
- voice must not bypass runtime safety

Current input behavior:

```text
empty input + transcript
→ fill

existing input + transcript
→ append
```

TTS should speak final user-facing responses, not:

- raw tool JSON
- traces
- internal reasoning
- permission payloads

Half-duplex / self-echo prevention is a current practical strategy, not a permanent product rule.

---

# 11. UI PRODUCT PRINCIPLES

## PiP

PiP should be:

- calm
- lightweight
- ambient
- focused on the next relevant interaction

It is not a developer console.

PiP should prioritize:

- required permission
- current state
- next immediate action
- minimal ambient information

Avoid leaving long duplicated Qwen responses in PiP.

## Command Center

The full view is the richer workspace.

It may contain:

- full answers
- Activity
- SYSTEM MAP
- project context
- tasks
- pages
- files
- execution controls

## SYSTEM MAP

SYSTEM MAP is a real view of JARVIS state.

Data correctness comes before visual polish.

Do not improve tree visuals while the underlying state is fake, stale, duplicated, or disconnected.

---

# 12. OBSERVABILITY

Preserve useful runtime observability.

Activity / traces should allow inspection of:

- input source: TEXT / VOICE
- model request
- tool selection
- permission request
- approval / rejection
- tool result
- final response
- failures

This does **not** mean exposing private chain-of-thought.

Focus on operational traces, state transitions, tool calls, tool results, and user-visible outcomes.

A known failure class is:

```text
tool result is correct
but
Qwen final answer contradicts the tool result
```

Preserve such examples.

Potential future uses:

- deterministic consistency evaluator
- bounded rewrite/retry
- evaluation dataset
- later LoRA data

Do not compensate for a model-quality problem by corrupting canonical state logic.

---

# 13. MODEL / LoRA DIRECTION

Current runtime model:

```text
local-jarvis-qwen3:8b
```

This is a current implementation detail, not a permanent contract.

Long-term model workflow:

```text
open model
→ JARVIS runtime
→ collect real usage/failures
→ curate data
→ evaluate
→ LoRA / Soup if justified
→ held-out comparison
→ deploy only if better
```

Prefer deterministic Harness-level solutions first when the failure is deterministic or structural.

Fine-tuning should not be the default fix for one bad answer.

Private user data may be used for training **only when deliberately curated and explicitly authorized for that purpose**.

Do not automatically train on:

- arbitrary private files
- credentials
- secrets
- unrelated conversations
- raw unreviewed user data

---

# 14. IMPLEMENTATION WORKFLOW

Development agents should generally:

1. read this document
2. inspect the actual repository
3. establish verified current behavior
4. identify the relevant architectural boundary
5. distinguish fact from assumption
6. implement a cohesive, bounded milestone
7. verify deterministically
8. perform real runtime/render verification when practical
9. inspect the diff
10. run build/lint/tests
11. create a local checkpoint when appropriate
12. report remaining gaps

Do not force every task into a tiny one-line change.

Large structural changes are allowed when justified.

However, avoid combining unrelated work into one run.

Good:

```text
Markdown read model
→ hierarchy snapshot
→ SYSTEM MAP
→ verification
```

Bad:

```text
Markdown hierarchy
+ voice redesign
+ database
+ semantic search
+ LoRA
+ new UI
```

---

# 15. SCRATCH DATA VS USER DATA

Always distinguish:

```text
SCRATCH / DEV DATA
```

from:

```text
REAL USER DATA
```

Automated mutation tests must use:

- scratch
- fixtures
- temporary files
- copies

Do not mutate real user state just to prove a feature works.

Recommended progression:

```text
scratch implementation
→ scratch verification
→ real data READ ONLY
→ compare/read verification
→ gated real writes later
```

Never silently fall back between scratch and real data.

The active mode should be explicit.

---

# 16. DEPENDENCY POLICY

Do not ban databases, vector stores, frameworks, or new dependencies categorically.

Instead:

> Avoid premature complexity, but adopt a dependency when it clearly solves a demonstrated requirement better than maintaining custom infrastructure.

Examples:

- SQLite may become appropriate for relationships, indexing, or transactional state.
- A vector index may become appropriate for semantic search.
- A parser library may be appropriate for Markdown/frontmatter.

Any new dependency should justify:

- why it is needed
- what complexity it removes
- local-first compatibility
- migration/replacement cost
- long-term ownership burden

---

# 17. GIT / CHANGE POLICY

Prefer small, meaningful local checkpoints.

Before commit:

- inspect `git status`
- inspect the full diff
- remove debug/generated artifacts
- run relevant build/lint/tests

Freebuff may create local commits.

Unless explicitly requested, do not:

- push
- open PR
- merge
- force-reset
- rewrite unrelated history
- modify global Git configuration

Do not rebuild a subsystem from scratch without evidence that incremental evolution is unreasonable.

---

# 18. REPORTING STANDARD

After implementation, report:

1. previous behavior
2. root cause / relevant boundary
3. exact change
4. files changed
5. verification performed
6. build/lint/tests
7. what remains incomplete
8. commit hash
9. blockers / uncertainty

Never present planned behavior as completed behavior.

If verification used stubs instead of the native Electron path, say so.

If a result is partially verified, label it that way.

---

# 19. CURRENT OPEN GAPS

As of the current checkpoint, these are not solved:

- Goal persistence model is not finalized
- Next Action representation is not finalized (still derived + transient)
- hierarchical Markdown Pages are not implemented
- arbitrary file access is not implemented
- Page → Page recursive hierarchy is not implemented
- unified Knowledge + Execution read model is not yet implemented
- automatic Tree refresh after **model-initiated** Harness writes is not
  guaranteed — Tree-originated Add/Edit/Delete re-snapshot from canonical state
  after success, but there is no bridge-event subscription, so a task created by
  Qwen through the tool loop does not refresh the Tree until a manual refresh
- end-to-end resume flow has not been verified through the real Electron GUI
  (verified through the bridge/Python path only)

Settled since this list was first written — do not report these as open:

- production REAL USER-state location **is** established and resolved at runtime
  (§4 Current development data)
- Project/Task integration is **not** based on `electron_scratch`; the canonical
  user directory is the default and scratch is opt-in via `JARVIS_USE_SCRATCH`
- Tree Add/Edit/Delete writes canonical state through `TaskStore` as single writer
  (`create_task` / `update_task` / `delete_task` bridge messages)

Treat this section as a dated implementation snapshot.

Update it when the architecture materially changes.

---

# 20. LONG-TERM ARCHITECTURAL TEST

When deciding whether a change belongs in JARVIS, ask:

### Execution

Does it improve:

```text
Goal
→ Project
→ Task
→ Next Action
→ Execution
```

### Knowledge

Does it improve the user's ability to connect:

```text
Pages
↔ Files
↔ Projects
↔ Tasks
↔ Conversations
↔ Results
```

### System quality

Does it preserve or improve:

- canonical ownership per domain
- context continuity
- safe execution
- low user-management overhead
- local-first operation
- observability
- replaceable components
- user control over private data

If a change does not advance these goals, reconsider it.

---

# 21. AGENT STARTUP INSTRUCTION

When ChatGPT, Freebuff, or another development agent begins a JARVIS task, use this document as the implementation contract.

Recommended startup instruction:

```text
Read `JARVIS_IMPLEMENTATION_CONTEXT.md` first.

Treat it as the long-term implementation contract, but verify all CURRENT STATE claims against the actual repository before relying on them.

Today's milestone:
<INSERT GOAL>

Stay within one cohesive milestone.
Preserve the runtime, data ownership, privacy, and permission boundaries defined in the context document.

Distinguish:
- verified current state
- target architecture
- open design decisions

Verify the result, review the diff, run relevant checks, create a local checkpoint if appropriate, report remaining gaps, then stop.
```
