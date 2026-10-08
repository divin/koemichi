# Koemichi

Koemichi is a voice-note processing service for a single user and a single host. It accepts authenticated audio uploads, stores recordings and processing state, transcribes and classifies notes, sends results to a configured webhook, and reports lifecycle events through Pushover.

## Architecture and scope

```text
Audio client
    │ authenticated multipart upload
    ▼
FastAPI ingest ──► audio files + note state in SQLite
                            │
                            ▼
                    single SQLite worker
                    ├── transcription ──► configured ASR service
                    ├── classification ──► keyword match, then LLM fallback
                    ├── dispatch ────────► configured webhook (for example, n8n)
                    └── notifications ──► Pushover outbox
```

The API and worker are separate processes built from the same image and share a persistent data directory. SQLite stores note state and supports the worker's polling and retry loop. Pushover notifications use a separate outbox so delivery failures do not block note processing. Redis and a general-purpose task broker are not used.

This design is intended for one host and one worker. Do not run multiple worker replicas or put the SQLite database on a network filesystem.

This repository contains the Python services and deployment files. Webhook workflows (including n8n workflow definitions) are external dependencies and are not included here. The configured webhook must accept the dispatch payload described below.

## Requirements and configuration

For Compose, create a local environment file from the sample:

```sh
cp example.env .env
```

Edit `.env` with values for your environment. The sample token is a placeholder. Do not commit `.env` or put real credentials in `example.env`.

| Variable | Required? | Purpose |
|---|---|---|
| `WEBHOOK_TOKEN` | Yes | High-entropy Bearer token required by the ingest endpoints. |
| `STT_URL` | Yes | Speech-to-text endpoint. |
| `STT_MODEL_NAME` | Yes | ASR provider/model label used by the transcription client and notices. |
| `LLM_URL` | Worker | OpenAI-compatible API base URL used for intent fallback. |
| `LLM_MODEL_NAME` | Worker | Model used for intent fallback. |
| `N8N_WEBHOOK_URL` | Worker | Dispatch webhook URL. |
| `NOTIFICATION_MODE` | No | `normal` by default; supported values are `normal`, `debug`, and `off`. |
| `PUSHOVER_API_TOKEN` | Unless notifications are off | Pushover application API token. |
| `PUSHOVER_USER_KEY` | Unless notifications are off | Pushover user or group key. |
| `HOST_DATA_DIR` | No, Compose only | Host directory mounted at `/data`; defaults to `./data`. |
| `DATA_DIR` | No | Application storage directory; defaults to `./data` for local runs and `/data` in Compose. |
| `DATABASE_PATH` | No | SQLite path; defaults to `${DATA_DIR}/koemichi.db`. |
| `TZ` | No | Time zone used to organize audio files; defaults to `Europe/Berlin`. |

The worker validates its ASR, LLM, and webhook configuration at startup. It also requires both Pushover credentials when notifications are enabled. Set `NOTIFICATION_MODE=off` to disable notifications without those credentials. Configure service URLs so they are reachable from the process or container using them.

## Run with Compose

The Compose file uses the image `ghcr.io/divin/koemichi:latest`. With a published image and a configured `.env` file:

```sh
docker compose pull
docker compose up -d
docker compose logs -f ingest worker
```

The API is bound to `127.0.0.1:8000`; the worker has no published port. Both containers share `${HOST_DATA_DIR:-./data}` mounted at `/data`. The worker is configured as a single instance.

## Webhook API

Both endpoints require `Authorization: Bearer <WEBHOOK_TOKEN>` and multipart form data:

- `POST /memo` stores audio under `memos/`.
- `POST /action` stores audio under `actions/`.

Required form fields are `audio` (recording file), `recordedAt` (Unix timestamp in milliseconds), and `client` (client identifier). `transcription` is optional. The optional `X-Audio-Size` header checks that the stored file matches the client-reported byte count; it is not the upload-size security limit.

Example upload:

```sh
curl -X POST http://127.0.0.1:8000/memo \
  -H 'Authorization: Bearer REPLACE_WITH_WEBHOOK_TOKEN' \
  -F 'recordedAt=1760000000000' \
  -F 'client=pebble' \
  -F 'audio=@voice-note.m4a;type=audio/mp4'
```

An accepted upload returns HTTP `202` with `{"status":"accepted"}`. The service rejects missing audio, invalid credentials, audio-size mismatches, and audio larger than **25 MiB** (HTTP `413`). An authenticated oversized upload also queues a rate-limited Pushover alert; it does not create a note.

Notes move through `received → transcribing → transcribed → routing → routed → dispatched`, or reach terminal `error` after exhausted retries. `dispatched` means the webhook accepted the request; it does not indicate whether downstream workflow actions completed successfully.

## Classification and dispatch contract

The classifier first checks the beginning of a transcript for configured intent phrases. If none match, it calls the configured LLM and validates the result against these intents: `journal`, `todo`, `memo`, `research`, and `other`.

The service sends this JSON payload to the configured webhook:

```json
{
  "note_id": "stable-note-uuid",
  "transcript": "recognized words",
  "intent": "memo"
}
```

`note_id` remains stable across dispatch retries so the receiver can deduplicate side effects. The receiving workflow is responsible for handling this contract and its downstream actions.

## Notifications and privacy

Create a Pushover application to obtain an application API token, and configure it with your Pushover user or group key. Keep both credentials in `.env` or a deployment secret manager. Only the worker receives the Pushover credentials. Notification events are stored in SQLite and delivered asynchronously.

| Mode | Notifications |
|---|---|
| `normal` | Priority 0 when a note is received, transcribed, and dispatched with its intent; priority 1 for terminal processing errors. |
| `debug` | Normal lifecycle messages plus low-priority stage, retry, and recovery details. Completion messages can include provider/timing and classifier-path details. |
| `off` | No notifications are queued or delivered; Pushover credentials are not required. |

Routine Pushover messages do not include the full transcript. Debug messages contain limited operational details, not the transcript. Notifications may appear on a lock screen, so treat them as private data. Audio and transcripts are sent to the configured ASR service; transcripts may be sent to the classifier LLM and are sent with their intent to the dispatch webhook. Choose those services with these data flows in mind.

Pushover priority `2` (emergency/repeating) is not used for routine updates. Delivery failures are retried with bounded backoff and recorded in the outbox.

## Storage and backups

By default, Compose stores the SQLite database at `/data/koemichi.db` and audio below `/data/memos/` and `/data/actions/`. On the host, these files live in `${HOST_DATA_DIR:-./data}`. A direct local run defaults to `./data` and derives the database path as `./data/koemichi.db`. Keep the selected directory on persistent storage and back up the whole directory so the database and referenced recordings stay together. Use a local filesystem for SQLite rather than NFS/SMB.

SQLite runs in WAL mode. For a simple consistent backup, stop both services, copy the configured data directory, and restart them:

```sh
docker compose stop ingest worker
# Copy the configured HOST_DATA_DIR (default: ./data) to your backup destination.
docker compose start ingest worker
```

Do not copy only `koemichi.db` while services are running; committed data may still be in the WAL file. Use SQLite's online backup mechanism for live backups.

## Internet-facing deployments

Compose binds the API to loopback. If exposing it through a reverse proxy:

- Terminate TLS at the proxy and keep port `8000` private; do not expose the application directly to the internet.
- Enforce a maximum total request-body size at the proxy, slightly above 25 MiB to allow for multipart framing and form fields. The application checks the audio file size after multipart parsing, so that check alone does not protect the parser from oversized request bodies.
- Apply conservative request-rate limits.
- Keep `WEBHOOK_TOKEN` high entropy, store credentials as secrets, and rotate exposed values. Never log authorization headers or credentials.

The proxy is not bundled or configured here. Do not publish SQLite, ASR, LLM, or webhook administration interfaces publicly.

## Development checks

With Python 3.12 and `uv` installed:

```sh
uv sync --dev
uv run pytest
uv run ty check .
uv run ruff format --check .
uv run ruff check --extend-ignore I .
uv run ruff check --select I .
```
