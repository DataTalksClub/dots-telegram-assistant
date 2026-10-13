"""Offline durable state and fixed-owner behavior; no external network calls."""
import base64
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from telegram_events.core import Config, EVENT, RPCError, Service, Store, canonical
from telegram_events.telegram import Telegram

SECRET = 'whsec_' + base64.b64encode(b'a' * 32).decode()
NEW_SECRET = 'whsec_' + base64.b64encode(b'b' * 32).decode()


def update(uid=1, mid=10, text='hello', owner=42, chat=42, kind='private'):
    return {'update_id': uid, 'message': {'message_id': mid, 'date': 1700000000,
            'from': {'id': owner}, 'chat': {'id': chat, 'type': kind}, 'text': text}}


def subscription(**kw):
    result = {'name': EVENT, 'arguments': {}, 'delivery': {'mode': 'webhook',
              'url': 'https://callbacks.example.com/events', 'secret': SECRET}}
    result.update(kw)
    return result


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = 1700000000
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'events.db')
        self.store = Store(self.path, 'owner42')
        self.callbacks = Mock()
        self.callbacks.post.return_value = (204, b'')
        self.telegram = Mock()
        self.telegram.send.return_value = {'message_id': 500}
        self.config = Config('123:abc', 42, 42, 'a' * 32, frozenset({'callbacks.example.com'}))
        self.service = Service(self.config, self.store, self.callbacks, self.telegram, lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def rows(self, table):
        return list(self.store.db.execute('SELECT * FROM ' + table))

    def reopen(self):
        self.store.close()
        self.store = Store(self.path, 'owner42')
        self.service.store = self.store

    def queue(self):
        result = self.service.subscribe(subscription())
        self.service.ingest([update()])
        return result['id']

    def test_owner_filter_offset_and_dedup_survive_restart(self):
        self.queue()
        wrong = [update(2, 20, owner=7), update(3, 30, chat=7), update(4, 40, kind='group')]
        bot = update(5, 50)
        bot['message']['from']['is_bot'] = True
        no_text = update(6, 60)
        del no_text['message']['text']
        edited = {'update_id': 7, 'edited_message': update()['message']}
        self.service.ingest(wrong + [bot, no_text, edited, update(8, 10)])
        self.assertEqual(self.store.offset(), 9)
        self.assertEqual(len(self.rows('messages')), 1)
        self.assertEqual(len(self.rows('deliveries')), 1)
        self.reopen()
        self.service.ingest([update(), update(8, 10)])
        self.assertEqual(self.store.offset(), 9)
        self.assertEqual(len(self.rows('messages')), 1)
        self.assertEqual(len(self.rows('deliveries')), 1)
        self.assertEqual(self.service.read_message({'message_id': 10})['text'], 'hello')
        with self.assertRaises(RPCError):
            self.service.read_message({'message_id': 20})

    def test_sorted_intake_and_atomic_rollback(self):
        self.service.ingest([update(3, 30), update(1, 10), update(2, 20)])
        self.assertEqual(self.store.offset(), 4)
        self.assertEqual(len(self.rows('messages')), 3)
        self.store.db.execute("CREATE TRIGGER reject_message BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT, 'synthetic disk error'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.ingest([update(4, 40)])
        self.assertEqual(self.store.offset(), 4)
        self.assertEqual(len(self.rows('messages')), 3)

    def test_event_preview_and_read_full_text(self):
        self.service.subscribe(subscription())
        self.service.ingest([update(text='é' * 800)])
        event = json.loads(self.rows('deliveries')[0]['body'])
        self.assertEqual(event['eventId'], 'tg_1')
        self.assertEqual(event['name'], EVENT)
        self.assertIsNone(event['cursor'])
        self.assertEqual(len(event['data']['text']), 500)
        self.assertTrue(event['data']['truncated'])
        self.assertEqual(len(self.service.read_message({'message_id': 10})['text']), 800)
        self.assertNotIn('chat_id', event['data'])

    def test_subscription_lifetime_capped_by_authorization_expiry(self):
        self.service.subscribe(subscription(), authorization_expires=self.now + 60)
        self.assertEqual(self.rows('subscriptions')[0]['expires'], self.now + 60)
        self.callbacks.verify.side_effect = lambda *args: setattr(self, 'now', self.now + 70)
        with self.assertRaises(RPCError):
            self.service.subscribe(subscription(), authorization_expires=self.now + 60)

    def test_callback_validation_and_verification_errors_are_distinct(self):
        self.callbacks.validate.side_effect = ValueError('private destination')
        with self.assertRaises(RPCError) as error:
            self.service.subscribe(subscription())
        self.assertEqual(error.exception.code, -32602)
        self.assertNotIn('private destination', str(error.exception))
        self.callbacks.verify.assert_not_called()
        self.callbacks.validate.side_effect = None
        self.callbacks.verify.side_effect = RuntimeError('private secret')
        with self.assertRaises(RPCError) as error:
            self.service.subscribe(subscription())
        self.assertEqual(error.exception.code, -32015)
        self.assertEqual(error.exception.data, {'reason': 'challenge_failed'})

    def test_preview_truncated_boundary(self):
        self.service.subscribe(subscription())
        self.service.ingest([update(1, 1, 'a' * 499), update(2, 2, 'a' * 500), update(3, 3, 'a' * 501)])
        flags = [json.loads(row['body'])['data']['truncated'] for row in self.rows('deliveries')]
        self.assertEqual(flags, [False, False, True])

    def test_subscription_is_future_only(self):
        self.service.ingest([update()])
        self.service.subscribe(subscription())
        self.assertEqual(self.rows('deliveries'), [])

    def test_subscribe_refresh_stable_id_verification_and_ttl_cap(self):
        one = self.service.subscribe(subscription(ttlMs=200000000))
        self.assertEqual(self.rows('subscriptions')[0]['expires'], self.now + 86400)
        self.now += 1
        two = self.service.subscribe(subscription(ttlMs=1000))
        self.assertEqual(one['id'], two['id'])
        self.assertEqual(len(self.rows('subscriptions')), 1)
        self.assertEqual(self.callbacks.verify.call_count, 2)
        self.assertEqual(self.rows('subscriptions')[0]['expires'], self.now + 1)

    def test_refresh_failure_keeps_existing_subscription(self):
        self.service.subscribe(subscription())
        before = dict(self.rows('subscriptions')[0])
        self.callbacks.verify.side_effect = RuntimeError('secret token leak')
        with self.assertRaises(RPCError) as error:
            self.service.subscribe(subscription(ttlMs=1000))
        self.assertNotIn('secret token leak', str(error.exception))
        self.assertEqual(dict(self.rows('subscriptions')[0]), before)

    def test_invalid_subscription_parameters(self):
        cases = [subscription(cursor='replay'), subscription(ttlMs=0), subscription(ttlMs=True),
                 subscription(ttlMs='100'), subscription(arguments={'owner': 7}), subscription(name='other')]
        weak = subscription(); weak['delivery']['secret'] = 'weak'; cases.append(weak)
        for p in cases:
            with self.subTest(p=p), self.assertRaises(RPCError):
                self.service.subscribe(p)
        self.callbacks.verify.assert_not_called()

    def test_only_one_destination_until_unsubscribed_or_expired(self):
        self.service.subscribe(subscription(ttlMs=1000))
        another = subscription(); another['delivery']['url'] += '/two'
        with self.assertRaises(RPCError):
            self.service.subscribe(another)
        self.now += 2
        self.service.subscribe(another)

    def test_unsubscribe_is_idempotent_and_clears_queued_work(self):
        self.queue()
        self.assertEqual(self.service.unsubscribe(subscription()), {})
        self.assertEqual(self.service.unsubscribe(subscription()), {})
        self.assertEqual(self.rows('subscriptions'), [])
        self.assertEqual(self.rows('deliveries'), [])
        self.assertFalse(self.service.deliver_one())

    def test_secret_rotation_and_same_secret_refresh(self):
        self.queue()
        new = subscription(); new['delivery']['secret'] = NEW_SECRET
        self.service.subscribe(new)
        self.now += 100
        self.service.subscribe(new)
        self.assertEqual(self.rows('subscriptions')[0]['old_secret'], SECRET)
        self.service.deliver_one()
        self.assertEqual(self.callbacks.post.call_args.kwargs['old_secret'], SECRET)
        self.now += 201
        self.service.ingest([update(2, 20)])
        self.service.deliver_one()
        self.assertIsNone(self.callbacks.post.call_args.kwargs['old_secret'])

    def test_expired_subscription_never_delivers(self):
        self.service.subscribe(subscription(ttlMs=1000))
        self.service.ingest([update()])
        self.now += 1
        self.assertFalse(self.service.deliver_one())
        self.assertEqual(self.rows('deliveries')[0]['status'], 'expired')
        self.service.ingest([update(2, 20)])
        self.assertEqual(len(self.rows('deliveries')), 1)

    def test_retry_preserves_exact_event_and_stops_after_eight_attempts(self):
        self.queue()
        self.callbacks.post.return_value = (503, b'')
        for attempt in range(1, 9):
            self.assertTrue(self.service.deliver_one())
            row = self.rows('deliveries')[0]
            self.assertEqual(row['attempts'], attempt)
            self.assertEqual(row['status'], 'pending' if attempt < 8 else 'failed')
            self.assertFalse(self.service.deliver_one())
            self.now = row['due']
        calls = self.callbacks.post.call_args_list
        self.assertTrue(all(c == calls[0] for c in calls))

    def test_status_policy(self):
        for status, outcome in [(200, 'delivered'), (299, 'delivered'), (301, 'failed'),
                                (400, 'rejected'), (401, 'rejected'), (408, 'pending'),
                                (413, 'rejected'), (429, 'pending'), (500, 'pending')]:
            with self.subTest(status=status):
                self.service.unsubscribe(subscription())
                self.service.subscribe(subscription())
                self.service.ingest([update(status, status)])
                self.callbacks.post.return_value = (status, b'')
                self.assertTrue(self.service.deliver_one())
                self.assertEqual(self.rows('deliveries')[0]['status'], outcome)

    def test_410_removes_subscription_and_no_other_delivery(self):
        self.queue()
        self.service.ingest([update(2, 20)])
        self.callbacks.post.return_value = (410, b'')
        self.service.deliver_one()
        self.assertEqual(self.rows('subscriptions'), [])
        self.assertFalse(self.service.deliver_one())
        self.assertEqual(self.callbacks.post.call_count, 1)

    def test_callback_exception_retries_without_exposing_details(self):
        self.queue()
        self.callbacks.post.side_effect = RuntimeError('private callback credential')
        self.assertTrue(self.service.deliver_one())
        self.assertEqual(self.rows('deliveries')[0]['status'], 'pending')
        self.assertNotIn('credential', self.rows('deliveries')[0]['body'])

    def test_pending_delivery_survives_restart(self):
        self.queue()
        self.callbacks.post.return_value = (503, b'')
        self.service.deliver_one()
        before = dict(self.rows('deliveries')[0])
        self.reopen()
        self.assertEqual(dict(self.rows('deliveries')[0]), before)
        self.now = before['due']
        self.callbacks.post.return_value = (204, b'')
        self.service.deliver_one()
        self.assertEqual(self.rows('deliveries')[0]['status'], 'delivered')
        self.assertEqual(self.callbacks.post.call_args_list[0], self.callbacks.post.call_args_list[1])

    def test_parallel_duplicate_replies_send_once(self):
        self.service.ingest([update()])
        args = {'message_id': 10, 'text': 'answer', 'idempotency_key': 'request_123'}
        results = []
        workers = [threading.Thread(target=lambda: results.append(self.service.reply(args))) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(2)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(result == {'status': 'sent', 'message_id': 500} for result in results))
        self.telegram.send.assert_called_once()

    def test_reply_idempotency_is_durable_and_content_bound(self):
        self.service.ingest([update()])
        args = {'message_id': 10, 'text': 'answer', 'idempotency_key': 'request_123'}
        result = self.service.reply(args)
        self.assertEqual(result, {'status': 'sent', 'message_id': 500})
        self.reopen()
        self.assertEqual(self.service.reply(args), result)
        self.telegram.send.assert_called_once_with('answer', 10)
        with self.assertRaises(RPCError):
            self.service.reply(dict(args, text='changed'))

    def test_ambiguous_send_is_not_retried(self):
        self.service.ingest([update()])
        self.telegram.send.side_effect = TimeoutError('private token')
        args = {'message_id': 10, 'text': 'answer', 'idempotency_key': 'request_123'}
        result = self.service.reply(args)
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('private token', canonical(result))
        self.reopen()
        self.assertEqual(self.service.reply(args), result)
        self.telegram.send.assert_called_once()

    def test_crash_after_reserving_key_is_not_retried(self):
        self.service.ingest([update()])
        self.telegram.send.side_effect = KeyboardInterrupt
        args = {'message_id': 10, 'text': 'answer', 'idempotency_key': 'request_123'}
        with self.assertRaises(KeyboardInterrupt):
            self.service.reply(args)
        self.reopen()
        self.assertEqual(self.service.reply(args)['status'], 'unknown')
        self.telegram.send.assert_called_once()

    def test_reply_rejects_unowned_messages_and_extra_destinations(self):
        self.service.ingest([update()])
        valid = {'message_id': 10, 'text': 'answer', 'idempotency_key': 'request_123'}
        cases = [dict(valid, message_id=99), dict(valid, message_id=True), dict(valid, chat_id=7),
                 dict(valid, text=''), dict(valid, text='a' * 4097), dict(valid, idempotency_key='bad')]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(RPCError):
                self.service.reply(args)
        self.telegram.send.assert_not_called()


class StoreConfigTests(unittest.TestCase):
    def test_private_state_and_single_process_lock(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'state.db')
            one = Store(path, 'same')
            try:
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                with self.assertRaises(BlockingIOError):
                    Store(path, 'same')
            finally:
                one.close()
            with self.assertRaises(ValueError):
                Store(path, 'different')

    def test_refuse_world_readable_directory(self):
        with tempfile.TemporaryDirectory() as root:
            os.chmod(root, 0o755)
            with self.assertRaises(ValueError):
                Store(str(Path(root) / 'state.db'), 'owner')

    def test_config_owner_and_secret_constraints(self):
        env = {'AUTH_MODE': 'bearer', 'TELEGRAM_BOT_TOKEN': '123:abc_DEF', 'TELEGRAM_USER_ID': '42',
               'TELEGRAM_CHAT_ID': '42', 'MCP_AUTH_TOKEN': 'x' * 32,
               'CALLBACK_ALLOWED_HOSTS': 'callbacks.example.com'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Config.from_env().user_id, 42)
            for key, value in [('TELEGRAM_BOT_TOKEN', 'bad'), ('TELEGRAM_CHAT_ID', '7'),
                               ('TELEGRAM_USER_ID', '-42'), ('MCP_AUTH_TOKEN', 'short'),
                               ('MCP_AUTH_TOKEN', 'x' * 31 + ' '), ('CALLBACK_ALLOWED_HOSTS', '')]:
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                    Config.from_env()


class TelegramTests(unittest.TestCase):
    def test_fixed_destination_no_parse_mode_and_poll_filter(self):
        client = Telegram('123:secret', 42)
        client.call = Mock()
        client.send('a < b', 100)
        method, payload = client.call.call_args.args
        self.assertEqual(method, 'sendMessage')
        self.assertEqual(payload['chat_id'], 42)
        self.assertEqual(payload['reply_parameters'], {'message_id': 100})
        self.assertNotIn('parse_mode', payload)
        client.updates(123)
        self.assertEqual(client.call.call_args.args[1]['allowed_updates'], ['message'])
        self.assertEqual(client.call.call_args.args[1]['offset'], 123)

    def test_api_call_fixed_https_destination_and_bounded_response(self):
        with patch('telegram_events.telegram.http.client.HTTPSConnection') as cls:
            response = cls.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"ok":true,"result":{"message_id":101}}'
            result = Telegram('123:secret', 42).send('literal <text>', 10)
            self.assertEqual(result['message_id'], 101)
            self.assertEqual(cls.call_args.args[0], 'api.telegram.org')
            self.assertEqual(cls.call_args.kwargs['timeout'], 40)
            request = cls.return_value.request.call_args.args
            self.assertEqual(request[:2], ('POST', '/bot123:secret/sendMessage'))
            payload = json.loads(request[2])
            self.assertEqual(payload['chat_id'], 42)
            self.assertEqual(payload['text'], 'literal <text>')
            self.assertNotIn('parse_mode', payload)
            response.read.assert_called_once_with(2 * 1024 * 1024 + 1)
            cls.return_value.close.assert_called_once()

    def test_api_failure_and_oversize_response_sanitized(self):
        for status, body in [(500, b'secret'), (200, b'{"ok":false,"description":"secret"}'),
                             (200, b'x' * (2 * 1024 * 1024 + 1)), (200, b'bad json')]:
            with self.subTest(status=status, size=len(body)), patch('telegram_events.telegram.http.client.HTTPSConnection') as cls:
                response = cls.return_value.getresponse.return_value
                response.status, response.read.return_value = status, body
                with self.assertRaises(RuntimeError) as error:
                    Telegram('123:secret', 42).send('hello', 10)
                self.assertNotIn('secret', str(error.exception))
                cls.return_value.close.assert_called_once()

    def test_telegram_network_error_is_sanitized(self):
        with patch('telegram_events.telegram.http.client.HTTPSConnection') as cls:
            cls.return_value.request.side_effect = RuntimeError('/bot123:secret/sendMessage')
            with self.assertRaises(RuntimeError) as error:
                Telegram('123:secret', 42).send('hello', 10)
            self.assertNotIn('secret', str(error.exception))
            cls.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
