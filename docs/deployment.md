# Deployment and configuration

## Compose setup

Copy the example environment and fill in the service addresses and secrets:

```sh
cp example.env .env
```

Then pull the image and start the services:

```sh
docker compose pull
docker compose up -d
docker compose logs -f receiver worker
```

The receiver listens on container port `8000`, published as `8001:8000` by default. `KOEMICHI_PORT` changes the host port. The port mapping listens on all host interfaces; use the host firewall to limit access to trusted networks, especially when authentication is disabled. The worker has no published port. Both services share `HOST_DATA_DIR` (default `./data`) mounted at `/data`.

The image runs as UID/GID `1000:1000`. On Linux, create the data directory and make it writable by that user:

```sh
mkdir -p ./data
sudo chown -R 1000:1000 ./data
```

Do the same for a custom `HOST_DATA_DIR`. Keep the SQLite database on a local filesystem and run one worker per database.

## Required service connectivity

The worker needs:

- `NOTEDISCOVERY_API_URL`: a NoteDiscovery base URL reachable from the worker.
- `LLM_URL` and `LLM_MODEL_NAME`: an OpenAI-compatible model endpoint and the model ID it serves. Both must be configured even though keyword matches avoid a model request.
- `VIKUNJA_API_URL`, `VIKUNJA_API_TOKEN`, and `VIKUNJA_PROJECT_ID`: Vikunja origin, a `tk_...` API token with the `mcp:access` scope, and the numeric id of the dedicated inbox project for voice-captured todos. `VIKUNJA_MCP_URL` optionally overrides the MCP endpoint (defaults to `${VIKUNJA_API_URL}/api/v2/mcp`); it is forwarded to the worker container.

The Vikunja host must resolve from **inside the worker container**. The worker connects to Vikunja's built-in streamable-HTTP MCP server (`/api/v2/mcp`), so use an address the container can resolve and route to: a LAN IP (like the NoteDiscovery example below), a shared Docker network service name, or `host.docker.internal` when Vikunja runs on the host. A hostname only resolvable on the host machine will fail in the worker with `Temporary failure in name resolution`; the worker logs the resolved endpoint at startup as `Connecting to Vikunja MCP endpoint ...`.

If NoteDiscovery or the model server runs in another container, connect the worker to a shared Docker network or use an address reachable from its container. On Linux, `host.docker.internal` is mapped to the host gateway by Compose. The example uses this for NoteDiscovery; change it when your deployment differs.

## Receiver authentication and exposure

`TRANSCRIPT_INGEST_TOKEN` is optional. When set, the sender must supply `Authorization: Bearer <token>`. When unset, the receiver accepts requests without authentication. Because Compose publishes the port on all host interfaces, use the host firewall to keep unauthenticated access limited to trusted local networks. For traffic crossing an untrusted network, enable a long random token and use HTTPS.

If another container sends requests, `127.0.0.1` on the host is not reachable from that container. Use a shared Docker network and receiver service name, or a trusted host/network route. In Hako, the receiver has no host-published port; Koebako sends to `http://koemichi-receiver:8000/transcripts` over shared Docker networks. Do not publicly expose the receiver without HTTPS and authentication. See [API reference](api.md).

## Configuration reference

| Variable | Required? | Purpose |
|---|---|---|
| `TRANSCRIPT_INGEST_TOKEN` | Optional | Bearer token for `POST /transcripts`; unset disables authentication. |
| `NOTEDISCOVERY_API_URL` | Yes for startup/workflows | NoteDiscovery base URL reachable by the worker. |
| `VIKUNJA_API_URL` | Yes for startup/workflows | Vikunja origin reachable by the worker. |
| `VIKUNJA_API_TOKEN` | Yes for startup/workflows | Vikunja `tk_...` API token with `mcp:access` scope. |
| `VIKUNJA_PROJECT_ID` | Yes for startup/workflows | Numeric id of the inbox project todos are added to. |
| `VIKUNJA_MCP_URL` | No | Overrides Vikunja's MCP endpoint; defaults to `${VIKUNJA_API_URL}/api/v2/mcp`. Passed through to the worker by Compose. |
| `LLM_URL` | Worker | OpenAI-compatible language-model API base URL. |
| `LLM_MODEL_NAME` | Worker | Model ID accepted by the configured model server. |
| `NOTIFICATION_MODE` | No | `normal` by default; supports `normal`, `debug`, and `off`. |
| `PUSHOVER_API_TOKEN` | Unless notifications are off | Pushover application token. |
| `PUSHOVER_USER_KEY` | Unless notifications are off | Pushover user/group key. |
| `HOST_DATA_DIR` | Compose | Host directory for the SQLite database; defaults to `./data`. |
| `DATA_DIR` | No | Application data path; Compose uses `/data`, local runs default to `data`. |
| `DATABASE_PATH` | Optional | SQLite database path; defaults to `${DATA_DIR}/koemichi.db`. |
| `KOEMICHI_PORT` | No | Host port for the receiver; defaults to `8001`. |
| `TZ` | Optional | Timezone used for dates; defaults to `Europe/Berlin`. |

## Storage

Koemichi stores its SQLite database at `/data/koemichi.db`. The same file is mounted into the receiver and worker. Back up the complete data directory. The database stores transcript text, workflow drafts, paths, retry state, and notification messages. Memo and Journal content may also be retained in the notification outbox, up to Pushover's message limit. Protect backups and the data directory accordingly.
