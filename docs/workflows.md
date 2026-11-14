# Classification and intent workflows

## Choosing an intent

Koemichi checks the start of each transcript for a known keyword phrase. When no starting phrase matches, it asks the configured OpenAI-compatible LLM to choose an intent. Empty transcripts default to `memo`. The LLM is only used for classification when keyword matching does not decide the route.

| Intent | Purpose | Example starting phrases |
|---|---|---|
| `journal` | Personal reflection or diary entry | `journal`, `diary`, `tagebuch` |
| `todo` | Actionable task or reminder | `todo`, `to-do`, `task`, `aufgabe` |
| `memo` | General note and fallback | `memo`, `note`, `notiz`, `remember` |
| `research` | Look something up | `research`, `recherche`, `look up`, `lookup`, `search` |

Phrase matching is case-insensitive. See `src/koemichi/services/router/intents.py` for the source of truth.

## Implemented workflows

Only intents with a registered Python handler are implemented. Currently those are Memo, Journal, Todo, and Research; there is no n8n or webhook fallback.

### Memo

The specialist extracts only a short descriptive title, preserving the transcript's language. Koemichi keeps the transcript itself as the note body without AI rewriting or summarization.

The note title and filename use the recording's local date and zero-padded time in `TZ`, followed by the descriptive title. The vault-relative destination folder defaults to `Memo` and can be set with `NOTEDISCOVERY_MEMO_FOLDER`; for example:

```text
Memo/2025-01-01 14:30 - Meeting with Alice.md
```

Python sanitizes path separators in the title, checks for collisions, and adds a numbered suffix when needed. The Markdown heading uses the same readable title as the filename.

### Journal

Journal entries are raw transcript text; no AI specialist rewrites them. The recording's local date in `TZ` selects `<journal-folder>/YYYY/MM/YYYY-MM-DD.md`; the vault-relative root defaults to `Journal` and can be set with `NOTEDISCOVERY_JOURNAL_FOLDER`. Koemichi creates the dated file when it does not exist and appends each new transcript to the same day's file.

Each append carries a hidden, stable marker derived from the transcript's note ID. On retry, Koemichi checks for that marker before appending, so a timeout after a successful API write does not duplicate the entry. Existing journal content is preserved.

## Deterministic behavior and retries

Python selects dates and paths, ensures folders, persists memo titles/content, journal transcripts/destinations, todo titles, and research titles/content/paths before external writes, and performs the NoteDiscovery and Vikunja operations. Memo retries reuse the saved title, content, and path. Journal retries reuse the saved transcript and path, and the stable entry marker prevents duplicate appends. Todo retries reuse the saved transcript title. Research retries reuse the saved title, content, and path instead of running the research agent again. Changing either folder setting affects new destinations only; it does not move existing notes, and retries continue using their persisted paths.

### Todo

Todo creation does not use an LLM. Koemichi trims only leading and trailing whitespace from the raw transcript, persists it as the Vikunja task title, and sends it directly through the configured Vikunja client. It does not generate a separate description or due date.

The workflow validates the Vikunja configuration, persists the title under the worker lease, and lists the inbox project to check whether an equivalent title already exists before creating a task. This duplicate check and the saved title make retries safe after a crash around the Vikunja write. Older in-flight tasks with an already-persisted draft continue using that saved draft.

### Research

The research topic is extracted deterministically: the matched keyword phrase and any following punctuation are stripped from the transcript, and the remainder is the research question verbatim. A transcript that contains only the keyword (or no keyword) fails before any side effect.

The research specialist is a bounded agentic loop over the read-only Sagashi MCP tools (`SAGASHI_MCP_URL`): web search, Reddit search and thread fetching, page fetching, and YouTube transcript fetching. The agent chooses its own searches up to `RESEARCH_MAX_TOOL_CALLS` tool calls; once the budget is spent, a budget message replaces further tool results and the agent must summarize what it gathered. `RESEARCH_TIMEOUT_SECONDS` caps the wall-clock time of the run. The agent returns a typed result: a short descriptive title, a factual summary in the transcript's language, and at least one source. Sources are mandatory; a summary without citations is rejected, and a run that finds nothing usable fails like any other workflow error.

Koemichi renders the note as the research question, the summary, and a `Sources` list of Markdown links. The vault-relative destination folder defaults to `Research` and can be set with `NOTEDISCOVERY_RESEARCH_FOLDER`; the filename uses the same recording date/time and sanitized title convention as Memo, with numbered suffixes on collision. The rendered title, content, and path are persisted under the worker lease before the NoteDiscovery write, and retries reuse that draft instead of running the agent again. Temporary failures (Sagashi or the LLM unreachable) use the normal retry schedule; exhausted retries are marked as errors and notified at priority 1, with no note written.

Temporary failures are retried; exhausted retries and unimplemented intents are marked as errors. `dispatched` means the selected Python workflow completed.
