# Design: News → Issue Note

> **HISTORICAL / PROPOSAL REFERENCE ONLY — NOT AUTHORITATIVE.**
> This document is the original Webz.io-based proposal. It was written before any News
> feature existed and it is kept here as a record of the design intent that still holds
> (bounded results, source links, visible evaluator, draft before save, no automatic Task
> creation). It does **not** describe shipped behavior.
>
> The authoritative document is
> `jarvis-app-v3-harness/docs/NEWS_TO_ISSUE_NOTE_DESIGN.md`, which describes the Freebuff
> News Scout that actually ships in the Electron app (`electron/news-service.cjs`,
> `src/FreebuffNews.jsx`). That implementation uses free public sources — GDELT, Hacker
> News, GitHub, arXiv, RSS — not Webz.io, and the `sentiment` / `trust.categories` /
> `ai_allow` provider-annotation concerns below do not apply to it.
>
> Read the app repo's document for current state. Read this one only for history.

**Status:** Superseded proposal. No Webz.io integration or product behavior is implemented
by this document.

**Purpose:** Let a user ask JARVIS about current external news, choose relevant coverage, then turn that coverage into a user-editable, source-linked issue document. JARVIS remains the assistant; users do not have to operate a news dashboard or learn query syntax.

## Product shape

```text
User asks about a current issue
→ JARVIS searches news only when asked
→ presents source-attributed results and caveats
→ user selects relevant articles / asks to document them
→ JARVIS drafts an Issue Note with citations
→ user reviews and edits the draft
→ user approves saving to Knowledge (Markdown Page)
→ optional, separate Task/link only when the user asks for follow-up work
```

The Issue Note is a normal semantic **Page**, not a new canonical Issue database or a copy of a news provider's index. Markdown in the configured Pages directory remains the canonical note content. Existing Page identity metadata remains derived/rebuildable. News articles remain external sources; their URLs and source metadata are cited in the note. Do not create a persistent ResourceLink to a URL until that relation type is explicitly designed.

## User flow

1. **Search:** User asks, for example, “최근 Tesla 관련 부정적 보도를 찾아줘.” JARVIS may translate intent into a provider query, but must not include private workspace text, open-file contents, or unrelated personal context in that query. Show the provider/search scope and an “as of” time.
2. **Inspect:** Return a small bounded set (initial target up to 10) of deduplicated results. Each result shows headline, publisher/domain, publication date, short excerpt, source URL, country/language when useful, and clearly attributed provider annotations (e.g. “Webz sentiment: negative”, “Trust flag: fake_news”). Sentiment/category are metadata, not verified facts. Users can open the original source and select which articles matter.
3. **Draft:** “이 이슈로 정리해줘” drafts a note from selected results (or asks which results if selection is ambiguous). The draft distinguishes sourced facts, provider classifications, JARVIS synthesis/inference, conflicting coverage, and unanswered questions. Every material factual claim has an inline source reference such as `[1]`.
4. **Review and save:** Show the complete editable Markdown preview, destination, and source list. Save only after an explicit user action. If Pages storage is unavailable, keep the draft in the conversation and ask for a destination; do not silently create it in an arbitrary Workspace root.
5. **Update:** To update an existing note, the user selects it or JARVIS resolves it unambiguously. Show a diff/preview and require approval before replacing/appending. Never silently overwrite a note or auto-merge new articles.
6. **Follow-up:** Creating a Task or linking the Page to a Task/Project is a separate explicit action. News retrieval or note creation alone must not create tasks, links, alerts, or ongoing monitoring.

Users can request a structure/style in natural language before drafting. Start with one stable default template; do not build a template-management subsystem until repeated use demonstrates a need.

## Default Issue Note template

```markdown
---
id: <stable-page-id>
title: <user-editable issue title>
type: issue-note
status: monitoring
created: <ISO-8601>
updated: <ISO-8601>
---

# <Issue title>

## Current summary
<Short, attributed synthesis; as-of time; no unsupported certainty.>

## What sources report
- <Claim or event> [1]

## What is confirmed vs. uncertain
- Reported by sources:
- Provider annotations (not fact checks):
- Unconfirmed / conflicting:

## Timeline
- <Published date> — <event/report> [1]

## Open questions
- <What would change the assessment?>

## Sources
1. <Publisher>, “<headline>,” <published time>, <URL>. Retrieved <time>. Provider UUID: <id>. Provider sentiment/trust labels: <labels, attributed>.
```

The initial note should store source metadata, links, concise attributed summaries, and only short quotation/excerpt snippets where needed. Do not persist the full Webz response, API token, or entire article body by default. Confirm Webz.io account/licensing terms before adding long excerpts or full-text retention/republication.

## Ownership and boundaries

- **Electron/JARVIS UI:** user intent, provider disclosure/settings, result selection, editable draft, save/update confirmation, source links, and any Task/link follow-up.
- **Harness:** provider adapter, bounded request/response normalization, schema validation, trace redaction, and deterministic Page-write operation once authorized.
- **Webz.io:** external news retrieval and provider-assigned enrichment only. It does not become a canonical JARVIS datastore or truth authority.
- **Knowledge Pages:** canonical Issue Note Markdown content. Reuse Page identity/indexing behavior; do not create a second issue database.
- **Tasks/ResourceLinks:** existing canonical stores continue to own these domains; optional follow-up is a distinct user-approved operation.

The current Harness `PageStore` scans Markdown Pages and maintains derived identity metadata, but is read-only. The current `create_file` tool writes only inside approved Workspace roots and requires confirmation; it is not automatically the correct Page-creation path. A future implementation must add/use a dedicated Page write path rooted under configured `pages_dir`, with unique-path/no-overwrite checks, bounded content, atomic file replacement for safe new creation, and identity refresh. If the Electron app already owns Page creation, use its established bridge instead and keep a single writer.

## External query and safety rules

1. Webz.io integration is opt-in and disabled when no key/provider configuration exists. A user-initiated search is explicit for that query; no proactive/background polling in V1.
2. Keep the API key outside renderer code, prompts, source control, Issue Notes, and traces. The documented News API puts `token` in the GET query string, so redact request URLs and errors; never log the token.
3. Do not send private workspace/file context with news queries. Queries can reveal user interests to the provider; show this disclosure before first use and provide a disable/clear setting.
4. Bound result count, response bytes, timeout, and pagination/credits. Handle missing key, provider errors, rate/credit limits, empty results, and stale/cache responses without breaking ordinary local JARVIS use.
5. Preserve provider uncertainty: show publication time separately from retrieval/cache time; treat `sentiment`, `categories`, `trust`, `ai_allow`, and similar fields as attributed provider labels. A trust flag such as `fake_news` must remain visible in results and notes, not silently become either a definitive verdict or disappear.
6. Deduplicate syndicated copies where possible; do not treat multiple copies of one wire story as independent corroboration.
7. Keep provider response data transient by default. Any cache is derived, bounded, expiring, and rebuildable—not canonical source material.

## Suggested delivery slices

**Slice 1 — contract and mock:** Define a provider-neutral `NewsArticle` result schema and mock cases, including a trust warning, conflicting dates, syndication, empty/error/stale responses. No key or network call required.

**Slice 2 — user-requested search:** Add one Webz adapter behind a provider interface; configure credentials outside code; add a read-only search tool and user-visible source cards. Validate limits, redaction, timeout, and opt-in behavior with mocked HTTP tests before a live account call.

**Slice 3 — draft only:** Select result cards and produce a cited Markdown draft in chat; no disk mutation.

**Slice 4 — approved Page save:** Add the Page-specific save/replace flow and real-surface approval preview; verify new note identity, no overwrite, citations, and recovery from failure. Keep Task creation/linking separate.

Do not combine provider integration, Page-write architecture, proactive alerts, and Task automation into one milestone.

## Acceptance criteria

- A user can ask for current coverage in natural language and see bounded, deduplicated, clickable, dated results.
- Provider sentiment/trust labels are explicitly attributed; suspicious/fake-news flags and conflicting coverage are not hidden.
- The user can choose sources, request a draft, edit it, and cancel without any state change.
- Saving/updating is an explicit reviewed action; the resulting Markdown Page contains provenance and no API secret/full response by default.
- Repeating the same search/draft does not create duplicate notes unless the user chooses a new note.
- Missing credentials, offline provider, rate/credit limit, malformed data, or empty results leave local JARVIS and Workspace flows usable.
- No background polling, automatic Task creation, automatic ResourceLink creation, or unapproved note writes occur.

## Decisions intentionally deferred

- Webz News API versus News Search API: choose after confirming the user's account entitlement, pricing/credits, response/license terms, and a small relevance evaluation. The supplied sample alone does not identify which endpoint/wrapper produced it.
- Exact disclosure cadence and any persistent provider-consent setting.
- Whether the Issue Note template is user-configurable beyond natural-language per-draft instructions.
- Webz cache TTL and long-term article retention; default to no persistent full article cache until terms and actual use are known.
- Cross-provider search, automatic monitoring/alerts, and machine-generated Tasks.
