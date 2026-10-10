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

The specialist returns a typed structure:

- `title`
- `summary`
- `key_points`
- `explicit_next_steps`

Python renders a fixed Markdown layout with a title and summary, adding key-point and next-step sections only when they are nonempty. The prompt asks for next steps only when the speaker explicitly states them and forbids invented advice or generic report sections.

The NoteDiscovery path is `Memo/YYYY-MM-DD-title.md`, using the recording time in `TZ`. Python normalizes the title for the filename, checks for collisions, and adds a numbered suffix when needed.

### Journal

The specialist returns simple prose, not a report, list, or generic label such as “Journal Entry” or “Tagebucheintrag.” Python adds only the date heading and writes to `Journal/YYYY/MM/YYYY-MM-DD.md`. The recording's local date is treated as today; the previous date is preferred when its journal file is available. If destinations already exist, Python adds a numbered suffix instead of overwriting or appending.

Both specialists are instructed to preserve the transcript's language, including German, and to retain language switches in multilingual input. They should correct clear transcription errors without changing meaning or adding unsupported facts.

## Deterministic behavior and retries

Agents only produce typed text fields. Python chooses paths and folders, checks collisions, persists drafts before external writes, and performs the NoteDiscovery API operations. If a NoteDiscovery write fails, a retry reuses the saved draft and path rather than generating a new note.


The worker stores stage, attempt, lease, and retry state in SQLite. Transcripts move through classification and workflow execution. Temporary failures are retried; exhausted retries and unimplemented intents are marked as errors. `dispatched` means the selected Python workflow completed.
