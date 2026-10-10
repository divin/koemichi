# Transcript API reference

## `GET /`

A lightweight health check that returns `200 {"status":"ok"}`. It does not test database writes, LLM connectivity, NoteDiscovery access, or notification delivery.

## `POST /transcripts`

Accepts a completed transcript from Koebako or another compatible sender. The request body is JSON:

```json
{
  "schema_version": 1,
  "note_id": "stable-note-uuid",
  "source": "memo",
  "recorded_at_ms": 1760000000000,
  "transcript": "recognized words"
}
```

| Field | Type | Description |
|---|---|---|
| `schema_version` | integer | Must be `1`. |
| `note_id` | UUID string | Stable sender-assigned identifier used for idempotency. |
| `source` | string | Currently `memo`. |
| `recorded_at_ms` | integer | Recording time as Unix milliseconds. |
| `transcript` | string or `null` | Recognized words; surrounding whitespace is stripped. |

Unknown fields and unsupported schema versions are rejected. If `TRANSCRIPT_INGEST_TOKEN` is configured, include:

```http
Authorization: Bearer <TRANSCRIPT_INGEST_TOKEN>
```

If the token is unset, the receiver does not authenticate requests. Keep such deployments on a trusted network; see [deployment and configuration](deployment.md#receiver-authentication-and-exposure).

### Responses and idempotency

- `200 {"status":"accepted"}` — a new transcript was committed to SQLite.
- `200 {"status":"duplicate"}` — the same `note_id` and transcript data were already accepted.
- `409` — the `note_id` already exists but its source, timestamp, or transcript differs.
- `401` — a token is configured but the Bearer credential is missing or incorrect.
- `422` — the JSON body does not satisfy the schema.

The receiver commits the transcript before returning `accepted`. The worker processes it asynchronously. The sender may retry after timeouts; stable IDs make repeated delivery safe.
