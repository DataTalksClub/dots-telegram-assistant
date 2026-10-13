"""Interactive local ID lookup; never edits the allowlist or sends Telegram messages."""
import os
import re
import secrets
import time

from .api import APIError, Telegram


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
        print("Set TELEGRAM_BOT_TOKEN in your environment first.")
        return 2
    nonce = "/pair " + secrets.token_hex(8)
    print("Stop any running worker first. In a PRIVATE chat with your bot, send:")
    print(nonce, flush=True)
    print("Waiting up to 2 minutes. No messages will be sent; no configuration is changed.", flush=True)
    telegram = Telegram(token)
    deadline = time.monotonic() + 120
    try:
        # Deliberately do not acknowledge updates; never discard pending bot messages.
        while time.monotonic() < deadline:
            for update in telegram.updates(0, timeout=5):
                message = update.get("message", {}) if isinstance(update, dict) else {}
                if not isinstance(message, dict):
                    continue
                sender, chat = message.get("from", {}), message.get("chat", {})
                if not isinstance(sender, dict) or not isinstance(chat, dict):
                    continue
                if (message.get("text") == nonce and chat.get("type") == "private" and
                        sender.get("is_bot") is False and type(sender.get("id")) is int and
                        sender.get("id") == chat.get("id")):
                    print(f"ALLOWED_TELEGRAM_USER_IDS={sender['id']}")
                    print(f"ALLOWED_TELEGRAM_CHAT_IDS={chat['id']}")
                    return 0
            time.sleep(1)
    except APIError as exc:
        print(f"Telegram lookup failed ({exc.code}). Check token and stop competing polling/webhooks.")
        return 1
    print("Timed out. Check the bot and private chat. A backlog of 100+ updates can hide the pairing message; see README.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
