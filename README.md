# dots-telegram-assistant

A small, self-hosted **separate assistant**: private Telegram messages → OpenAI Responses API → plain-text Telegram replies. Python 3.11+ on Linux or macOS; Docker is supported. Standard-library runtime, no pip dependencies.

This is not a bridge to a ChatGPT conversation, subscription, identity, memory, account, or connected tools. It has its **own bounded local conversation history**. OpenAI API access and billing are separate from a ChatGPT subscription. Supply your own bot token and API key. No credentials or user data belong in this repository.

## What it does

- Allows only explicitly configured **numeric sender IDs AND numeric private chat IDs**. Sender must equal the private chat ID. Groups, channels, other users, bots, and edited messages are ignored.
- Handles text and `/start`, `/help`, `/reset`. Non-text messages get a text-only explanation. Unknown slash commands never reach the model.
- Persists intake, polling offset, conversation turns, model attempts, daily call counts, generated replies, and sent chunk progress in SQLite.
- Caps input/context length, history turns, output tokens, retry attempts, and global daily API attempts.
- Sends plain text with link previews disabled; splits at a conservative 4,000 UTF-16-unit boundary, including emoji.
- Has **no shell, filesystem, browsing, external tools, scheduling, or autonomous actions**. Model text cannot execute anything.

## Local setup

1. Create a Telegram bot using the official [@BotFather](https://t.me/BotFather). Keep its token private. Use a dedicated bot with no other poller and no webhook. This service will not silently remove an existing webhook.
2. Create an API key in your [OpenAI project](https://platform.openai.com/api-keys), enable billing as needed, and check the model is available to your project. The example model `gpt-4.1-mini` is documented in the [model catalog](https://developers.openai.com/api/docs/models/gpt-4.1-mini); this is not a promise of access or current pricing. Change `OPENAI_MODEL` for your project.
3. Clone this repository, then create your private configuration:

   ```sh
   cp .env.example .env
   chmod 600 .env
   # Edit .env with your local editor. Never paste credentials into a chat or commit them.
   ```

   Set the bot token and OpenAI key. The application reads environment variables, **not `.env` automatically**. Load only your own trusted configuration in your shell:

   ```sh
   set -a
   . ./.env
   set +a
   ```

4. Verify your numeric IDs without a third-party ID bot:

   ```sh
   python3 -m telegram_assistant.identify
   ```

   With the worker stopped, send the random `/pair …` code printed by this helper to your bot **in a private chat**. The helper prints the exact sender and chat IDs from that matching update. Put both values in `.env`, then load it again. It never edits your allowlist, sends messages, or acknowledges/discards pending updates. Keep the pairing code local. The helper may time out if more than 100 older updates obscure the matching message: do not discard them blindly; use a new dedicated bot, or inspect and explicitly resolve the old backlog yourself. The initial `/pair` message will later receive “Unknown command”; this is harmless.
5. Run:

   ```sh
   python3 -m telegram_assistant
   ```

   Send `/start`, a short question, then a follow-up. Restart the process and verify a follow-up still has context. `/reset` starts a fresh local conversation. Missing/invalid secrets or an empty allowlist prevent startup.

Multiple trusted users can be listed as comma-separated IDs in **both** allowlists. Conversations are isolated by user and private chat. Anyone you allow can consume the shared API quota. Do not authorize a username or a display name. Removing an ID also prevents already-queued jobs for that ID from being sent after restart.

## Limits and spending

Defaults are in `.env.example`. `MAX_DAILY_CALLS=50` is a **global API-attempt limit**, including failures, retries, and attempts interrupted by crashes. It persists across restarts and resets at midnight UTC. It is **not a monetary spending cap**: model prices vary; prompts and replies incur API charges; a request can be billed even if its reply is lost. Also configure project-level controls and billing alerts in OpenAI. Don't assume an alert enforces a hard cap.

`MAX_CONTEXT_CHARS=24000` includes only recent complete question/answer pairs plus the current message; character counts are not token counts. The fixed instructions add overhead. Output is limited to `MAX_OUTPUT_TOKENS=1200` plus an independent 32,000-character defensive guard. Only the latest `MAX_HISTORY_TURNS=12` pairs are retained for each user/chat. Increase limits deliberately. Model-specific context limits can still cause API errors.

## Reliability and boundaries

- A batch's accepted jobs and next polling offset are committed in one transaction **before** Telegram can be told to advance. Replayed update IDs are deduplicated. Unauthorized payloads are not persisted.
- One worker processes jobs in order. A delayed retry can briefly delay other users. Run **one replica per bot token and one worker per database**. A local file lock prevents two local workers sharing a database; it cannot prevent another host/database from polling the same bot.
- Rate limits, network errors, and server errors get bounded exponential retries; `Retry-After` is honored up to one hour. Permanent request failures don't loop forever. A bad Telegram token or polling conflict stops the worker. A bad OpenAI key/model yields a generic user-visible error; check the logged status code and configuration.
- Replies are saved before sending; a restart won't normally regenerate them. Sent chunk progress is durable. **Exactly-once network delivery is impossible here**: if OpenAI finishes before a crash/timeout, retrying can incur another charge; if Telegram accepts a chunk before the local commit, retrying can duplicate that chunk. After the configured attempts, undeliverable replies are abandoned with a status-only error log. No silent promise of guaranteed delivery.
- Telegram retains pending updates for a limited time (currently documented as at most 24 hours). This cannot recover updates that expired while the service was down. On first start, queued messages may be processed; there is no automatic backlog deletion.
- Graceful stop waits for the current blocking request (up to 75 seconds). Supervisors allow 90 seconds. No token, message body, user ID, request URL, response body, or raw exception is intentionally logged.

## Privacy and security

Text and bounded conversation history are sent to OpenAI; replies go through Telegram. SQLite contains plaintext conversation history and pending messages/replies: restrict host and volume access, encrypt the disk/backups where appropriate, and don't share the database. The application creates new files with restrictive permissions, but you must fix permissions on pre-existing files. Completed jobs have their text and replies cleared; only up to 1,000 content-free queue records are kept.

`/reset` clears active history for that user/chat. It does **not** delete Telegram messages, backups, provider logs, or guarantee forensic erasure of SQLite/WAL storage. Messages already queued after `/reset` start the new history. `store: false` is sent to OpenAI; this is **not a claim of zero provider retention**. Review [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data) and Telegram's terms before sending sensitive information. Rotate leaked keys/tokens at the provider, never by posting replacements in chat.

There is no arbitrary URL/base-endpoint configuration, and HTTP redirects are refused to avoid forwarding credentials. Never run untrusted source trees or `.env` files. This is a minimal personal-use service, not a multi-tenant SaaS or a comprehensive abuse-prevention system.

## Docker

With `.env` configured:

```sh
docker compose up --build -d
docker compose logs -f
docker compose down
```

The container runs as an unprivileged user with a read-only root filesystem, no published ports, and a persistent named volume. Do not run `docker compose down -v` unless you intend to delete history and durable queue/quota state. Back up the database while stopped (or use SQLite's backup API), including consistent WAL handling. Recreating a blank database resets the quota and offset; do not use that as a retry mechanism.

## systemd (Linux alternative)

Install Python 3.11+, create an unprivileged `telegram-assistant` system user/group, and place the reviewed source in `/opt/dots-telegram-assistant`, owned by an administrator and readable by that user. Save configuration as `/etc/dots-telegram-assistant.env` (root-owned, mode `0600`; systemd loads it). Copy `deploy/dots-telegram-assistant.service` to `/etc/systemd/system/`, inspect its paths, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now dots-telegram-assistant
sudo journalctl -u dots-telegram-assistant -f
```

The unit uses `/var/lib/dots-telegram-assistant` for durable state. Use Docker **or** systemd, not both for one token. Native Windows is not supported by the POSIX file lock; use Linux/WSL or Docker.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q telegram_assistant tests
```

Tests use temporary SQLite databases and mocked network clients; no secrets, live bot, or paid API calls are needed. CI runs the same tests on Python 3.11, 3.12, and 3.13. Live provider integration, deployment, and real-account model access must be smoke-tested by the operator; offline tests do not establish those.

The service follows the official [Responses conversation-state guide](https://developers.openai.com/api/docs/guides/conversation-state) and [Telegram Bot API](https://core.telegram.org/bots/api). Its code and documentation were written independently; no private assistant configuration or private repository code is included.
