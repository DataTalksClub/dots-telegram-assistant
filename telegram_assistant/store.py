"""Single-worker durable queue. Intake and Telegram offset advance atomically."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('offset', 0);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                text TEXT NOT NULL, reply TEXT, cursor INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, send_attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quota (day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
        """)

    @property
    def offset(self):
        return self.db.execute("SELECT value FROM metadata WHERE key='offset'").fetchone()[0]

    @staticmethod
    def authorized(message, config):
        sender, chat = message.get("from", {}), message.get("chat", {})
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return False
        uid, cid = sender.get("id"), chat.get("id")
        return (type(uid) is int and type(cid) is int and
                uid in config.allowed_users and cid in config.allowed_chats and
                uid == cid and chat.get("type") == "private" and
                sender.get("is_bot") is False)

    def ingest(self, updates, config):
        # Validate all IDs before touching the offset. Telegram batches are ordered,
        # but sorting makes mocked/reordered batches safe too.
        if any(not isinstance(u, dict) or type(u.get("update_id")) is not int for u in updates):
            raise ValueError("Invalid Telegram update batch")
        with self.db:
            offset = self.offset
            for update in sorted(updates, key=lambda u: u["update_id"]):
                uid = update["update_id"]
                if uid < offset:
                    continue
                message = update.get("message", {})
                if isinstance(message, dict) and self.authorized(message, config):
                    text = message.get("text")
                    if not isinstance(text, str):
                        text = ""
                    # Store only bounded text, never attachments or the raw update.
                    text = text[:config.max_input_chars + 1]
                    self.db.execute("INSERT OR IGNORE INTO jobs (id,user_id,chat_id,text) VALUES (?,?,?,?)",
                                    (uid, message["from"]["id"], message["chat"]["id"], text))
                offset = uid + 1
            self.db.execute("UPDATE metadata SET value=? WHERE key='offset'", (offset,))

    def next_job(self):
        # Deliberately serialize: later turns must not overtake a retry/reset.
        return self.db.execute("SELECT * FROM jobs WHERE done=0 ORDER BY id LIMIT 1").fetchone()

    def context(self, job, config):
        rows = self.db.execute(
            "SELECT question,answer FROM history WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT ?",
            (job["user_id"], job["chat_id"], config.max_history_turns)).fetchall()
        remaining = config.max_context_chars - len(job["text"])
        turns = []
        for row in rows:
            size = len(row["question"]) + len(row["answer"])
            if size > remaining:
                break
            turns.append(row)
            remaining -= size
        messages = []
        for row in reversed(turns):
            messages.extend([{"role": "user", "content": row["question"]},
                             {"role": "assistant", "content": row["answer"]}])
        return messages + [{"role": "user", "content": job["text"]}]

    def reserve_call(self, job_id, limit, now):
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO quota VALUES (?,0)", (day,))
            calls = self.db.execute("SELECT calls FROM quota WHERE day=?", (day,)).fetchone()[0]
            if calls >= limit:
                return False
            self.db.execute("UPDATE quota SET calls=calls+1 WHERE day=?", (day,))
            self.db.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?", (job_id,))
            self.db.execute("DELETE FROM quota WHERE day < date(?, '-7 days')", (day,))
        return True

    def save_reply(self, job, text, config, remember=False, reset=False):
        with self.db:
            if reset:
                self.db.execute("DELETE FROM history WHERE user_id=? AND chat_id=?",
                                (job["user_id"], job["chat_id"]))
            if remember:
                self.db.execute("INSERT INTO history (user_id,chat_id,question,answer) VALUES (?,?,?,?)",
                                (job["user_id"], job["chat_id"], job["text"], text))
                self.db.execute("""DELETE FROM history WHERE user_id=? AND chat_id=? AND id NOT IN
                    (SELECT id FROM history WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT ?)""",
                    (job["user_id"], job["chat_id"], job["user_id"], job["chat_id"], config.max_history_turns))
            self.db.execute("UPDATE jobs SET reply=?,text='',due=0 WHERE id=?", (text, job["id"]))

    def update(self, job_id, **values):
        allowed = {"cursor", "attempts", "send_attempts", "due"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid job fields")
        with self.db:
            self.db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                            (*values.values(), job_id))

    def finish(self, job_id):
        with self.db:
            self.db.execute("UPDATE jobs SET done=1,text='',reply=NULL WHERE id=?", (job_id,))
            # The durable offset handles old update deduplication. Keep only 1000 tombstones.
            self.db.execute("DELETE FROM jobs WHERE done=1 AND id NOT IN (SELECT id FROM jobs WHERE done=1 ORDER BY id DESC LIMIT 1000)")

    def close(self):
        self.db.close()
