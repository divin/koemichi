# Plan: move intent workflows from n8n into Python

## Goal and architecture

Replace the per-intent n8n workflows with Python implementations, migrating one intent at a time.

Keep classification separate from execution:

1. The existing intent classifier chooses `memo`, `journal`, `todo`, or `research`.
2. A specialist PydanticAI agent for that intent receives only its own prompt, context, and any read-only tools it genuinely needs.
3. The specialist handles only unstructured interpretation/generation and returns a typed result. Deterministic orchestration and side effects remain in application code.

FastMCP servers are importable components for the future agents; they do not need to be deployed as separate services. Koemichi has no n8n fallback: an intent without an implemented Python handler fails explicitly until its handler is added.

## Current baseline

- The transcript receiver persists submissions to SQLite and deduplicates by `note_id`.
- The worker classifies notes, persists the intent, and routes it to its registered Python handler. Intents without handlers are marked as terminal errors.
- The classifier already has a deterministic keyword fast path and a PydanticAI fallback with a typed `IntentDecision` (`intent`, `confidence`, `reason`). Preserve this classifier as a separate stage unless implementation reveals a specific need to change it.
- The worker has leases, retry scheduling, and a notification outbox. Retries reuse the saved intent instead of classifying again.
- FastMCP is a project dependency. The importable NoteDiscovery MCP server exposes the non-destructive note/search tools; it is not a Compose service.

## Shared implementation principles

- Keep each specialist agent's prompt, context, output schema, and any necessary read-only tools specific to its intent.
- Limit agents to unstructured tasks such as understanding a transcript, extracting a title, or improving wording. Use typed Pydantic outputs and validate them.
- Do not let an agent invent facts beyond the transcript. It may improve wording, readability, and structure.
- Handle deterministic behavior in code: intent dispatch, dates/timezones, path and slug generation, folder creation, collision handling, duplicate checks, API writes, retries, and persistence.
- Do not expose deterministic write operations as choices for the agent to make. Application code should execute validated writes through the appropriate client/tool after the agent returns its structured result.
- Persist generated content and the selected destination before external side effects. Retries must reuse those values and be idempotent.
- Keep credentials in environment configuration and provide only the credentials needed for each integration.
- Preserve current lifecycle notifications and processing retries unless a workflow requirement calls for a change.
- Add safe operational logs for workflow stages and failures; never log transcripts, generated note bodies, credentials, or auth headers.
- In `normal` and `debug` modes, enqueue Pushover notifications through the durable outbox: normal sends major milestones at priority 0, debug adds minor diagnostics at priority -1, and terminal failures use priority 1. Respect the existing explicit `off` mode.
- Deduplicate notifications with stable event keys and enqueue milestone notifications transactionally with the corresponding persisted state where possible; do not call Pushover directly from agents or handlers.

## Phase 1: verify intent classification

**Status: implemented and verified.**

- The classifier stays separate from intent-specific execution.
- It uses a keyword fast path, then a PydanticAI agent for unmatched non-empty transcripts; empty transcripts default to `memo`.
- The PydanticAI agent has a typed `IntentDecision` output: the intent is constrained to the `Intent` enum, confidence is constrained to 0–1, and reason is optional.
- Tests cover keyword matching, the memo fallback, missing model configuration, and rejection of invalid structured outputs. The router test module passes.
- No classifier rewrite is needed before proceeding; preserve this boundary as specialist agents are added.

## Phase 2: NoteDiscovery API as FastMCP tools

**Status: implemented and verified for the memo/journal note workflows.**

- The importable FastMCP server object is `koemichi.services.mcp.server.mcp`.
- Agent-visible tools are read-only: `search_notes`, `list_notes`, and `get_note`.
- Do not register write or destructive operations as agent tools. Deterministic application code uses `NoteDiscoveryClient` directly for folder creation, note creation/update, and append operations.
- Configure `NOTEDISCOVERY_API_URL` in the process importing the server.
- Tests cover API mapping, HTTP failures, path safety, pagination, and MCP tool registration.

## Phase 3: specialist-agent execution

**Status: execution boundary implemented and used by the memo specialist.**

- The worker routes a persisted intent to its registered Python handler. An unregistered intent is marked as a terminal error immediately; it is never sent to n8n or another fallback.
- Memo has a typed PydanticAI specialist with no tools; it handles only title/content generation. Application code handles all NoteDiscovery reads and writes.
- Future agents receive intent-specific prompts, typed outputs, and only necessary read-only tools. Compose MCP capabilities in-process; do not start a separate MCP service.
- Model configuration is required for LLM classification and memo generation; NoteDiscovery configuration is validated at worker startup.


## Phase 4: implement `memo`

**Status: implemented; NoteDiscovery folder/create/read-back smoke test passed. Full worker-plus-LLM live testing requires deployment.**

### Behavior

- Save one memo per transcript in the dedicated `Memo` folder; application code ensures the folder exists.
- Application code derives the memo date from `recorded_at_ms` using the configured `TZ` environment variable.
- The specialist handles only the unstructured work: produce a useful title and readable, structured memo text without adding unsupported facts.
- Application code slugifies the title, builds `Memo/YYYY-MM-DD-title.md`, checks for path collisions, and adds a numeric suffix such as `(1)` before `.md` when needed.
- Persist the generated title/content and chosen path before writing, then write through the NoteDiscovery client/tools in code. Retries reuse the same path and content.

### Implementation and verification

- Unit tests cover typed output validation, date conversion, title slugging, collision selection, lease-guarded persistence, and retry reuse.
- Test that memo uses its Python implementation and unimplemented intents fail clearly without an external fallback.
- Verify normal mode queues only major memo milestones, debug mode adds low-priority processing details, and terminal failures use priority 1.
- Mock NoteDiscovery calls and verify no transcript content or credentials leak into logs or notifications.
- Before release, run against a live NoteDiscovery instance to verify folder creation is safe/idempotent and repeated create/update calls behave correctly after a retry.
- Roll out memo first and verify successful writes, collision handling, and retries before migrating another intent.

## Phase 5: implement `journal`

**Status: implemented; live testing requires deploying the current worker.**

- Save journal entries under `Journal/YYYY/MM/YYYY-MM-DD.md`, with a zero-padded month and date as the title.
- Use the recorded timestamp and configured `TZ` for local calendar dates. The recorded local date is treated as today; code prefers yesterday unless that file exists, then selects today.
- The specialist handles only the unstructured work: improve readability and structure without inventing content. Code supplies the date heading.
- Application code ensures the year/month folders exist, checks for a path collision, and creates a numeric-suffixed file rather than appending if the selected date's file already exists.
- Persist the generated content and destination before writes; retries reuse both values.
- Tests cover date paths, yesterday/today/collision selection, output validation, lease-guarded persistence, retry reuse, and privacy-safe logs.

## Phase 6: implement `todo` with Vikunja tools

- Create an importable, minimal Vikunja MCP tool set using the API key from environment configuration.
- Initial operations are adding a task and listing tasks; there is no need for a mark-as-done tool.
- The specialist extracts a task title, description, and optional explicitly provided due date as structured output. Code lists tasks in the dedicated project to check for duplicates and performs any task creation; do not let the agent decide whether to make API writes.
- Use the dedicated Vikunja project/inbox and do not create a task if the same task is already present.
- Remove the temporary `api-1.json` API reference once the required endpoints are established.

## Later: implement `research`

Design and implement the research pipeline separately after the memo, journal, and todo workflows are established.

## Remaining decisions

- Create Vikunja tasks with a title and description, and include a due date only when one is provided in the transcript. Do not infer a due date.
