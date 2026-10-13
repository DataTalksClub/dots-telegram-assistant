import fcntl
import logging
import os
from pathlib import Path
import signal
import time

from .api import APIError, OpenAI, Telegram
from .config import Config
from .store import Store

LOG = logging.getLogger("telegram_assistant")
HELP = (
    "I'm a separate OpenAI-powered Telegram assistant with my own local conversation history. "
    "I don't share another assistant's memory, tools, or account. Send a text question.\n\n"
    "/help - show this message\n/reset - clear this bot's local conversation history\n\n"
    "Text and recent conversation context go to OpenAI; replies go through Telegram. "
    "No web browsing, shell commands, file access, or other action tools are enabled. "
    "Don't send passwords or other secrets."
)


def chunks(text, limit=4000):
    """Limit UTF-16 code units, safely handling astral emoji without splitting code points."""
    result, current, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            result.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        result.append("".join(current))
    return result


class Bot:
    def __init__(self, config, store, telegram, openai, clock=time.time, stop_requested=lambda: False):
        self.config, self.store, self.telegram, self.openai = config, store, telegram, openai
        self.clock = clock
        self.stop_requested = stop_requested

    def process_one(self):
        if self.stop_requested():
            return False
        job = self.store.next_job()
        if job is None or job["due"] > self.clock():
            return False
        # Recheck access on restart: revoked IDs must not be processed or sent replies.
        if job["user_id"] not in self.config.allowed_users or job["chat_id"] not in self.config.allowed_chats:
            self.store.finish(job["id"])
            return True
        if job["reply"] is None:
            self.generate(job)
            job = self.store.next_job()
            if job is None or job["reply"] is None:
                return True
        parts = chunks(job["reply"])
        if job["send_attempts"] >= self.config.max_attempts:
            LOG.error("Reply delivery abandoned after bounded attempts")
            self.store.finish(job["id"])
            return True
        for index in range(job["cursor"], len(parts)):
            if self.stop_requested():
                return False
            # Record before the network call: process crashes also consume an attempt.
            self.store.update(job["id"], send_attempts=job["send_attempts"] + 1)
            try:
                self.telegram.send(job["chat_id"], parts[index])
            except APIError as exc:
                if exc.code in (401, 409):
                    raise
                if exc.retryable and job["send_attempts"] + 1 < self.config.max_attempts:
                    self.store.update(job["id"], due=self.clock() + max(exc.retry_after, 2 ** (job["send_attempts"] + 1)))
                else:
                    LOG.error("Reply delivery failed (%s)", exc.code)
                    self.store.finish(job["id"])
                return True
            self.store.update(job["id"], cursor=index + 1, send_attempts=0)
            job = self.store.next_job()
        self.store.finish(job["id"])
        return True

    def generate(self, job):
        text = job["text"].strip()
        command = text.split(maxsplit=1)[0].lower() if text else ""
        if command in ("/start", "/help"):
            self.store.save_reply(job, HELP, self.config)
        elif command == "/reset":
            self.store.save_reply(job, "Local conversation history cleared. This doesn't delete Telegram messages or provider records.", self.config, reset=True)
        elif command.startswith("/"):
            self.store.save_reply(job, "Unknown command. Use /help or /reset, or send a text question.", self.config)
        elif not text:
            self.store.save_reply(job, "I can read text messages only. Please send your question as text.", self.config)
        elif len(job["text"]) > self.config.max_input_chars:
            self.store.save_reply(job, f"Please shorten your message to {self.config.max_input_chars} characters or fewer.", self.config)
        elif job["attempts"] >= self.config.max_attempts:
            self.store.save_reply(job, "I couldn't get a reply after several attempts. Please try again later.", self.config)
        elif not self.store.reserve_call(job["id"], self.config.max_daily_calls, self.clock()):
            self.store.save_reply(job, "This bot's daily API-call limit is reached. It resets at midnight UTC.", self.config)
        else:
            try:
                answer = self.openai.answer(self.store.context(job, self.config))
            except APIError as exc:
                LOG.warning("OpenAI request failed (%s)", exc.code)
                if exc.retryable and job["attempts"] + 1 < self.config.max_attempts:
                    self.store.update(job["id"], due=self.clock() + max(exc.retry_after, 2 ** (job["attempts"] + 1)))
                else:
                    self.store.save_reply(job, "The AI service is unavailable or this bot's configuration needs attention. Please try again later.", self.config)
            else:
                self.store.save_reply(job, answer, self.config, remember=True)


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
    except ValueError as exc:
        LOG.error("Configuration error: %s", exc)
        return 2
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(config.db_path + ".lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG.error("Another worker is using this database")
        lock.close()
        return 2
    store = Store(config.db_path)
    running = True
    bot = Bot(config, store, Telegram(config.telegram_token), OpenAI(config),
              stop_requested=lambda: not running)

    def stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("Assistant started; allowlist enforced; no action tools enabled")
    failures = 0
    try:
        while running:
            try:
                pending = store.next_job()
                updates = bot.telegram.updates(store.offset, timeout=1 if pending else 30)
                store.ingest(updates, config)
                if running:
                    bot.process_one()
                failures = 0
                if pending and not updates:
                    time.sleep(0.2)
            except APIError as exc:
                # Only controlled fields are logged. Never log exception reprs/tracebacks,
                # which can include tokens embedded in Telegram request URLs.
                LOG.warning("%s request failed (%s)", exc.service, exc.code)
                if not exc.retryable:
                    return 1
                failures += 1
                delay = max(exc.retry_after, min(60, 2 ** min(failures, 6)))
                for _ in range(delay):
                    if not running:
                        break
                    time.sleep(1)
    except Exception:
        LOG.error("Unexpected local failure; stopped safely. Check database/disk and configuration.")
        return 1
    finally:
        store.close()
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
