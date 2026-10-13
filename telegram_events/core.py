"""Durable, single-owner Telegram transport. No inference is performed here."""
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

VERSION = '2026-07-28'
EVENT = 'telegram.message'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


class RPCError(Exception):
    def __init__(self, code, message, data=None):
        self.code, self.message, self.data = code, message, data
        super().__init__(message)


def check(condition, message='Invalid params'):
    if not condition:
        raise RPCError(-32602, message)


@dataclass(frozen=True)
class Config:
    token: str
    user_id: int
    chat_id: int
    auth_token: str
    callback_hosts: frozenset
    state_path: str = '/data/events.sqlite3'
    host: str = '127.0.0.1'
    port: int = 8765
    auth_mode: str = 'bearer'

    @classmethod
    def from_env(cls):
        cfg = cls(os.environ['TELEGRAM_BOT_TOKEN'], int(os.environ['TELEGRAM_USER_ID']),
                  int(os.environ['TELEGRAM_CHAT_ID']), os.getenv('MCP_AUTH_TOKEN', ''),
                  frozenset(x.strip() for x in os.environ['CALLBACK_ALLOWED_HOSTS'].split(',') if x.strip()),
                  os.getenv('STATE_PATH', '/data/events.sqlite3'), os.getenv('BIND_HOST', '127.0.0.1'),
                  int(os.getenv('PORT', '8765')), os.getenv('AUTH_MODE', 'oauth'))
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', cfg.token):
            raise ValueError('Invalid Telegram bot token format')
        if cfg.user_id <= 0 or cfg.user_id != cfg.chat_id:
            raise ValueError('Require one positive, matching private user/chat ID')
        if cfg.auth_mode not in ('oauth', 'bearer'):
            raise ValueError('AUTH_MODE must be oauth or bearer')
        if cfg.auth_mode == 'bearer' and (len(cfg.auth_token) < 32 or not cfg.auth_token.isascii() or any(c.isspace() for c in cfg.auth_token)):
            raise ValueError('MCP_AUTH_TOKEN must be a random ASCII token of at least 32 characters')
        if not cfg.callback_hosts:
            raise ValueError('CALLBACK_ALLOWED_HOSTS must contain verified callback hostnames')
        return cfg


class Store:
    def __init__(self, path, identity):
        self.lock = threading.RLock()
        self.file_lock = None
        self.db = None
        try:
            if path != ':memory:':
                import fcntl
                parent = Path(path).parent
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if parent.is_symlink() or parent.stat().st_mode & 0o077:
                    raise ValueError('State directory must be private (chmod 700)')
                flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
                fd = os.open(str(path) + '.lock', flags, 0o600)
                self.file_lock = os.fdopen(fd, 'a')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = os.open(path, flags, 0o600)
                info = os.fstat(fd)
                os.close(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    raise ValueError('State file must be private and regular (chmod 600)')
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.executescript('''
            PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES('offset','0');
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, update_id INTEGER UNIQUE, text TEXT NOT NULL, occurred REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,url TEXT NOT NULL,secret TEXT NOT NULL,old_secret TEXT,rotation_until REAL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries(sub_id TEXT,event_id TEXT,body TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL,created REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(sub_id,event_id));
            CREATE TABLE IF NOT EXISTS replies(key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,status TEXT NOT NULL,result TEXT);
            ''')
            existing = self.db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()
            if existing and existing[0] != identity:
                raise ValueError('State belongs to a different bot/owner; use a separate private state directory')
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES('identity',?)", (identity,))
    
        except Exception:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
        if self.file_lock:
            self.file_lock.close()

    def offset(self):
        with self.lock:
            return int(self.db.execute("SELECT value FROM metadata WHERE key='offset'").fetchone()[0])


class Service:
    def __init__(self, config, store, callbacks, telegram, clock=time.time):
        self.config, self.store, self.callbacks, self.telegram, self.clock = config, store, callbacks, telegram, clock

    def subscription_id(self, p):
        check(p.get('name') == EVENT and p.get('arguments', {}) == {})
        d = p.get('delivery')
        check(isinstance(d, dict) and d.get('mode') == 'webhook' and isinstance(d.get('url'), str))
        identity = [str(self.config.user_id), d['url'], EVENT, {}]
        return 'sub_' + hashlib.sha256(canonical(identity).encode()).hexdigest()

    def subscribe(self, p, authorization_expires=None):
        sid = self.subscription_id(p)
        check(p.get('cursor') is None, 'Replay is not supported; cursor must be null')
        ttl = p.get('ttlMs', 86400000)
        check(ttl is None or (type(ttl) is int and ttl > 0), 'ttlMs must be positive integer or null')
        lifetime = min(ttl / 1000, 86400) if ttl is not None else 86400
        d = p['delivery']
        check(isinstance(d.get('secret'), str))
        from .network import validate_secret
        try:
            validate_secret(d['secret'])
        except ValueError:
            raise RPCError(-32602, 'Invalid signing secret') from None
        try:
            self.callbacks.validate(d['url'])
        except ValueError:
            raise RPCError(-32602, 'Invalid or disallowed HTTPS callback URL') from None
        # Serialize subscription state transitions and deliveries, including unsubscribe.
        with self.store.lock:
            now = self.clock()
            other = self.store.db.execute('SELECT id FROM subscriptions WHERE expires>? AND id!=?', (now, sid)).fetchone()
            check(other is None, 'One active subscription is allowed; unsubscribe the existing destination first')
            try:
                self.callbacks.verify(d['url'], d['secret'], sid)
            except Exception:
                raise RPCError(-32015, 'Callback verification failed', {'reason': 'challenge_failed'}) from None
            now = self.clock()
            old = self.store.db.execute('SELECT secret FROM subscriptions WHERE id=?', (sid,)).fetchone()
            old_secret = old[0] if old and old[0] != d['secret'] else None
            # A refresh with the same key retains an in-progress rotation window.
            prior = self.store.db.execute('SELECT old_secret,rotation_until FROM subscriptions WHERE id=?', (sid,)).fetchone()
            until = now + 300 if old_secret else (prior[1] if prior else 0)
            if old_secret is None and prior and prior[1] > now:
                old_secret = prior[0]
            expires = now + lifetime
            if authorization_expires is not None:
                expires = min(expires, authorization_expires)
                check(expires > now, 'Authorization expired during callback verification')
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO subscriptions VALUES(?,?,?,?,?,?)',
                                      (sid, d['url'], d['secret'], old_secret, until, expires))
            return {'id': sid, 'refreshBefore': iso(expires), 'cursor': None, 'truncated': False}

    def unsubscribe(self, p):
        sid = self.subscription_id(p)
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM subscriptions WHERE id=?', (sid,))
            self.store.db.execute('DELETE FROM deliveries WHERE sub_id=?', (sid,))
        return {}

    def ingest(self, updates):
        """Persist intake plus offset together; unauthorized content is never stored."""
        with self.store.lock, self.store.db:
            offset = self.store.offset()
            for u in sorted(updates, key=lambda u: u['update_id']):
                uid = u.get('update_id')
                if type(uid) is not int or uid < offset:
                    continue
                m = u.get('message', {})
                authorized = (isinstance(m, dict) and m.get('from', {}).get('id') == self.config.user_id
                              and not m.get('from', {}).get('is_bot', False)
                              and m.get('chat', {}).get('id') == self.config.chat_id
                              and m.get('chat', {}).get('type') == 'private')
                if authorized and isinstance(m.get('text'), str) and type(m.get('message_id')) is int:
                    mid, text = m['message_id'], m['text']
                    occurred = m.get('date', self.clock())
                    if type(occurred) not in (int, float) or not 0 <= occurred < 253402300800:
                        occurred = self.clock()
                    inserted = self.store.db.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?)', (mid, uid, text, occurred)).rowcount
                    if inserted:
                        event_id = 'tg_' + str(uid)
                        body = canonical({'eventId': event_id, 'name': EVENT, 'timestamp': iso(occurred),
                                          'data': {'message_id': mid, 'text': text[:500], 'truncated': len(text) > 500}, 'cursor': None})
                        now = self.clock()
                        for sub in self.store.db.execute('SELECT id FROM subscriptions WHERE expires>?', (now,)):
                            self.store.db.execute('INSERT OR IGNORE INTO deliveries(sub_id,event_id,body,due,created) VALUES(?,?,?,?,?)', (sub[0], event_id, body, now, now))
                offset = uid + 1
            self.store.db.execute("UPDATE metadata SET value=? WHERE key='offset'", (str(offset),))

    def deliver_one(self):
        with self.store.lock:
            now = self.clock()
            with self.store.db:
                self.store.db.execute("UPDATE deliveries SET status='expired' WHERE status='pending' AND (created<? OR sub_id NOT IN (SELECT id FROM subscriptions WHERE expires>?))", (now - 86400, now))
                self.store.db.execute('UPDATE subscriptions SET old_secret=NULL WHERE rotation_until<?', (now,))
            row = self.store.db.execute("SELECT d.*,s.url,s.secret,s.old_secret FROM deliveries d JOIN subscriptions s ON d.sub_id=s.id WHERE d.status='pending' AND d.due<=? AND s.expires>? ORDER BY d.created,d.event_id LIMIT 1", (now, now)).fetchone()
            if row is None:
                return False
            try:
                status, _ = self.callbacks.post(row['url'], row['secret'], row['sub_id'], row['event_id'], row['body'].encode(), old_secret=row['old_secret'])
            except Exception:
                status = 503
            attempts = row['attempts'] + 1
            if 200 <= status < 300:
                outcome = 'delivered'
            elif status == 410:
                outcome = 'gone'
            elif status == 413 or (400 <= status < 500 and status not in (408, 429)):
                outcome = 'rejected'
            elif 300 <= status < 400 or attempts >= 8:
                outcome = 'failed'
            else:
                outcome = 'pending'
            with self.store.db:
                self.store.db.execute('UPDATE deliveries SET attempts=?,due=?,status=? WHERE sub_id=? AND event_id=?', (attempts, self.clock() + min(3600, 2 ** attempts), outcome, row['sub_id'], row['event_id']))
                if status == 410:
                    self.store.db.execute('DELETE FROM subscriptions WHERE id=?', (row['sub_id'],))
            return True

    def read_message(self, args):
        check(set(args) == {'message_id'} and type(args.get('message_id')) is int)
        with self.store.lock:
            row = self.store.db.execute('SELECT id,text,occurred FROM messages WHERE id=?', (args['message_id'],)).fetchone()
        check(row is not None, 'Message not found in paired private chat')
        return {'message_id': row['id'], 'text': row['text'], 'timestamp': iso(row['occurred'])}

    def reply(self, args):
        check(set(args) == {'message_id', 'text', 'idempotency_key'})
        check(type(args.get('message_id')) is int)
        check(isinstance(args.get('text'), str) and 0 < len(args['text']) <= 4096)
        check(isinstance(args.get('idempotency_key'), str) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}', args['idempotency_key']) is not None)
        fingerprint = hashlib.sha256(canonical(args).encode()).hexdigest()
        key = args['idempotency_key']
        with self.store.lock:
            self.read_message({'message_id': args['message_id']})
            prior = self.store.db.execute('SELECT * FROM replies WHERE key=?', (key,)).fetchone()
            if prior:
                check(prior['fingerprint'] == fingerprint, 'Idempotency key was already used for different content')
                return json.loads(prior['result']) if prior['result'] else {'status': 'unknown', 'detail': 'An earlier send may have succeeded. Check Telegram; do not retry with a new key automatically.'}
            with self.store.db:
                self.store.db.execute('INSERT INTO replies VALUES(?,?,?,NULL)', (key, fingerprint, 'unknown'))
            try:
                result = self.telegram.send(args['text'], args['message_id'])
                response = {'status': 'sent', 'message_id': result['message_id']}
            except Exception:
                response = {'status': 'unknown', 'detail': 'Telegram may have accepted this reply. Check Telegram before explicitly sending again; this key will not resend.'}
            with self.store.db:
                self.store.db.execute('UPDATE replies SET status=?,result=? WHERE key=?', (response['status'], canonical(response), key))
            return response
