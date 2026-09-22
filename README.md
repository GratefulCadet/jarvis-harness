# JARVIS Harness

Python Harness for the local-first JARVIS assistant. It keeps runtime orchestration, validation, permissions, tracing, and deterministic tool execution outside the Electron UI.

## Architecture

```text
Local Qwen/Ollama
        ↓
Python Harness
        ↓
validation · policy · tracing · Permission Gate
        ↓
canonical tools and state
```

The Electron application is the user-facing interaction layer; this repository provides the runtime boundary it calls through the bridge. The Harness is designed to keep model reasoning replaceable while tool execution remains explicit and testable.

## Implemented foundations

- canonical Project and Task state with stable Task identity and CRUD
- Markdown-backed Pages with stable Page identity and recursive hierarchy
- WorkspaceRoot and FileRef identity with root-relative locators
- read-only workspace file listing, search, and context discovery
- Project primary workspaces and semantic Project/Task resource links
- deterministic validation, Permission Gate behavior, tracing, and scratch-state tests

Markdown remains the Page content source, and filesystem capabilities are primarily read-oriented. Filesystem writes, semantic/vector search, database migration, and autonomous behavior are not implied by this README.

## Development

```bash
python -m unittest discover -s tests -p 'test*.py'
```

See `JARVIS_V4_PRODUCT_DIRECTION.md` for the current interaction/product direction and the separate architecture contracts for stable backend decisions.
