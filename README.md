# Telegram Events for Vale

A self-hosted, owner-only Telegram transport for the **same Vale dot that subscribes to this plugin**. It receives private Telegram text, emits `telegram.message` events, and exposes narrowly scoped tools to read the received message and reply in that owner's chat. Python 3.11+. Transport and persistence use the standard library; OAuth token verification uses pinned `PyJWT[crypto]` from PyPI.

There is **no model, OpenAI API key, inference call, prompt, conversation memory, or separate assistant** in this service. Vale's identity, memory, connected tools, and decisions remain with the subscribed dot. The plugin does not itself grant access to that dot or its tools. Until a compatible client installs it and creates a working event subscription, it cannot wake Vale or generate replies.

**Status: live installation and end-to-end delivery are UNVERIFIED.** The default production mode is an OAuth resource server; deployment requires a separately configured OAuth 2.1 identity provider. Offline tests establish local behavior, not compatibility with the user's live dot account. This repository does not invent a private dot ingress URL or claim that ordinary ChatGPT MCP tool support includes Events subscriptions.

## Data flow and API

1. A dedicated Telegram bot long-polls the official Telegram Bot API.
2. Only new text messages from the configured numeric owner in that same private chat are accepted. Other users, groups, channels, edited messages, and non-text updates cannot become owner events.
3. The authenticated MCP client subscribes to `telegram.message`; the server delivers event notifications to its validated HTTPS callback.
4. The subscribed dot decides what to do. `read_message(message_id)` retrieves an accepted message; `reply(message_id, text, idempotency_key)` sends a plain-text reply to that message in the fixed owner's chat.

`POST /mcp` requires an authorized bearer access token. Production `AUTH_MODE=oauth` validates tokens from the configured external issuer; `AUTH_MODE=bearer` is only for explicit local development. The implementation targets MCP protocol version `2026-07-28` with discovery, Events, and tools. Requests must carry MCP 2 protocol/client-capability metadata and matching `MCP-Protocol-Version` and `Mcp-Method` headers; tool calls also require matching `Mcp-Name`. Legacy initialize-only clients are not interchangeable. Inspect discovery/tool schemas for the precise wire contract. Events are **non-replayable** and use `cursor: null`; a durable local pending-delivery queue is not an event-history/replay API.

Tool scope is intentionally small:

- `read_message`: integer ID of an accepted owner message.
- `reply`: integer message ID, nonempty text of at most 4,096 characters, and an idempotency key. Telegram's own text limits also apply. No arbitrary chat ID, recipient, file, command, URL-fetch, or shell tool.

No `/reset` or local conversation-history command exists. Text sent to the bot is input for the subscribed dot, subject to that dot's permissions and policies.

## Configuration

Use a dedicated bot created with official [@BotFather](https://t.me/BotFather). Stop other pollers first. An existing Telegram webhook conflicts with long polling; inspect and deliberately resolve it rather than assuming the service will remove it.

```sh
cp .env.example .env
chmod 600 .env
# Edit .env locally. Never paste credentials into chat, commit them, or put them in a URL.
```

Configure the OAuth issuer, audience, JWKS URL, and owner subject below. The application loads environment variables, not `.env` automatically. Docker Compose loads the file for you; for direct execution load only your own trusted file:

```sh
set -a
. ./.env
set +a
```

Required variables:

- `TELEGRAM_BOT_TOKEN`: the dedicated bot's secret token.
- `TELEGRAM_USER_ID`: verified positive numeric owner ID.
- `TELEGRAM_CHAT_ID`: verified positive numeric private chat ID, equal to the owner ID. A username or display name is insufficient.
- `CALLBACK_ALLOWED_HOSTS`: exact comma-separated HTTPS callback hostnames supplied and verified by the intended MCP client. No wildcard, guessed host, URL scheme, port, or path. Do not put arbitrary domains here to make a failed subscription pass.
- `AUTH_MODE`: production default `oauth`. Explicit `bearer` is for local development only.
- `OAUTH_ISSUER`: exact HTTPS issuer from your OAuth provider's metadata.
- `OAUTH_AUDIENCE`: exact canonical resource URL, such as `https://your-domain.example/mcp`; the issuer must mint tokens for this audience.
- `OAUTH_JWKS_URL`: verified HTTPS signing-key endpoint of that issuer.
- `OAUTH_SUBJECT`: exact verified `sub` claim for the one authorized owner. All other subjects are rejected.

For explicit `AUTH_MODE=bearer` local testing, the OAuth variables are unused and `MCP_AUTH_TOKEN` is required: generate a random secret of at least 32 characters with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`. ChatGPT cannot present custom API keys, so this development mode is not a production installation shortcut.

Optional variables:

- `STATE_PATH`: SQLite path, default `/data/events.sqlite3`.
- `BIND_HOST`: default `127.0.0.1`.
- `PORT`: default `8765`.

Verify owner IDs from your bot's actual private-chat updates using the official [Telegram Bot API](https://core.telegram.org/bots/api#getupdates), while the worker is stopped. Inspect locally; bot tokens in request URLs and full responses are sensitive. Avoid third-party ID bots. Do not advance polling offsets or discard queued updates merely to discover an ID.

For a native run, install dependencies from PyPI into a virtual environment and choose a private writable state directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
mkdir -p data
chmod 700 data
export STATE_PATH="$PWD/data/events.sqlite3"
python3 -m telegram_events
```

## Deploy privately or with HTTPS

Run one instance per bot token and one process per state database. Keep the HTTP listener private. Choose either Secure MCP Tunnel (below) or a trusted HTTPS reverse proxy. A public server URL needs valid HTTPS; the tunnel route can reach the local listener without publishing it. Localhost alone, without a configured tunnel, is not a remote installation URL.

### Docker Compose

```sh
docker compose up --build -d
docker compose logs -f
docker compose down
```

The container runs as UID/GID 10001 with a read-only root filesystem and persistent state volume. Compose binds the published port only to **127.0.0.1:8765** on the host; binding `0.0.0.0` inside the isolated container is needed for that port mapping. Do not publish it on every host interface.

For example, a separately installed, trusted Caddy reverse proxy on the host can use:

```caddyfile
telegram-events.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

Replace the example domain with a domain you control, point DNS at the host, and configure firewall/TLS access deliberately. This is an illustrative configuration, not a deployed endpoint. Preserve the `Authorization` header, disable request-body/credential logging, apply reasonable connection/request limits, and expose no other internal services. Do not remove MCP authentication just because the reverse proxy has TLS. If the proxy runs in another container, its loopback is different; configure a private container network deliberately instead of exposing the app to the internet.

### systemd alternative

Install Python 3.11+ and the package/dependencies in `/opt/dots-telegram-assistant/.venv`, create the unprivileged `telegram-events` system user/group, and install reviewed source in `/opt/dots-telegram-assistant`, owned by an administrator and readable by that user. Save configuration in `/etc/dots-telegram-events.env` (root-owned, mode `0600`). Inspect and copy `deploy/dots-telegram-assistant.service` to `/etc/systemd/system/dots-telegram-events.service`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now dots-telegram-events
sudo journalctl -u dots-telegram-events -f
```

The unit uses `/var/lib/dots-telegram-events/events.sqlite3`, binds to loopback, and can use either Secure MCP Tunnel or the HTTPS reverse proxy. Use Docker **or** systemd for one bot, never both. Native Windows is not a supported deployment target; use Linux/WSL or Docker.

### Secure MCP Tunnel alternative

OpenAI's [custom-server guide](https://developers.openai.com/api/docs/guides/custom-mcp-server) supports choosing **Tunnel** instead of a public Server URL. This avoids publishing this service's HTTP listener. It does not publish the plugin publicly, establish Vale's identity automatically, or remove account/workspace restrictions.

Follow the official [tunnel-client configuration reference](https://github.com/openai/tunnel-client/blob/master/docs/configuration.md) for setup:

- Obtain an authorized tunnel ID and runtime API key. Runtime/connector users need Tunnels Read + Use; creating/managing tunnels needs separate permissions. Keep the runtime key separate from any admin key and out of source control, chat, and command arguments.
- Run the tunnel client on a host that can reach `http://127.0.0.1:8765/mcp`; configure that address as its main MCP server binding. Compose already publishes this address only on the host loopback. A client in another container needs deliberate private networking.
- Use the official OAuth-aware profile guidance and its `doctor` checks. Configure any separate OAuth origin trust explicitly. Keep `AUTH_MODE=oauth`; the tunnel runtime key authenticates the tunnel connection, not the Telegram owner's OAuth identity.

In ChatGPT's custom MCP server dialog, select **Tunnel**, choose the verified tunnel or enter its returned ID, configure OAuth, and review the discovered tools. Do not put the tunnel runtime key in this service's `MCP_AUTH_TOKEN` field.

This server still requires its configured HTTPS resource audience, issuer metadata, owner subject, and scopes. Verify their discovery/routing and token audience with the tunnel's OAuth behavior before relying on it; do not change the audience to a loopback HTTP URL to bypass validation. Event delivery remains **outbound HTTPS** from this service to the client's verified callback host, and the host also needs outbound Telegram/JWKS connectivity. The tunnel does not replace the callback allowlist or subscription renewal.

No tunnel has been installed, registered, or started by this repository. Tunnel/OAuth/Event interoperability with the live dot remains **UNVERIFIED** and must pass the same end-to-end checklist below.

## Connect the existing dot

Deployment and plugin installation are distinct. Follow the official [connect-and-test guide](https://developers.openai.com/plugins/deploy/connect-chatgpt) and [authentication guide](https://developers.openai.com/plugins/build/auth), checked 2026-10-13. Authenticated ChatGPT plugins use OAuth 2.1; custom API keys are not supported.

This service is the **resource server**, not an authorization server. Use an established OAuth provider with authorization-code + PKCE, supported client identification/registration, authorization metadata, and refresh-token support appropriate for long-lived access. Configure that provider to issue RS256 JWT access tokens with the exact issuer, resource audience, and owner subject above, and scopes `telegram:read` and `telegram:reply`. Reading, Events, and discovery require the read scope; replies require the reply scope. Public protected-resource metadata is available at `/.well-known/oauth-protected-resource/mcp` (with a root alias). Its advertised issuer must have working authorization-server metadata.

The external provider must support the client's OAuth registration and resource parameter end to end. That setup, issuer policy, user consent, token issuance/refresh, and real ChatGPT connection are **operator prerequisites and remain unverified**. Configuring four environment variables alone does not create an OAuth provider. Do not make the service anonymous to get past a connection failure or replace OAuth with its development bearer gate.

For the public-endpoint workflow: open ChatGPT Plugins, choose the plus button and **Add custom MCP server**, enter a name/description and the HTTPS `/mcp` URL, configure OAuth, review the risk warning, choose **Create as a plugin**, and inspect discovered tools. Account/workspace policy may restrict this; recheck the official guide when deploying. Obtain verified client callback hostnames for `CALLBACK_ALLOWED_HOSTS`. Never guess an internal ingress address, put secrets in endpoint URLs, or enter the Telegram bot token in the installer.

After installation, subscribe the intended existing Vale dot to `telegram.message`, authorize the intended Telegram use in that dot, and verify all of these before treating the connection as live:

1. Unauthenticated `/mcp` requests fail; authenticated discovery succeeds through the selected HTTPS endpoint or authorized tunnel.
2. OAuth consent/token refresh and event subscription succeed with the verified issuer, owner identity, scopes, and callback host.
3. A fresh text sent by the owner in the private bot chat produces one usable event.
4. The same Vale dot can read that message and explicitly reply to its message ID.
5. A different Telegram user and a group message produce no owner event or reply.
6. Retrying a completed reply with the same idempotency key does not send a duplicate.
7. Restart the transport; verify durable state and no duplicate sends.

A successful local HTTP request or mock callback test does not prove any of steps 2–4 against a live dot.

## Reliability and privacy

- SQLite holds accepted message content, polling offset, deduplication records, subscriptions, pending event deliveries, and reply outcomes. Treat the database, WAL files, volume, and backups as sensitive, including any callback secrets. Restrict permissions, use encrypted storage/backups where appropriate, and never attach state to bug reports.
- Accepted intake and polling progress are durable. Pending callback delivery is limited to eight attempts and a 24-hour expiry. Expired/exhausted deliveries are not silently converted to new events. Non-replayable events cannot be recovered through a replay cursor; retain and reconcile failures deliberately.
- At most one active event subscription is supported. Subscription grants are finite and capped at 24 hours and, in OAuth mode, at the access token expiry, including requests with `ttlMs: null`. Pending deliveries stop at subscription expiry; renewal requires a valid token for the configured owner and scope. JWT revocation at the issuer is not observed instantly without introspection, which is not implemented here; use short-lived access tokens. The client must renew before expiry. Callback signing-secret rotation allows a 300-second overlap.
- Telegram and network delivery are not exactly-once. A callback may have been accepted before an acknowledgement was lost, so the client must deduplicate event IDs. A failed or ambiguous **Telegram reply is never automatically resent**, including after restart. Inspect Telegram and the returned status before choosing any new send. A new idempotency key is a new action and can duplicate an earlier ambiguous send.
- Telegram retains pending updates for a limited period (the official API currently documents no more than 24 hours). The service cannot recover expired updates or messages missed before subscription from Telegram history.
- State has no automatic retention/pruning policy; monitor disk use. Do not casually delete deduplication or idempotency receipts to reclaim space. Plan a deliberate stopped-service archival/migration with reconciliation before resuming.
- Back up while stopped or with SQLite's backup API; handle WAL consistently. Do not run `docker compose down -v` unless intentionally deleting durable state. A blank/replaced database loses deduplication and send receipts, and is not a safe retry mechanism.
- Changing the owner is not a supported in-place multi-user migration. Stop the deployment, review state handling, and configure a separate dedicated instance rather than carrying one owner's state into another owner's service.
- Callback verification runs for every subscribe/refresh, including repeated requests, rather than caching a successful challenge. This is a conservative implementation deviation from the guide's recommended bounded verification cache.
- Callback hosts are an explicit trust boundary. HTTPS alone does not establish ownership. Keep the list narrow; do not allow internal/private destinations or redirects as a shortcut. Keep secrets out of logs and rotate compromised credentials at the provider.
- Message content crosses Telegram, your host, and the subscribed MCP client's delivery/processing systems. Their retention rules apply. This is not an end-to-end encrypted Telegram channel or a promise of zero retention. Deleting local state does not erase provider data or backups.

## Development

```sh
python3 -m pip install .
python3 -m unittest discover -s tests -v
python3 -m compileall -q telegram_events tests
```

Dependency installation requires package-registry access. The tests themselves are offline and require no real credentials, live bot, identity provider, or model/API access. CI tests Python 3.11, 3.12, and 3.13; GitHub Actions remain pinned to reviewed commit hashes. Live Telegram, reverse-proxy deployment, custom-plugin installation, and dot subscription must be tested separately by the operator.

## Public design reference

The event-source and message-reading separation was informed by Sriram Panyam's public [mcpkit Telegram Events example](https://github.com/panyam/mcpkit/tree/main/examples/events/telegram), an experimental Go implementation under [Apache-2.0](https://github.com/panyam/mcpkit/blob/main/LICENSE). This transport is a new, narrower Python implementation, with no copied upstream source or SDK dependency. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for reviewed file identifiers and attribution. The reference example's demo authentication and wider replay/stream features are not this service's contract.
