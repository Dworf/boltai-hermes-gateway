# BoltAI Hermes Gateway

> **OpenAI-compatible HTTP gateway for [Hermes Agent](https://github.com/NousResearch/hermes-agent) with full markdown rendering and slash-command support — built for [BoltAI v2](https://boltai.com) and any other OpenAI-compatible chat UI that streams Server-Sent Events and renders markdown.**

This is a **standalone Hermes plugin** (not a fork). It installs into Hermes via
the official plugin system, runs on its own port alongside the built-in
`api_server` gateway, and adds two things the stock gateway can't do today:

1. **Full markdown** in streamed responses (headers, bullets, bold, code fences, tables) — preserved verbatim instead of flattened to plain text.
2. **Slash commands** (`/help`, `/status`, `/stop`, `/model …`, `/personality …`, etc.) typed directly into the chat box, dispatched server-side and returned as a normal chat completion.
3. **Inline images.** When the agent emits a local image path (from `image_generate`, vision tool screenshots, browser captures, etc.), the plugin reads the file, base64-encodes it, and rewrites the markdown to a `data:` URL before streaming. BoltAI renders the image inline with no extra hosting step or upload channel.

   
<img width="1158" height="1593" alt="hermes_gateway_boltai_image" src="https://github.com/user-attachments/assets/c2733ed9-a75a-40b6-b8c8-558dcf51922d" />

---

## Why a separate plugin (and not patching the built-in `api_server`)

The official Hermes [`api_server` gateway](https://github.com/NousResearch/hermes-agent/tree/main/gateway/platforms/api_server.py) is excellent for programmatic OpenAI-compatible access (Cursor, Continue, raw `curl`, etc.). But after a recent Hermes update it deliberately:

- **Strips/flattens markdown** in streamed responses — fine for code IDEs, but BoltAI and similar chat clients render markdown beautifully and lose all formatting under the stock gateway.
- **Does not intercept [slash commands](https://hermes-agent.nousresearch.com/docs/reference/slash-commands)** — typing `/help` or `/status` in BoltAI's chat box just sends those literal strings to the LLM. The slash-command UX described in [Hermes' messaging docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/) is only available on Telegram/Discord/Slack/etc. via their respective adapters, not on the OpenAI-compatible HTTP gateway.

Rather than patch the stock gateway and risk breaking IDE clients (or fighting upstream merges every release), this plugin **subclasses** `APIServerAdapter` and runs as an independent gateway on its own port, with its own env namespace and its own settings. The official `api_server` keeps its current behaviour; this gateway adds chat-client-friendly behaviour. Pick whichever you need, run both at once if you like.

## Compatibility

Tested with:

- **[BoltAI v2](https://boltai.com)** (macOS/iOS) — primary target. Setup follows BoltAI's own [Hermes Agent guide](https://help.boltai.com/en/help/articles/9536908-how-to-use-hermes-agent-in-boltai), with one small change documented below.

Should also work with any OpenAI-API-compatible chat client that:

- Sends standard `POST /v1/chat/completions` with `stream: true`
- Renders markdown in streamed responses
- Lets you set a custom Base URL and Bearer token

That includes (untested but expected to work): TypingMind, ChatBox, OpenWebUI, LibreChat, Jan, LobeChat, and most other "bring your own OpenAI endpoint" UIs. **If you use this with another client successfully, please open an issue/PR to add it to the list.**

---

## Install

The plugin is installed via Hermes' standard plugin loader:

```bash
hermes plugins install Dworf/boltai-hermes-gateway --enable
```

This clones the repo into `~/.hermes/plugins/boltai-hermes-gateway/` and enables it. Restart the gateway to pick it up.

To install manually:

```bash
git clone https://github.com/Dworf/boltai-hermes-gateway ~/.hermes/plugins/boltai-hermes-gateway
hermes plugins enable boltai-hermes-gateway
```

## Configure BoltAI

Follow [BoltAI's official Hermes guide](https://help.boltai.com/en/help/articles/9536908-how-to-use-hermes-agent-in-boltai) — but with **one change**: BoltAI's docs point at the built-in `api_server` (port `8642`). To get markdown + slash commands, point it at this plugin's port instead.

In BoltAI: **Settings → AI Providers → Add OpenAI-compatible provider**

| Field | Value |
|---|---|
| **Base URL** | `http://127.0.0.1:8643/v1` &nbsp; *(default — change `8643` if you set `BOLTAI_HERMES_GW_PORT`)* |
| **API Key** | whatever you set in `BOLTAI_HERMES_GW_KEY` |
| **Model** | `hermes-agent` &nbsp; *(or whatever `BOLTAI_HERMES_GW_MODEL_NAME` is set to)* |
| **Streaming** | enabled |

That's the only difference from the BoltAI doc: **port 8643 instead of 8642**.

---

## What it does

- Exposes the same OpenAI-compatible endpoints as the built-in `api_server`: `/v1/chat/completions`, `/v1/responses`, `/v1/models`, `/v1/runs/*`, `/health`.
- Before forwarding a request to the agent loop, peeks at the last user message:
  - If it begins with a known gateway slash command (`/help`, `/status`, `/stop`, `/model …`, `/personality …`, `/reset`, `/usage`, …), the plugin **dispatches the slash directly** and returns the command's output as a regular OpenAI chat-completion response.
  - Otherwise, the request falls through to the agent and runs normally.
- **Auth is enforced first.** Slash dispatch is gated by the same bearer-token check the parent class uses (`Authorization: Bearer …`). No slash bypasses auth.
- **Two streaming modes** for slash responses:
  - `token_stream` *(default)* — chunk the output into ~20-char pieces with small delays so it visually resembles agent streaming. Best for chat UIs.
  - `single_chunk` — emit one SSE delta with the full text + `[DONE]`. Lowest latency; good for programmatic clients.
  - Selected via env, `config.yaml`, or per-request `X-Hermes-Slash-Stream` header (header > config > env > default).
- **Unlimited slash output.** Other gateways truncate `/help` to ~10 entries (with "and 82 more…") and paginate `/commands` 15-20 at a time — sensible for Discord/Telegram limits, painful on a full chat client. This plugin renders the **full unfiltered list** for `/help` and bare `/commands`. `MAX_MESSAGE_LENGTH` is bumped to 10 MB so the chunker never splits a long response on the wire.
- **Image inlining.** OpenAI-compatible chat completions have no server→client file-upload channel — clients can only receive text. To work around this, the plugin scans agent output for local image paths (`MEDIA:<path>`, `![alt](/abs/path.png)`, etc.), reads the file, base64-encodes it, and rewrites the reference to a `data:image/<type>;base64,…` URL before streaming. BoltAI's markdown renderer displays the image inline. Works for `image_generate`, vision/browser screenshots, and any other tool that returns a local file path. Both streaming and non-streaming responses are rewritten. Files larger than 8 MB are skipped (configurable in `image_inliner.py`) to avoid blowing up response sizes. Note that BoltAI strips data URLs from the conversation history it replays on subsequent turns, so this does not eat input tokens on follow-ups.

## Configuration reference

All settings are optional. The plugin auto-enables itself when `BOLTAI_HERMES_GW_ENABLED=true` is set; otherwise enable it via `config.yaml`.

### Environment variables (`~/.hermes/.env`)

| Variable | Default | Description |
|---|---|---|
| `BOLTAI_HERMES_GW_ENABLED` | unset | Set to `true`/`1`/`yes` to auto-enable from env |
| `BOLTAI_HERMES_GW_PORT` | `8643` | TCP port to listen on |
| `BOLTAI_HERMES_GW_HOST` | `127.0.0.1` | Bind host (`0.0.0.0` for LAN access — secure with a strong key!) |
| `BOLTAI_HERMES_GW_KEY` | empty | Bearer token for `Authorization: Bearer …`. Empty = open (local-only OK) |
| `BOLTAI_HERMES_GW_CORS_ORIGINS` | empty | Comma-separated allowed origins, or `*`. Empty disables CORS |
| `BOLTAI_HERMES_GW_MODEL_NAME` | `hermes-agent` | Model name advertised on `/v1/models` |
| `BOLTAI_HERMES_GW_SLASH_STREAM_MODE` | `token_stream` | `token_stream` or `single_chunk` |

Example `~/.hermes/.env`:
```
BOLTAI_HERMES_GW_ENABLED=true
BOLTAI_HERMES_GW_PORT=8643
BOLTAI_HERMES_GW_HOST=127.0.0.1
BOLTAI_HERMES_GW_KEY=replace-with-a-long-random-secret
BOLTAI_HERMES_GW_SLASH_STREAM_MODE=token_stream
```

### `~/.hermes/config.yaml`

```yaml
platforms:
  boltai_hermes_gateway:
    enabled: true
    extra:
      port: 8643
      host: 127.0.0.1
      key: replace-with-a-long-random-secret
      cors_origins: "https://app.boltai.com"
      model_name: hermes-agent
      slash_stream_mode: token_stream   # or single_chunk
```

`config.yaml` values take precedence over env vars when both are set.

### Per-request override

The slash-stream mode can be overridden per HTTP request via header:

```bash
curl -N http://127.0.0.1:8643/v1/chat/completions \
  -H 'Authorization: Bearer replace-with-a-long-random-secret' \
  -H 'X-Hermes-Slash-Stream: single_chunk' \
  -H 'Content-Type: application/json' \
  -d '{"model":"hermes-agent","stream":true,
       "messages":[{"role":"user","content":"/help"}]}'
```

Header values: `single_chunk`, `token_stream`. Anything else is ignored and the configured default is used.

---

## Running

After install + configure:

```bash
hermes gateway run --replace
```

Look for:
```
INFO gateway.platforms.api_server: [Boltai_Hermes_Gateway] API server listening on http://127.0.0.1:8643 (model: hermes-agent)
INFO gateway.run: ✓ boltai_hermes_gateway connected
```

The built-in `api_server` (port 8642) keeps running unchanged — both servers coexist with isolated keys and settings.

## Slash command coverage

Every command in Hermes' `GATEWAY_KNOWN_COMMANDS` set ([`hermes_cli/commands.py`](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/commands.py)) is intercepted. As of writing that includes:

`/new`, `/reset`, `/retry`, `/undo`, `/title`, `/branch`, `/compress`, `/rollback`, `/stop`, `/approve`, `/deny`, `/background`, `/agents`, `/queue`, `/steer`, `/goal`, `/status`, `/profile`, `/sethome`, `/resume`, `/model`, `/personality`, `/footer`, `/yolo`, `/reasoning`, `/fast`, `/voice`, `/curator`, `/kanban`, `/reload-mcp`, `/reload-skills`, `/commands`, `/help`, `/restart`, `/usage`, `/insights`, `/update`, `/debug`, plus all skill commands (`/airtable`, `/apple-notes`, `/spotify`, …).

Unrecognized slashes fall through to the agent loop, same as plain text.

> **Note:** BoltAI does not autocomplete slash commands — type the full command name. The plugin does not change BoltAI's UI; it only makes the slash text *behave* as it does on Telegram/Discord/Slack.

## Tests

```bash
cd ~/.hermes/hermes-agent
source .venv/bin/activate
# Either symlink the test file or copy it into tests/gateway/, then:
pytest tests/gateway/test_boltai_hermes_gateway.py -v
```

The repo ships 38 tests covering: env-namespace isolation (no `API_SERVER_*` fallthrough), auth enforcement before slash dispatch, slash detection edge cases (whitespace, casing, unknown commands), both streaming modes, mid-stream client disconnect cleanup, and non-slash fallthrough to the parent class.

## Repo layout

```
boltai-hermes-gateway/
├── plugin.yaml          # Plugin manifest with optional_env declarations
├── __init__.py          # Re-exports register from adapter
├── adapter.py           # BoltAIGatewayAdapter — subclasses APIServerAdapter
├── image_inliner.py     # Local-image-path → data-URL rewriter (streaming + final)
├── README.md            # This file
├── LICENSE              # Apache-2.0 (matches Hermes upstream)
├── pyproject.toml       # Optional packaging metadata
└── tests/
    └── test_boltai_hermes_gateway.py
```

## Pitfalls / gotchas

1. **Port collision.** If `8643` is already in use, set `BOLTAI_HERMES_GW_PORT` to a free port (and update BoltAI's Base URL to match). The built-in `api_server` on `8642` is untouched.
2. **BoltAI docs say port 8642.** That's the built-in gateway. To get markdown + slash commands, you **must** change the port to whatever this plugin is bound to.
3. **Empty `extra["key"]`.** The gateway's config builder pre-seeds `extra["key"] = ""` for every platform, so the plugin treats empty/missing as "not configured" (not "explicitly empty") and reads `BOLTAI_HERMES_GW_KEY` from env in that case. To run open with no auth, leave the env var unset.
4. **No autocomplete in BoltAI.** Slash commands work when typed in full but BoltAI's UI does not list them. A future BoltAI-aware shim could expose `GET /v1/commands` to drive autocomplete; not implemented today.
5. **Per-user allowlists are bypassed.** Slash events synthesized from API requests are marked `internal=True` so they bypass per-platform user allowlists (e.g. Telegram's `ALLOWED_USERS`). HTTP-layer bearer-token auth is the only access control on this server — set a strong `BOLTAI_HERMES_GW_KEY` if exposing beyond `127.0.0.1`.
6. **Streaming finalization.** Mid-stream client disconnects are caught in a try/finally so `write_eof()` always runs. `ConnectionResetError` at INFO level when a BoltAI tab closes mid-stream is expected.
7. **Network exposure.** Setting `BOLTAI_HERMES_GW_HOST=0.0.0.0` exposes the gateway on your LAN. Use a strong `BOLTAI_HERMES_GW_KEY` and consider a firewall rule. There is no rate limiting in this plugin.

## Contributing

Issues and PRs welcome. If you build on top of this for another OpenAI-compatible client, please:

1. Open an issue describing the client + any quirks you hit.
2. If you patched something, send a PR — keep it focused and ideally add a test.

## License

Apache-2.0 — same as upstream [Hermes Agent](https://github.com/NousResearch/hermes-agent). See [`LICENSE`](./LICENSE).

## Acknowledgements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research — the agent core this plugs into.
- [BoltAI](https://boltai.com) — the chat client that motivated the plugin.
