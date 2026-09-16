# JARVIS Workspace / Filesystem Architecture Contract

Status: Architecture decision record for the next JARVIS filesystem, workspace, search, and semantic-linking milestones.

This file complements `JARVIS_IMPLEMENTATION_CONTEXT.md`.

If this file conflicts with a temporary implementation detail, prefer this file for the workspace/filesystem domain unless the user explicitly overrides it.

---

## 1. Product Intent

JARVIS should not become a simple file explorer.

The target is a local-first personal context system where the user can work in terms of meaning:

- Projects
- Tasks
- Pages
- Files
- Conversations
- Results

without needing to memorize internal IDs or physical file locations.

JARVIS should recover context from the user's natural language, resolve canonical entities internally, and show the same underlying resource through multiple useful views.

Example:

```text
User:
"LPIPS 결과 어디 있었지?"

JARVIS:
search Projects / Tasks / Pages / Files / Conversations
→ resolve relevant canonical entities
→ follow explicit links
→ read canonical sources
→ answer with provenance
```

Internal identifiers and paths are implementation details, not user requirements.

---

## 2. SYSTEM MAP

SYSTEM MAP should support both semantic and physical views.

Preferred structure:

```text
SYSTEM MAP
├─ PROJECTS        # semantic / execution view
├─ KNOWLEDGE       # semantic knowledge view
└─ FILES           # physical workspace/filesystem view
```

The semantic view is the primary product view.

The FILES view exists because physical location still matters.

The same canonical File may appear in multiple views without being duplicated as an entity.

Example:

```text
FILES
└─ Research
   └─ papers
      └─ qian2018.pdf

PROJECTS
└─ Graduation Thesis
   └─ References
      └─ qian2018.pdf
```

These are two projections of one file reference.

Do not make the SYSTEM MAP tree itself a canonical datastore.

---

## 3. Canonical Ownership

Canonical source is domain-specific.

```text
Projects / Tasks
→ JARVIS domain store

Pages
→ Markdown + Page metadata

Files
→ user's actual filesystem

Derived metadata / search / links / cache
→ rebuildable JARVIS state
```

A cache, index, search database, or vector store must never silently become the only source of truth for the original resource.

---

## 4. Project and Folder Relationship

Decision:

```text
Project ≠ Folder
```

However, a Project should usually be allowed to have a primary workspace folder.

Conceptual model:

```text
Project
├─ id
├─ title
├─ primary_workspace_root?   # optional
└─ linked resources
```

Example:

```text
Graduation Thesis
├─ Primary Workspace
│  → C:\Research\graduation-thesis
│
└─ Additional Resources
   ├─ D:\Papers\qian2018.pdf
   └─ C:\Datasets\Qian
```

This preserves coding-agent ergonomics where a project often maps naturally to one working directory, while keeping JARVIS flexible enough to model projects that span multiple physical locations.

Do not force a one-project-one-folder invariant.

---

## 5. Page and File Relationship

Decision:

```text
Page ≠ arbitrary File
```

A Markdown file does not automatically become a JARVIS Page merely because its extension is `.md`.

A Page is a semantic knowledge object.

A Page may optionally be backed by a Markdown file.

Preferred behavior:

```text
File discovered
→ remains a File

User explicitly uses it as knowledge
or
JARVIS proposes promotion/linking
→ Page may reference/back onto that File
```

The existing Page hierarchy may continue to use semantic metadata such as frontmatter `id` / `parent`.

Moving a Page's backing file should not necessarily change its semantic parent.

---

## 6. File Ownership

Default policy:

JARVIS does not import or copy ordinary user files into an internal store.

The original file remains where the user placed it.

```text
User filesystem
        ↑
     JARVIS
   link/reference
```

JARVIS may later support managed/imported resources, but external-link behavior is the default.

---

## 7. Workspace Roots

A physical path should be accessed through an approved logical root.

Preferred locator:

```text
root_id + relative_path
```

not:

```text
absolute_path as identity
```

Conceptual model:

```text
WorkspaceRoot
├─ id
├─ display_name
├─ device_path
├─ access_mode
├─ ignore_rules
└─ indexing_policy
```

Example:

```text
root_id: thesis

Windows:
D:\Research\graduation-thesis

Mac:
~/Research/graduation-thesis
```

Canonical logical references can remain:

```text
thesis:experiments/lpips-results.csv
```

while physical paths differ per device.

Multi-device support is a long-term requirement.

---

## 8. Folder Approval UX

Preferred UX:

1. User asks naturally:
   - "내 졸업논문 폴더 연결해."
2. JARVIS opens an OS folder picker.
3. User selects a folder.
4. JARVIS registers a WorkspaceRoot.
5. Settings provides an explicit fallback for viewing, adding, removing, or changing approved roots.

Current environment-variable configuration is transitional developer plumbing, not the final user experience.

---

## 9. Coverage Preference

User preference:

Missing relevant files is worse than JARVIS seeing a broad but approved information set.

Therefore prefer:

```text
high coverage
+
strong secret/noise filtering
```

rather than extremely narrow project-only roots.

Do not automatically grant the entire drive or user profile.

Broad meaningful roots such as Documents, Research, Development, or Music may be user-approved.

---

## 10. Sensitive and Noisy Paths

Default deny / ignore should include obvious secret and generated areas where applicable:

- `.env`
- `.ssh`
- credentials / token / key files
- browser profiles
- secret stores
- `.git` internals
- `node_modules`
- build / dist output
- caches
- OS/system application state

Generated/noisy directories may be overridable.

Secrets should not become search/index content merely because they reside under an approved root.

---

## 11. File Identity

Decision:

A file should, where practical, remain the same JARVIS entity after rename or move.

Therefore:

```text
File identity ≠ path
```

V1 should be minimal and reversible.

Recommended conceptual model:

```text
FileRef
├─ id                  # immutable JARVIS identity
├─ root_id
├─ relative_path       # current locator
├─ size
├─ mtime
├─ content_fingerprint? # optional
└─ last_seen
```

Do not require OS-native identity in the first version.

Initial reconciliation may use:

- stable JARVIS ID
- prior path
- size
- mtime
- optional bounded content fingerprint

Later, if needed:

- Windows File ID
- macOS security-scoped bookmarks / native resource identity

may strengthen rename/move detection.

Ambiguous rename/move matches should not be silently accepted.

---

## 12. Project / Page / File Relationships

Do not infer permanent relationships solely from search similarity.

Distinguish:

```text
inferred relationship
≠
persisted relationship
```

Preferred UX:

```text
JARVIS:
"This file appears related to Graduation Thesis.
Link it as a result?"

User approves
→ persisted link
```

Search can surface probable relevance without creating persistent structure.

---

## 13. Resource Link Model

Internally prefer a generic relationship layer rather than embedding every relationship directly into each entity.

Conceptual model:

```text
ResourceLink
├─ from_type
├─ from_id
├─ to_type
├─ to_id
└─ relation
```

This is an internal model.

Do not expose graph-management complexity to the user unless useful.

The relationship should carry meaning where helpful.

Examples:

```text
Project --reference--> File
Task --produced--> File
Page --backed_by--> File
Task --source--> Page
Conversation --about--> Project
```

The user wants JARVIS to remember what a relationship means, not merely that two objects were connected.

---

## 14. Search

Long-term search scope:

- Projects
- Tasks
- Pages
- Files
- Conversations
- Results

Search should prioritize context recovery, not just filename lookup.

The user should be able to ask:

- "LPIPS 결과 어디 있었지?"
- "예전에 diffusion 결과 이상했던 실험 찾아줘."
- "보컬 앱 아이디어 작업하던 프로젝트 열어줘."

The model should resolve internal IDs itself.

Initial implementation order:

1. deterministic lexical search
2. full-text local index
3. real usage evaluation
4. semantic retrieval when justified

Do not prematurely make a vector database the source of truth.

---

## 15. File Content Support

Recommended staged support:

### V1
Textual files:

- `.md`
- `.txt`
- source code
- JSON / config text
- other bounded safe text formats

### V2
Structured documents:

- PDF
- DOCX
- PPTX
- XLSX

### V3
Media-derived context:

- audio transcripts
- image understanding / metadata

Do not implement all formats in one milestone.

---

## 16. Index Architecture

Long-term recommendation:

```text
Initial scan
+
filesystem watcher
+
incremental update
+
periodic / manual reconciliation
```

A watcher alone is not sufficient.

The filesystem remains canonical.

The index is derived and rebuildable.

SQLite is acceptable for derived local state if and when scale justifies it.

Possible derived tables later:

```text
workspace_roots
file_refs
resource_links
file_metadata
full_text_index
```

If SQLite or another DB is introduced:

```text
Filesystem / Markdown / TaskStore
= canonical

SQLite
= derived metadata / links / search / cache
```

unless a future explicit architecture decision says otherwise.

---

## 17. OS Search

Platform facilities such as Windows Search or macOS Spotlight may be used as accelerators.

They should not be the only implementation of JARVIS search.

Preferred approach:

```text
JARVIS local index / canonical readers
+
OS search when useful
```

so behavior remains portable.

---

## 18. Semantic Search

Semantic memory-style retrieval is a long-term goal.

The user ultimately wants meaning-based recall, not only keyword matching.

Example:

```text
"전에 결과가 이상하게 나온 diffusion 실험"
```

should eventually be retrievable even if those exact words are absent.

Do not implement semantic search before lexical/full-text behavior has been exercised on real user data.

Suggested first semantic targets later:

- Pages
- Tasks
- selected textual Files

Then potentially:

- Conversations
- Results

---

## 19. Folder Access and Indexing Permission

User-facing workflow should stay simple.

A single folder-connection approval may authorize both:

- read access
- search/index access

when the UI clearly states that JARVIS will be able to search supported document contents.

Internally these may still be represented as separate scopes.

Avoid repeated confirmation that adds no meaningful safety.

---

## 20. Filesystem Writes — Future

Current filesystem integration remains READ-first.

Long-term, JARVIS should be allowed to perform useful filesystem actions such as:

- edit a note
- rename a file
- move a file
- create a file

Permission model should be based on intent and risk.

Preferred principle:

```text
explicit user + reversible action
→ may execute directly

AI-proposed structural action
→ confirm

destructive / overwrite / irreversible action
→ confirm
```

This is a future milestone, not authorization to implement writes now.

---

## 21. Task Identity

Open implementation issue, but intended semantic decision:

Changing Task title/reason should not create a new Task.

Therefore:

```text
Task identity ≠ title/reason
```

Existing content-derived Task IDs should not be treated as the permanent long-term identity model.

Recommended migration direction:

- preserve existing `t-*` IDs as valid legacy stable IDs
- new identity scheme should be immutable
- mutable title/reason should not determine entity identity
- idempotent create behavior may use a separate fingerprint if needed

Do not implement Edit Task before this is resolved.

---

## 22. Page Hierarchy

Preferred semantic behavior:

```text
Page parent
→ Page metadata/frontmatter

Physical file parent
→ filesystem path
```

Moving a backing Markdown file in Explorer should not automatically redefine the Page's semantic hierarchy.

FILES view reflects physical location.

KNOWLEDGE view reflects semantic organization.

---

## 23. Conversations

Conversations should eventually be searchable and linkable to Projects.

Do not force every conversation to appear as an expanded SYSTEM MAP node.

Preferred UX:

```text
Project
├─ Tasks
├─ Knowledge
├─ Resources
└─ Conversations (count / collapsed access)
```

with global search coverage.

---

## 24. Results

Do not introduce a separate Result entity prematurely.

Initially represent outputs with meaningful resource links.

Examples:

```text
Task --produced--> File
Task --produced--> Page
```

If real workflows later require structured experiment runs, metrics, checkpoints, or result history, a dedicated `Result` / `ExperimentRun` entity may be introduced then.

---

## 25. Expected JARVIS Behavior

Example 1:

```text
User:
"내 졸업논문 프로젝트 열어줘."

JARVIS:
search_projects("졸업논문")
→ resolve Project
→ load Tasks / Pages / linked resources
→ open semantic Project view
```

Example 2:

```text
User:
"LPIPS 결과 어디 있었지?"

JARVIS:
search Projects / Tasks / Pages / Files / Conversations
→ return relevant entities
→ follow explicit links
→ read canonical resources
→ answer with provenance
```

Example 3:

A new file appears:

```text
Research/results/new-lpips.csv
```

JARVIS:

```text
watcher/index notices File
→ FileRef created / reconciled
→ searchable
```

If it appears related to a Project:

```text
JARVIS:
"`new-lpips.csv` appears related to Graduation Thesis.
Link it as a result?"

User approves
→ ResourceLink persisted
```

Example 4:

User moves:

```text
results/new-lpips.csv
→ experiments/lpips-final.csv
```

JARVIS should attempt to preserve the existing FileRef and its Project links.

If reconciliation is ambiguous, ask instead of silently linking the wrong file.

---

## 26. Near-Term Implementation Order

Recommended sequence:

1. Formalize `WorkspaceRoot` as a first-class read-only concept.
2. Introduce minimal stable `FileRef` identity + root-relative locator.
3. Reconcile rename/move conservatively using minimal metadata/fingerprint.
4. Keep current read/list/search behavior working through the new abstraction.
5. Add explicit persisted `ResourceLink` only after identity is stable enough.
6. Add Project primary workspace folder support.
7. Add OS folder-picker UX + Settings fallback.
8. Add watcher + rebuildable metadata index when scale requires it.
9. Resolve Task immutable identity.
10. Then implement canonical Task Edit/Delete.
11. Re-evaluate full-text / semantic indexing from real usage.

Do not combine all of these into one large implementation change.

---

## 27. Non-Goals for the Next Milestone

Unless explicitly requested, do not:

- implement filesystem writes
- introduce a vector database
- semantic-index all user content
- scan the whole device
- force every Markdown file into PageStore
- force every Project to equal one folder
- auto-persist Project/File/Page relationships from similarity
- implement OS-native File IDs
- migrate Task IDs
- implement Task Edit/Delete
- redesign SYSTEM MAP
- refactor unrelated large UI files
- modify TTS
- invent Project↔Page relationships

---

## 28. Architecture Test

A proposed implementation is healthy if all of the following remain true:

1. The original user file remains usable outside JARVIS.
2. JARVIS can rebuild derived indexes from canonical sources.
3. Rename/move does not unnecessarily destroy semantic relationships.
4. A Project can use one primary workspace but is not imprisoned by one directory.
5. The same resource can appear in several SYSTEM MAP views without becoming duplicate canonical objects.
6. Search results distinguish identity, match evidence, and persisted relationships.
7. The user does not need to memorize IDs or paths.
8. JARVIS does not silently promote probabilistic relevance into permanent structure.
9. Multi-device root mapping remains possible.
10. Security boundaries remain narrow even when file coverage is broad.

---

## 29. Instruction to Development Agents

Before modifying workspace/filesystem/search/link architecture:

1. Read `JARVIS_IMPLEMENTATION_CONTEXT.md`.
2. Read this file.
3. Distinguish:
   - existing verified implementation
   - architecture decisions in this contract
   - open questions
4. Do not silently choose a different identity, ownership, or linking model.
5. If a required decision is not covered here, stop at the smallest safe boundary and report the decision needed.
