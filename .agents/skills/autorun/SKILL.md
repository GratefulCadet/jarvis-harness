---
name: autorun
scope: project
---

Before JARVIS product or UX work, read these repository-root canonical contracts:

- `JARVIS_V4_PRODUCT_DIRECTION.md`
- `JARVIS_IMPLEMENTATION_CONTEXT.md`
- `JARVIS_WORKSPACE_ARCHITECTURE.md`

Working rules:

- The current user request outranks historical roadmaps; actual current code outranks historical reports.
- JARVIS is currently in the V4 interaction / UX redesign phase.
- Do not automatically continue paused milestones.
- Work on one bounded hypothesis per implementation pass.
- Do not choose the next product milestone automatically.
- Stop when a new product or user decision is required.
- Preserve stable backend contracts and do not perform unrelated cleanup.
- Do not run a generic whole-project audit or Four-Dimension audit unless explicitly requested.
- Use scratch or copied state for mutating automated tests; never mutate real user data during automated tests.
- No push, PR, or merge without an explicit request; no destructive Git operations.
