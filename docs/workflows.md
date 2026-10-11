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

Only intents with a registered Python handler are implemented. Currently those are Memo and Journal. Todo and Research are recognized, but fail explicitly as unimplemented; there is no n8n or webhook fallback.

### Memo

The specialist extracts only a short descriptive title, preserving the transcript's language. Koemichi keeps the transcript itself as the note body without AI rewriting or summarization.

The note title and filename use the recording's local date and zero-padded time in `TZ`, followed by the descriptive title; for example:

```text
Memo/2025-01-01 14:30 - Meeting with Alice.md
```

Python sanitizes path separators in the title, checks for collisions, and adds a numbered suffix when needed. The Markdown heading uses the same readable title as the filename.

### Journal

Journal entries are raw transcript text; no AI specialist rewrites them. The recording's local date in `TZ` selects `Journal/YYYY/MM/YYYY-MM-DD.md`. Koemichi creates the dated file when it does not exist and appends each new transcript to the same day's file.

Each append carries a hidden, stable marker derived from the transcript's note ID. On retry, Koemichi checks for that marker before appending, so a timeout after a successful API write does not duplicate the entry. Existing journal content is preserved.

## Deterministic behavior and retries

Python selects dates and paths, ensures folders, persists memo titles/content and journal transcripts/destinations before external writes, and performs NoteDiscovery API operations. Memo retries reuse the saved title, content, and path. Journal retries reuse the saved transcript and path, and the stable entry marker prevents duplicate appends.

The worker stores stage, attempt, lease, and retry state in SQLite. Temporary failures are retried; exhausted retries and unimplemented intents are marked as errors. `dispatched` means the selected Python workflow completed.
