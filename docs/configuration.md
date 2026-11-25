# Configuration

[繁體中文](configuration.zh-TW.md) · [README](../README.md)

Everything is in [`config/config.yaml`](../config/config.yaml) and reads environment variables with `${VAR:-default}`. [`.env.example`](../.env.example) lists them:

| Variable | Default | Description |
|---|---|---|
| `SERVER_HOST` / `SERVER_PORT` | `0.0.0.0` / `5055` | Bind address. |
| `MCP_GATE_URL` | `http://localhost:4568` | The MCP Gate that issues tokens (`auth.issuer`). |
| `MORPHYARD_BASE_URL` | `http://127.0.0.1:5055` | The address this server is reached at. The token audience is `<MORPHYARD_BASE_URL>/mcp` and must equal the service registered in MCP Gate; it also appears in the protected-resource metadata. |
| `AUTH_ENABLED` | `true` | Set to `false` only for local experiments. |
| `MAX_INPUT_MB` | `50` | Per-file limit; also raises the MCP transport body limit (×1.5). |
| `ENGINE_TIMEOUT` | engine default | Seconds per engine call, applied to every engine. Unset keeps each engine's own limit: LibreOffice 180, Pandoc 120, OCR 300. |
| `MORPHYARD_MAX_CONCURRENT_CONVERSIONS` | CPUs, at most 8 | Conversions running at once, all engines together. See [Concurrent conversions](#concurrent-conversions). |
| `MORPHYARD_MAX_QUEUED_CONVERSIONS` | 4 × the limit | How many more wait for a slot; `0` refuses at once when every slot is taken. |
| `MORPHYARD_QUEUE_TIMEOUT` | `30` | Seconds a request may wait for a slot before it is refused as busy. MCP tool calls run as jobs and are not bound by it (see [Jobs](#jobs)). |
| `MORPHYARD_DATA_DIR` | `./data` | Stored files and the generated link-signing key. See [Stored files and download links](#stored-files-and-download-links). |
| `MORPHYARD_PUBLIC_URL` | `MORPHYARD_BASE_URL` | Address download links point at. |
| `MORPHYARD_SIGNING_KEY` | generated | Key that signs download links. |
| `MORPHYARD_FILE_TTL_SEC` | `3600` | Seconds a stored file is kept (at most 86400). |
| `MORPHYARD_STORE_MAX_MB` / `MORPHYARD_STORE_OWNER_MAX_MB` | `2048` / `512` | Store caps: whole store, per person. |
| `MORPHYARD_RESULT_DELIVERY` / `MORPHYARD_INLINE_MAX_MB` | `auto` / `10` | How tools return files. See [Delivery modes](#delivery-modes). |
| `MORPHYARD_JOB_WAIT_SEC` | `20` | Seconds a call waits for its conversion before answering with a job id; also the longest `job_status` wait (at most 120). See [Jobs](#jobs). |
| `MORPHYARD_MAX_JOBS_PER_OWNER` / `MORPHYARD_MAX_JOBS` | `5` / `64` | Jobs queued or running at once: per person, and on the server. |
| `MORPHYARD_JOB_MAX_SEC` | `1800` | A job is stopped after this many seconds, its wait in the queue included. |
| `LOG_LEVEL` | `INFO` | |

In the YAML, `auth.required_scopes` lets you demand a scope such as `mcp:tools:invoke` on every token, and `auth.audience` overrides the derived audience for the rare case where the registered resource URI is not `<base_url>/mcp`.

## Concurrent conversions

Each conversion takes one slot for its whole run (every hop of a multi-hop path included), whichever engine does the work: LibreOffice, Pandoc, Tesseract, Poppler or the built-in ones. There is one limit for all engines, not one each, because they all compete for the same CPU and memory and a request uses its engines one after another; a slot is at most one engine process at a time. `list_supported_conversions` and `inspect_document` take no slot.

A request that finds every slot taken waits in a first-in, first-out queue. It is refused straight away when `MORPHYARD_MAX_QUEUED_CONVERSIONS` requests are already waiting. MCP tool calls run as [jobs](#jobs) and wait in the queue as long as they need (up to `MORPHYARD_JOB_MAX_SEC`); `MORPHYARD_QUEUE_TIMEOUT` applies only to other callers of the conversion service. The refusal is an MCP tool error that says the server is busy, that nothing was started and to retry the same call after a few seconds (over HTTP it would be `503` with a `Retry-After` header). `GET /health` shows the current state under `conversions`: `running`, `queued` and the three limits.

The default limit is the number of CPUs the process may use, capped at 8 because LibreOffice and Tesseract need a few hundred MB each and memory usually runs out before CPUs do. Raise it on a large machine with memory to spare; lower it on a small one. Each waiting request holds its upload in memory (up to `MAX_INPUT_MB`), which is what the queue length bounds. The limit applies per server process: with several workers or replicas, each has its own.

## Jobs

Every tool that converts (all but `list_supported_conversions`, `inspect_document` and `pdf_metadata`) runs its work as a **job**. The call waits up to `MORPHYARD_JOB_WAIT_SEC` (20 s) for it:

- **Done in time:** the reply is exactly the one the tool always gave, and no job is kept. Most conversions take a few seconds, so most hosts never see a job.
- **Still running:** the reply is one text block with the job id and how to poll, for example
  `Still working on convert_document (job_id j_…): running for 20.1 s. Call job_status with job_id "j_…" to get the result; …`.
  The conversion keeps going on the server.

Three tools reach a job:

| Tool | What it does |
|---|---|
| `job_status(job_id, wait_sec=20)` | Waits until the job is done (at most `MORPHYARD_JOB_WAIT_SEC`) and returns what the original tool would have returned: the file(s) with their links under the [delivery mode](#delivery-modes), the text, or the same error. Still running → the same handle again. It can be called again after the job is done. |
| `job_cancel(job_id)` | Stops a queued or running job: its engine processes (LibreOffice and the processes it starts, Pandoc, Tesseract, Poppler) are killed, its conversion slot goes to the next job in the queue, and nothing is kept. |
| `job_list()` | Your jobs, newest first: id, tool, status (`queued`, `running`, `completed`, `failed`, `cancelled`), seconds, queue position, last progress message. |

Why 20 s: clients give up on long calls (Claude Code moves a call to the background after about 120 s and then drops its progress; Cursor's remote calls fail after about 300 s), and a call that answers in 20 s stays under all of them. Progress is sent as MCP `notifications/progress` when the request carries a `_meta.progressToken` (queue position, start, the engine's steps), both on the original call and on `job_status`, but nothing depends on it: few clients show it.

Rules:

- **A job belongs to the person who started it** (the token's `sub`; without authentication everyone is the same person). Another person's job id gets the same "unknown job" answer as an id that never existed, from all three tools.
- **Caps:** `MORPHYARD_MAX_JOBS_PER_OWNER` (5) jobs queued or running per person, `MORPHYARD_MAX_JOBS` (64) on the server; over either, the call is refused at once and nothing starts. With authentication off everyone shares the per-person cap. The conversion queue's own limit still applies: a job that would be number `MORPHYARD_MAX_QUEUED_CONVERSIONS` + 1 in line is refused as busy.
- **Time limit:** a job is stopped after `MORPHYARD_JOB_MAX_SEC` (30 min), queue wait included.
- **Kept for the file TTL:** a finished job is kept, with its result, as long as files are (`MORPHYARD_FILE_TTL_SEC`). A job that ends after its caller left keeps its result files in the [store](#stored-files-and-download-links) under every delivery mode, `inline` too (there is nowhere else to keep them); `job_status` then returns them as the mode says.
- **Jobs live in memory.** A restart loses every job: running ones stop (their engines die with the server) and `job_status` answers "unknown" for all ids. Files already kept stay until they expire, and their links keep working. Run the tool again after a restart.
- **A cancelled request:** if the MCP client cancels a call before it answered with a job id, the job is cancelled too (nobody holds its id). Once the id was handed out, only `job_cancel` stops it.

`GET /health` shows `jobs`: active, queued, kept and the limits.

## Stored files and download links

Morphyard keeps every result file on the server for a short time and hands out a signed download link to it, so a large result does not have to travel through the model's context as base64, and the next tool can take it by `file_id` instead of the bytes.

A link looks like `<MORPHYARD_PUBLIC_URL>/files/<file id>/<name>?exp=<unix time>&sig=<signature>`. **Anyone holding it can download the file until it expires**, the way S3 pre-signed URLs work: only the owner's tool call is given the link, which is where the owner check happens. A link works until its `exp` or until the file is deleted, whichever comes first. A wrong, expired or tampered link and a file that is gone all get the same `404`, so a link tells nobody whether a file ever existed. Downloads are served as attachments with `X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`, `Referrer-Policy: no-referrer` and `Content-Security-Policy: sandbox`, so an HTML result never runs as a page on this server. The query string (the signature) is never written to the logs.

| Variable / setting | Default | Description |
|---|---|---|
| `MORPHYARD_DATA_DIR` (`files.data_dir`) | `./data` | The store lives in `<data_dir>/store`, the generated key in `<data_dir>/.signing_key`. The Docker image declares a volume at `/app/data`. |
| `MORPHYARD_PUBLIC_URL` (`files.public_url`) | `MORPHYARD_BASE_URL` | The address people's browsers and agents reach this server at. Set it when the MCP address is internal (behind a reverse proxy, or a LAN name). Startup warns when it is a loopback address while the server listens on all interfaces. |
| `MORPHYARD_SIGNING_KEY` (`files.signing_key`) | generated | At least 32 characters (`openssl rand -hex 32`). Unset: 32 random bytes are generated once into `<data_dir>/.signing_key` (mode 600). In the YAML it must stay an `${ENV_VAR}` reference; a literal value refuses to start. A key file that cannot be read, or is too short, also refuses to start — it is never replaced silently. |
| `MORPHYARD_FILE_TTL_SEC` (`files.ttl_sec`) | `3600` | How long a file is kept, at most 86400 (24 h). A tool may ask for less, never more. |
| `MORPHYARD_STORE_MAX_MB` (`files.max_total_mb`) | `2048` | The whole store. |
| `MORPHYARD_STORE_OWNER_MAX_MB` (`files.max_per_owner_mb`) | `512` | Per person (the token's `sub`; everyone is one person when authentication is off). |
| `files.sweep_interval_sec` | `60` | Expired files are deleted this often, and at startup. |
| `MORPHYARD_RESULT_DELIVERY` (`files.result_delivery`) | `auto` | How tools return files: `auto`, `inline`, `both` or `link`. See [Delivery modes](#delivery-modes). |
| `MORPHYARD_INLINE_MAX_MB` (`files.inline_max_mb`) | `10` | The `auto` threshold. |

**When the store is full** a new file is refused with a `storage_full` error that says when to retry; expired files are swept first, and a file that has not expired is never deleted to make room, so a link that was handed out keeps working until it says it expires. Each file sits in `<store>/<sha256 of the owner>/<file id>/` (no raw identity on disk) and is written under a temporary name, then renamed, so a crash never leaves half a file behind.

**Restarts** keep unexpired files and their links. Keep the key (`.signing_key` or `MORPHYARD_SIGNING_KEY`) together with the store: if the key is lost or changes while files are stored, their links can no longer be checked, so the files are deleted at startup with a warning. Losing the store with the key kept only loses the files. The index of stored files is held in memory, so one store belongs to one server process.

`GET /health` shows the store under `store`: `files`, `bytes`, `owners` and the caps — never file names or ids.

### Delivery modes

A tool that produces files puts each one in the store (owned by the caller: the token's `sub`, or `anonymous` when authentication is off) and replies with:

1. **Text:** the tool's summary, then one line per file with its name, size, `file_id` and download URL, and when the links expire. The URL is in the text because some clients (Claude Code among them) ignore `resource_link`.
2. **The file inline** as an MCP embedded resource (base64), exactly the block 1.3 sent — depending on the mode.
3. **A `resource_link`** per file: `uri` (the signed URL), `name`, `mimeType`, `size`, and a description with the expiry and `file_id`.

| Mode | Inline file | Link + `file_id` | Use it when |
|---|---|---|---|
| `auto` (default) | when the file is at most `MORPHYARD_INLINE_MAX_MB` (10 MB) | always | Hosts that turn an inline file into an artifact (a download in the chat) keep working for everyday results, and a large result no longer floods the model's context: it comes back as a link only, and the text says it is not attached. |
| `inline` | always | never (nothing is stored) | Exactly the 1.3 behaviour, for a host that cannot use links at all. Results are not stored, so they cannot be passed on by `file_id`; every result travels through the context, however large. |
| `both` | always | always | A host that needs the inline file and also wants links. Large results cost as much context as in 1.3. |
| `link` | never | always | Clients that read links (or a browser user): the smallest replies. A host that only understands inline files shows no download. |

`MORPHYARD_INLINE_MAX_MB` is per file: a `pdf_split` result can have its small parts inline and its large parts as links. A base64 file costs about 1.33 × its size in the reply, so the threshold is a trade-off between hosts that need the file inline and the model's context.

Answers without a file (`inspect_document`, `extract_text`, `pdf_metadata`, `list_supported_conversions`, or "this PDF is not encrypted") are unchanged in every mode.

If the store has no room for a result, the tool answers with an error that names the limit and when to retry, and nothing of that result is stored (a `pdf_split` is stored whole or not at all). A result larger than the per-person or total cap is refused as one that can never fit.

### Chaining tools with file_id

Every tool that takes a file also takes `file_id` in place of `file_content` (and `pdf_merge` items take `{file_id}` in place of `{file_content, file_name}`). The file is read from the store, so a multi-step job never sends a result back through the model:

```python
r1 = await client.call_tool("convert_document", {"file_content": md, "file_name": "report.md",
                                                 "target_format": "pdf", "output_name": "report"})
# r1's text: "- report.pdf (20.8 KB, file_id f_o2UZ…): http://…/files/f_o2UZ…/report.pdf?exp=…&sig=…"
r2 = await client.call_tool("pdf_watermark", {"text": "DRAFT", "file_id": "f_o2UZ…"})
r3 = await client.call_tool("pdf_compress", {"file_id": "<file_id from r2>"})
```

- Give `file_content` **or** `file_id`, not both. An empty `file_id` counts as not given (a host that injects an upload blanks the parameter the reference was in).
- A `file_id` works only for the person whose call produced it, until it expires. An id that is unknown, expired or someone else's gets the same answer, so it never tells whether a file exists. With authentication off, everyone is the same person (`anonymous`).
- `file_name` / `mime_type` may still be given with a `file_id`; otherwise the stored name and type are used. The usual 50 MB input limit applies.

## Token revocation

Morphyard verifies MCP Gate tokens offline, so by default a token revoked in MCP Gate keeps working here until it expires — and a personal access token can live up to a year. Turn on the **revocation feed** and Morphyard polls MCP Gate's `POST /oauth/revoked` in the background (every `poll_interval_sec`, with jitter and error backoff), keeps the revoked-but-unexpired `jti`s in memory, and answers a listed token on `/mcp` with 401 `invalid_token`. A revocation takes effect within about one poll interval.

| Variable / setting | Default | Description |
|---|---|---|
| `MORPHYARD_REVOCATION_ENABLED` (`auth.revocation.enabled`) | `false` | Turn the feed on. Off = nothing is polled. |
| `MORPHYARD_REVOCATION_CLIENT_ID` | — | The resource-server client registered in MCP Gate (below). |
| `MORPHYARD_REVOCATION_CLIENT_SECRET` | — | Its secret. In the YAML it **must** stay an `${ENV_VAR}` reference; a literal value refuses to start. |
| `MORPHYARD_REVOCATION_POLL_INTERVAL` (`poll_interval_sec`) | `30` | Seconds between polls (5–3600). |
| `MORPHYARD_REVOCATION_FAIL_MODE` (`fail_mode`) | `open` | When MCP Gate is unreachable: `open` keeps accepting tokens and logs loudly; `closed` rejects every token once no poll has succeeded for `stale_after_intervals` × `poll_interval_sec` (YAML, default 4), and until the first poll after startup succeeds. |

The first poll fetches the whole list; later polls ask only for revocations since the previous one (with a 30 s overlap), and a full fetch repeats every hour. Entries are dropped once the token would have expired anyway. Startup logs whether the feed is enabled, and `GET /health` reports it under `revocation` (`last_success`, `revoked_count`, `consecutive_failures`, `last_error`, `stale`); a stale feed turns `status` into `degraded`.

**Setting it up** (once per deployment):

1. **Register a confidential client in MCP Gate.** Console → *OAuth Clients* → *Register trusted client*. Name it e.g. `Morphyard revocation feed`; leave *Redirect URIs* empty; under *Grant types* select only `client_credentials`; *Token endpoint authentication*: **Client secret (POST body)** (HTTP Basic works too — MCP Gate accepts either); *Resource server for*: **Morphyard**. Register, then copy the `client_id` (`mcpc_…`) and the client secret — the secret is shown once.
2. **Bind it to Morphyard** — already done if you picked *Resource server for* in step 1. Otherwise, on the client's row click the server icon (*Resource server…*), choose the Morphyard service and *Save*; or call `PUT /api/oauth/clients/<client_id>/resource-server` with `{"service_id": "<Morphyard service id>"}` as a console admin. The row then reads *Resource server for Morphyard*. The feed lists only tokens whose audience is that service's audience, so it must equal `<MORPHYARD_BASE_URL>/mcp`.
3. **Give Morphyard the credentials** in its environment (or `.env`), then restart:
   ```bash
   MORPHYARD_REVOCATION_ENABLED=true
   MORPHYARD_REVOCATION_CLIENT_ID=mcpc_...
   MORPHYARD_REVOCATION_CLIENT_SECRET=...
   ```
4. **Check it.** The startup log says `MCP Gate revocation feed: ENABLED …`; `curl -s localhost:5055/health` shows a recent `revocation.last_success`. Revoke a test token in MCP Gate; within `poll_interval_sec` a call to `/mcp` with it gets 401. `HTTP 401: invalid_client` in `last_error` means wrong credentials; `HTTP 403: unauthorized_client` means the client is not bound to a server.

## Renamed variables

Morphyard was called AnyDoc before 1.2.2, and MCP Gate was called MCP Center. The old variable names are no longer read, so rename them in your environment or `.env` when you upgrade:

| Before 1.2.2 | Now |
|---|---|
| `MCP_CENTER_URL` | `MCP_GATE_URL` |
| `ANYDOC_BASE_URL` | `MORPHYARD_BASE_URL` |
| `ANYDOC_REVOCATION_ENABLED` | `MORPHYARD_REVOCATION_ENABLED` |
| `ANYDOC_REVOCATION_CLIENT_ID` | `MORPHYARD_REVOCATION_CLIENT_ID` |
| `ANYDOC_REVOCATION_CLIENT_SECRET` | `MORPHYARD_REVOCATION_CLIENT_SECRET` |
| `ANYDOC_REVOCATION_POLL_INTERVAL` | `MORPHYARD_REVOCATION_POLL_INTERVAL` |
| `ANYDOC_REVOCATION_FAIL_MODE` | `MORPHYARD_REVOCATION_FAIL_MODE` |
