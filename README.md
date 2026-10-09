# Koemichi 🎙️

**A private voice-note pipeline for the [Pebble Index](https://repebble.com/index).** Koemichi receives a recording, stores it, transcribes and classifies it, sends the result to your chosen webhook, and reports progress through Pushover. The goal is to preserve your ownership and control of recordings and transcripts, rather than require a third-party cloud platform; you choose the services that process and receive your data.

The name is a Japanese-inspired play on *koe* (声, “voice”) and *michi* (道, “path” or “road”): a voice route for routing my commands.

## 🧭 How it works

```mermaid
flowchart TD
    A[ Pebble Index or other audio client ] -->|Authenticated multipart upload| B[FastAPI ingest]
    B -->|Store recording and note state| C[(Audio files and SQLite)]
    C -->|Pending notes| D[Single SQLite worker]
    D -->|Audio| E[Configured ASR service]
    E -->|Transcript| F[Keyword classification]
    F -->|Keyword match| G[Intent]
    F -->|No match| H[Optional LLM fallback]
    H --> G
    G --> I[Configured HTTP POST webhook]
    D --> K[Pushover outbox]
    K --> L[Pushover]
```

The API and worker are separate processes built from the same image and share persistent storage. A single SQLite worker handles note processing and retries; a separate outbox keeps notification delivery failures from blocking notes. There is no Redis or general-purpose task broker.


The speech-to-text service, optional LLM, and webhook receiver are external integrations. The webhook must accept the HTTP POST payload described below.

### 🤖 Example self-hosted AI services

For my setup, speech recognition is provided by the CPU Docker container from [`audio.cpp`](https://github.com/0xShug0/audio.cpp), using the [Qwen3-ASR-1.7B GGUF model](https://huggingface.co/audio-cpp/audio.cpp-gguf/tree/main/Qwen3-ASR-1.7B-GGUF). I use a separate Docker container running [`llama.cpp`](https://github.com/ggml-org/llama.cpp) as the OpenAI-compatible LLM endpoint for intent-classification fallback. Configure `STT_URL` and `STT_MODEL_NAME` for the ASR container, and `LLM_URL` and `LLM_MODEL_NAME` for the llama.cpp server.

These are my deployment choices, not bundled Koemichi dependencies; other compatible ASR and LLM services can be used instead.

## ⚙️ Requirements and configuration

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
| `DISPATCH_WEBHOOK_URL` | Worker | HTTP endpoint to receive classified-note POST requests. |
| `NOTIFICATION_MODE` | No | `normal` by default; supported values are `normal`, `debug`, and `off`. |
| `PUSHOVER_API_TOKEN` | Unless notifications are off | Pushover application API token. |
| `PUSHOVER_USER_KEY` | Unless notifications are off | Pushover user or group key. |
| `HOST_DATA_DIR` | No, Compose only | Host directory mounted at `/data`; defaults to `./data`. |
| `DATA_DIR` | No | Application storage directory; defaults to `./data` for local runs and `/data` in Compose. |
| `DATABASE_PATH` | No | SQLite path; defaults to `${DATA_DIR}/koemichi.db`. |
| `TZ` | No | Time zone used to organize audio files; defaults to `Europe/Berlin`. |

The worker validates its ASR, LLM, and webhook configuration at startup. It also requires both Pushover credentials when notifications are enabled. Set `NOTIFICATION_MODE=off` to disable notifications without those credentials. Configure service URLs so they are reachable from the process or container using them.

## 🚀 Run with Compose

The Compose file uses the image `ghcr.io/divin/koemichi:latest`. With a published image and a configured `.env` file:

```sh
docker compose pull
docker compose up -d
docker compose logs -f ingest worker
```

The API is bound to `127.0.0.1:8000`; the worker has no published port. Both containers share `${HOST_DATA_DIR:-./data}` mounted at `/data`. The worker is configured as a single instance.

## 📥 Webhook API

| Method and path | Authentication | Purpose |
|---|---|---|
| `GET /` | None | Connectivity ping; returns `200 {"status":"ok"}`. Does not create or process a note. |
| `POST /memo` | Bearer token | Accepts an audio recording; acknowledges audio-less test events without processing. |
| `POST /action` | Bearer token | Deprecated; returns `410 Gone` without processing the upload. |

For `POST /memo`, send multipart form data with `recordedAt` (Unix timestamp in milliseconds) and `client` (client identifier); real memo uploads must also include `audio` (the recording file). `transcription` is optional. The optional `X-Audio-Size` header checks that the stored file matches the client-reported byte count; it is not the upload-size security limit. An authenticated request with the required form fields but no audio is acknowledged with `200 {"status":"test_event"}` and does not create a note. This supports senders that issue audio-less test events, but it only checks webhook reachability—not transcription or downstream dispatch.

**Pebble Index requires an externally reachable HTTPS webhook URL.** Serving Koemichi only on localhost or your local network is not enough; expose the API through a TLS-terminating reverse proxy and configure its HTTPS URL for Pebble. The localhost URL in the example below is for local testing only.

Example upload:

```sh
curl -X POST http://127.0.0.1:8000/memo \
  -H 'Authorization: Bearer REPLACE_WITH_WEBHOOK_TOKEN' \
  -F 'recordedAt=1760000000000' \
  -F 'client=pebble' \
  -F 'audio=@voice-note.m4a;type=audio/mp4'
```

An accepted upload returns HTTP `202` with `{"status":"accepted"}`. The service rejects invalid credentials, audio-size mismatches, and audio larger than **25 MiB** (HTTP `413`). An authenticated oversized upload also queues a rate-limited Pushover alert; it does not create a note.

Notes move through `received → transcribing → transcribed → routing → routed → dispatched`, or reach terminal `error` after retries are exhausted. `dispatched` means the webhook accepted the request; it does not indicate whether downstream actions completed successfully.

## 🧠 Classification and dispatch

The classifier first checks the beginning of the transcript for a configured keyword phrase. If none matches, it asks the configured LLM to choose an intent. Every note is assigned one of these intent values:

| Intent | Meaning | Keyword fast path (transcript starts with) |
|---|---|---|
| `journal` | Personal reflection or diary entry. | `journal`, `diary`, `tagebuch` |
| `todo` | An actionable task or reminder. | `todo`, `to-do`, `task`, `aufgabe` |
| `memo` | General-note category and fallback when no more specific intent fits. | `memo`, `note`, `notiz`, `remember` |
| `research` | A request to look something up or research a topic. | `research`, `recherche`, `look up`, `lookup`, `search` |

The service sends this JSON payload to the configured webhook:

```json
{
  "note_id": "stable-note-uuid",
  "transcript": "recognized words",
  "intent": "memo"
}
```

Koemichi sends this payload as an HTTP POST with a JSON body to `DISPATCH_WEBHOOK_URL`. Any 2xx response counts as accepted; the response body is ignored. `note_id` remains stable across retries so the receiver can deduplicate side effects. The receiver can branch on `intent` and is responsible for downstream actions.

## 🔔 Notifications and privacy

Create a Pushover application to obtain an application API token, and configure it with your Pushover user or group key. Keep both credentials in `.env` or a deployment secret manager. Only the worker receives the Pushover credentials. Notification events are stored in SQLite and delivered asynchronously.

| Mode | Notifications |
|---|---|
| `normal` | Priority 0 when a note is received, transcribed, and dispatched with its intent; priority 1 for terminal processing errors. |
| `debug` | Lifecycle messages plus low-priority (`-1`) stage, retry, recovery, and detailed transcription/dispatch notifications; terminal processing errors remain priority 1. |
| `off` | No notifications are queued or delivered; Pushover credentials are not required. |

Routine Pushover messages do not include the full transcript. Debug messages contain limited operational details, not the transcript. Notifications may appear on a lock screen, so treat them as private data. Audio and transcripts are sent to the configured ASR service; transcripts may be sent to the classifier LLM and are sent with their intent to the dispatch webhook. Choose those services with these data flows in mind.

Pushover priority `2` (emergency/repeating) is not used for routine updates. Delivery failures are retried with bounded backoff and recorded in the outbox.

## 💾 Storage and backups

By default, Compose stores the SQLite database at `/data/koemichi.db` and audio below `/data/memos/` and `/data/actions/`. On the host, these files live in `${HOST_DATA_DIR:-./data}`. A direct local run defaults to `./data` and derives the database path as `./data/koemichi.db`. Keep the selected directory on persistent storage and back up the whole directory so the database and referenced recordings stay together. Use a local filesystem for SQLite rather than NFS/SMB.

SQLite runs in WAL mode. For a simple consistent backup, stop both services, copy the configured data directory, and restart them:

```sh
docker compose stop ingest worker
# Copy the configured HOST_DATA_DIR (default: ./data) to your backup destination.
docker compose start ingest worker
```

Do not copy only `koemichi.db` while services are running; committed data may still be in the WAL file. Use SQLite's online backup mechanism for live backups.

## ⚠️ Deployment limitations

Koemichi is intended for a single-user deployment on one host with one worker. Do not run multiple worker replicas or put the SQLite database on a network filesystem.

## 🔒 Internet-facing deployments

Compose binds the API to loopback. If exposing it through a reverse proxy:

- Terminate TLS at the proxy and keep port `8000` private; do not expose the application directly to the internet.
- Enforce a maximum total request-body size at the proxy, slightly above 25 MiB to allow for multipart framing and form fields. The application checks the audio file size after multipart parsing, so that check alone does not protect the parser from oversized request bodies.
- Apply conservative request-rate limits.
- Keep `WEBHOOK_TOKEN` high entropy, store credentials as secrets, and rotate exposed values. Never log authorization headers or credentials.

The proxy is not bundled or configured here. Do not publish SQLite, ASR, LLM, or webhook administration interfaces publicly.

## 🧪 Development checks

With Python 3.12, `uv`, and [`just`](https://just.systems/man/en/) installed:

```sh
uv sync --dev
just        # Format, lint, and sort imports
just check  # Type-check and run non-mutating checks
just test   # Run the test suite
```
