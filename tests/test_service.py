"""Offline security, durability, transport, and restart regression tests.

All credentials and updates are fabricated. No test may contact either API.
"""
import dataclasses
import io
import json
import logging
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from telegram_assistant.api import APIError, INSTRUCTIONS, OpenAI, Telegram, post_json
from telegram_assistant.app import Bot, HELP, chunks, main
from telegram_assistant.config import Config
from telegram_assistant.store import Store

TOKEN = "123456:LOCAL_TEST_TOKEN_NOT_A_CREDENTIAL"
KEY = "sk-local-test-placeholder-not-a-credential"
ENV = {
    "TELEGRAM_BOT_TOKEN": TOKEN,
    "OPENAI_API_KEY": KEY,
    "ALLOWED_TELEGRAM_USER_IDS": "101",
    "ALLOWED_TELEGRAM_CHAT_IDS": "101",
}


def config(**kwargs):
    return dataclasses.replace(Config(TOKEN, KEY, frozenset({101, 202}), frozenset({101, 202})), **kwargs)


def update(uid=1, text="Hello", sender=101, chat=101, chat_type="private", is_bot=False):
    return {"update_id": uid, "message": {"from": {"id": sender, "is_bot": is_bot},
            "chat": {"id": chat, "type": chat_type}, "text": text}}


class ConfigTests(unittest.TestCase):
    def load(self, changes=None, remove=()):
        env = {**ENV, **(changes or {})}
        for name in remove:
            env.pop(name, None)
        with patch.dict("os.environ", env, clear=True):
            return Config.from_env()

    def test_valid_defaults(self):
        cfg = self.load()
        self.assertEqual(cfg.allowed_users, frozenset({101}))
        self.assertEqual(cfg.allowed_chats, frozenset({101}))
        self.assertGreaterEqual(cfg.max_context_chars, cfg.max_input_chars)
        self.assertEqual(cfg.max_attempts, 3)

    def test_both_allowlists_are_mandatory(self):
        for name in ("ALLOWED_TELEGRAM_USER_IDS", "ALLOWED_TELEGRAM_CHAT_IDS"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load(remove=(name,))

    def test_allowlists_fail_closed(self):
        for name in ("ALLOWED_TELEGRAM_USER_IDS", "ALLOWED_TELEGRAM_CHAT_IDS"):
            for value in ("", "*", "101,", ",101", "101,,202", "-101", "0", "1.0", "True", "101;202"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.load({name: value})

    def test_allowlists_trim_and_deduplicate(self):
        self.assertEqual(self.load({"ALLOWED_TELEGRAM_USER_IDS": "101, 202,101"}).allowed_users,
                         frozenset({101, 202}))

    def test_credentials_missing_or_malformed(self):
        for token in ("", "123", "abc:" + "x" * 30, "123:short", "123:" + "x" * 25 + " "):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.load({"TELEGRAM_BOT_TOKEN": token})
        for key in ("", "replace_me", " key", "key\n"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load({"OPENAI_API_KEY": key})

    def test_repr_does_not_expose_credentials(self):
        rendered = repr(self.load())
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn(KEY, rendered)

    def test_numeric_limits_are_bounded(self):
        limits = {"MAX_INPUT_CHARS": (1, 32000), "MAX_CONTEXT_CHARS": (1, 100000),
                  "MAX_OUTPUT_TOKENS": (16, 8000), "MAX_HISTORY_TURNS": (1, 100),
                  "MAX_DAILY_CALLS": (1, 10000), "MAX_ATTEMPTS": (1, 10)}
        for name, (low, high) in limits.items():
            for value in (str(low - 1), str(high + 1), "nan", "1.5", ""):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.load({name: value})

    def test_context_cannot_be_smaller_than_input(self):
        with self.assertRaises(ValueError):
            self.load({"MAX_INPUT_CHARS": "100", "MAX_CONTEXT_CHARS": "99"})

    def test_model_cannot_be_empty_or_unbounded(self):
        for value in ("", " ", "a" * 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load({"OPENAI_MODEL": value})

    def test_explicit_configuration(self):
        cfg = self.load({"OPENAI_MODEL": " test-model ", "DATABASE_PATH": "local/test.sqlite3",
                         "MAX_DAILY_CALLS": "7", "MAX_OUTPUT_TOKENS": "32"})
        self.assertEqual(cfg.model, "test-model")
        self.assertEqual(cfg.db_path, "local/test.sqlite3")
        self.assertEqual(cfg.max_daily_calls, 7)
        self.assertEqual(cfg.max_output_tokens, 32)


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "nested" / "test.sqlite3")
        self.cfg = config(db_path=self.path)
        self.store = Store(self.path)
        self.now = 1704067200.0  # 2024-01-01 00:00:00 UTC
        self.telegram = Mock()
        self.openai = Mock()
        self.openai.answer.return_value = "A helpful answer."
        self.bot = self.make_bot()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make_bot(self):
        return Bot(self.cfg, self.store, self.telegram, self.openai, clock=lambda: self.now)

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.bot = self.make_bot()

    def ingest(self, text="Hello", uid=1, **kwargs):
        self.store.ingest([update(uid, text, **kwargs)], self.cfg)
        return self.store.next_job()

    def history(self, user=101, chat=101):
        return self.store.db.execute("SELECT * FROM history WHERE user_id=? AND chat_id=? ORDER BY id",
                                     (user, chat)).fetchall()

    def quota(self):
        row = self.store.db.execute("SELECT sum(calls) FROM quota").fetchone()
        return row[0] or 0

    def add_history(self, question, answer, user=101, chat=101):
        with self.store.db:
            self.store.db.execute("INSERT INTO history (user_id,chat_id,question,answer) VALUES (?,?,?,?)",
                                  (user, chat, question, answer))


class AuthorizationTests(StoreFixture):
    def test_sender_and_private_chat_must_both_match(self):
        cases = [dict(sender=999), dict(chat=999), dict(sender=101, chat=202),
                 dict(chat_type="group"), dict(chat_type="supergroup"), dict(chat_type="channel"),
                 dict(is_bot=True), dict(is_bot=None), dict(sender="101"), dict(chat="101"),
                 dict(sender=True, chat=True)]
        for index, change in enumerate(cases, 1):
            with self.subTest(change=change):
                self.store.ingest([update(index, "private", **change)], self.cfg)
                self.assertIsNone(self.store.next_job())
                self.assertEqual(self.store.offset, index + 1)

    def test_malformed_sender_chat_fail_closed(self):
        for field in ("from", "chat"):
            for value in (None, [], "101", 101, {}):
                message = update()["message"]
                message[field] = value
                with self.subTest(field=field, value=value):
                    self.assertFalse(Store.authorized(message, self.cfg))

    def test_missing_is_bot_is_denied(self):
        message = update()["message"]
        del message["from"]["is_bot"]
        self.assertFalse(Store.authorized(message, self.cfg))

    def test_only_new_message_events_are_queued(self):
        message = update()["message"]
        events = [{"update_id": i, key: message} for i, key in enumerate(
            ["edited_message", "channel_post", "callback_query", "edited_channel_post"], 1)]
        self.store.ingest(events, self.cfg)
        self.assertIsNone(self.store.next_job())
        self.assertEqual(self.store.offset, 5)

    def test_revoked_sender_drops_pending_work_after_restart(self):
        self.ingest()
        self.cfg = dataclasses.replace(self.cfg, allowed_users=frozenset({202}))
        self.restart()
        self.assertTrue(self.bot.process_one())
        self.openai.answer.assert_not_called()
        self.telegram.send.assert_not_called()
        self.assertIsNone(self.store.next_job())

    def test_revoked_chat_drops_saved_reply_after_restart(self):
        job = self.ingest()
        self.store.save_reply(job, "Already generated", self.cfg)
        self.cfg = dataclasses.replace(self.cfg, allowed_chats=frozenset({202}))
        self.restart()
        self.bot.process_one()
        self.telegram.send.assert_not_called()
        self.assertIsNone(self.store.next_job())


class IntakeTests(StoreFixture):
    def test_unordered_updates_are_sorted_and_deduplicated(self):
        self.store.ingest([update(3), update(1), update(2), update(2)], self.cfg)
        self.assertEqual(self.store.offset, 4)
        self.assertEqual([r[0] for r in self.store.db.execute("SELECT id FROM jobs ORDER BY id")], [1, 2, 3])
        self.assertEqual(self.store.next_job()["id"], 1)

    def test_intake_and_offset_survive_restart(self):
        self.ingest(uid=8)
        self.restart()
        self.assertEqual(self.store.offset, 9)
        self.assertEqual(self.store.next_job()["id"], 8)
        self.store.ingest([update(8)], self.cfg)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_old_updates_do_not_rewind_offset(self):
        self.ingest(uid=30)
        self.store.ingest([update(1), update(29)], self.cfg)
        self.assertEqual(self.store.offset, 31)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_invalid_batch_never_partially_advances_or_queues(self):
        for bad in ({}, {"update_id": "2"}, {"update_id": True}, [], None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.ingest([update(1), bad], self.cfg)
            self.assertEqual(self.store.offset, 0)
            self.assertIsNone(self.store.next_job())

    def test_database_error_rolls_back_offset_and_prior_insert(self):
        with self.assertRaises(OverflowError):
            self.store.ingest([update(1), update(2 ** 80)], self.cfg)
        self.assertEqual(self.store.offset, 0)
        self.assertIsNone(self.store.next_job())

    def test_malformed_message_is_ignored_but_acknowledged(self):
        self.store.ingest([{"update_id": 1, "message": None}], self.cfg)
        self.assertEqual(self.store.offset, 2)
        self.assertIsNone(self.store.next_job())

    def test_only_bounded_text_is_stored(self):
        self.cfg = dataclasses.replace(self.cfg, max_input_chars=10)
        record = update(1, "x" * 100)
        record["message"]["document"] = {"file_id": "must-not-be-stored"}
        self.store.ingest([record], self.cfg)
        job = self.store.next_job()
        self.assertEqual(job["text"], "x" * 11)
        self.assertNotIn("must-not-be-stored", str(dict(job)))

    def test_nontext_is_queued_as_empty_text(self):
        for index, text in enumerate((None, [], {}, 100), 1):
            self.store.ingest([update(index, text)], self.cfg)
        self.assertEqual([r[0] for r in self.store.db.execute("SELECT text FROM jobs")], [""] * 4)

    def test_finish_scrubs_payload_and_retains_dedup(self):
        job = self.ingest("private question", uid=1)
        self.store.save_reply(job, "private answer", self.cfg)
        self.store.finish(1)
        saved = self.store.db.execute("SELECT * FROM jobs WHERE id=1").fetchone()
        self.assertEqual(saved["text"], "")
        self.assertIsNone(saved["reply"])
        self.assertEqual(saved["done"], 1)
        self.restart()
        self.store.ingest([update(1, "private question")], self.cfg)
        self.assertIsNone(self.store.next_job())

    def test_tombstones_are_bounded_and_offset_still_prevents_replay(self):
        self.store.ingest([update(i) for i in range(1, 1006)], self.cfg)
        for i in range(1, 1006):
            self.store.finish(i)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1000)
        self.store.ingest([update(1)], self.cfg)
        self.assertIsNone(self.store.next_job())
        self.assertEqual(self.store.offset, 1006)

    def test_update_rejects_arbitrary_database_fields(self):
        for values in ({}, {"text": "new"}, {"done": 1}, {"cursor=0;DROP TABLE jobs;--": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.store.update(1, **values)


class ContextTests(StoreFixture):
    def test_context_is_chronological_complete_pairs(self):
        self.add_history("first", "answer1")
        self.add_history("second", "answer2")
        job = self.ingest("third")
        self.assertEqual(self.store.context(job, self.cfg), [
            {"role": "user", "content": "first"}, {"role": "assistant", "content": "answer1"},
            {"role": "user", "content": "second"}, {"role": "assistant", "content": "answer2"},
            {"role": "user", "content": "third"}])

    def test_context_never_leaks_another_sender_or_chat(self):
        self.add_history("secret-other-user", "answer", user=202, chat=202)
        self.add_history("secret-other-chat", "answer", user=101, chat=202)
        job = self.ingest("mine")
        self.assertEqual(self.store.context(job, self.cfg), [{"role": "user", "content": "mine"}])

    def test_context_respects_character_budget(self):
        self.cfg = dataclasses.replace(self.cfg, max_input_chars=10, max_context_chars=14)
        self.add_history("old", "1234")
        self.add_history("new", "5678")
        job = self.ingest("latest")
        context = self.store.context(job, self.cfg)
        self.assertEqual([m["content"] for m in context], ["new", "5678", "latest"])
        self.assertLessEqual(sum(len(m["content"]) for m in context), 14)

    def test_context_does_not_split_oversized_latest_pair(self):
        self.cfg = dataclasses.replace(self.cfg, max_input_chars=10, max_context_chars=12)
        self.add_history("old", "ok")
        self.add_history("recent", "large-answer")
        job = self.ingest("current")
        self.assertEqual(self.store.context(job, self.cfg), [{"role": "user", "content": "current"}])

    def test_history_turn_limit_and_save_pruning(self):
        self.cfg = dataclasses.replace(self.cfg, max_history_turns=2)
        for i in range(1, 5):
            job = self.ingest(f"question{i}", uid=i)
            self.store.save_reply(job, f"answer{i}", self.cfg, remember=True)
            self.store.finish(i)
        self.assertEqual([r["question"] for r in self.history()], ["question3", "question4"])
        job = self.ingest("latest", uid=5)
        self.assertEqual(len(self.store.context(job, self.cfg)), 5)

    def test_saving_reply_erases_pending_input_and_survives_restart(self):
        job = self.ingest("question")
        self.store.save_reply(job, "answer", self.cfg, remember=True)
        self.restart()
        self.assertEqual(self.store.next_job()["text"], "")
        self.assertEqual(self.store.next_job()["reply"], "answer")
        self.assertEqual(len(self.history()), 1)

    def test_reset_is_scoped_and_survives_restart(self):
        self.add_history("mine", "my-answer")
        self.add_history("theirs", "their-answer", user=202, chat=202)
        self.ingest("/reset")
        self.bot.process_one()
        self.restart()
        self.assertEqual(len(self.history()), 0)
        self.assertEqual(len(self.history(202, 202)), 1)
        self.openai.answer.assert_not_called()
        self.assertIn("doesn't delete Telegram messages", self.telegram.send.call_args.args[1])


class BotTests(StoreFixture):
    def test_idle_has_no_side_effects(self):
        self.assertFalse(self.bot.process_one())
        self.openai.answer.assert_not_called()
        self.telegram.send.assert_not_called()
        self.assertEqual(self.quota(), 0)

    def test_happy_path_remembers_and_finishes_once(self):
        self.ingest("Question")
        self.assertTrue(self.bot.process_one())
        self.assertFalse(self.bot.process_one())
        self.openai.answer.assert_called_once_with([{"role": "user", "content": "Question"}])
        self.telegram.send.assert_called_once_with(101, "A helpful answer.")
        self.assertEqual(self.quota(), 1)
        self.assertEqual(len(self.history()), 1)
        self.assertIsNone(self.store.next_job())

    def test_help_start_unknown_and_nontext_do_not_call_openai(self):
        for index, message in enumerate(("/help", "/start", "/unknown", "", "   ", None), 1):
            self.ingest(message, uid=index)
            self.bot.process_one()
        self.openai.answer.assert_not_called()
        self.assertEqual(self.telegram.send.call_count, 6)
        self.assertEqual(self.quota(), 0)
        self.assertEqual(len(self.history()), 0)
        self.assertIn("separate", HELP)
        self.assertIn("OpenAI", HELP)

    def test_oversized_input_is_rejected_without_spending(self):
        self.cfg = dataclasses.replace(self.cfg, max_input_chars=10)
        self.bot = self.make_bot()
        self.ingest("x" * 11)
        self.bot.process_one()
        self.openai.answer.assert_not_called()
        self.assertEqual(self.quota(), 0)
        self.assertIn("10 characters", self.telegram.send.call_args.args[1])

    def test_due_time_serializes_later_turns(self):
        self.store.ingest([update(1), update(2, "/reset")], self.cfg)
        self.store.update(1, due=self.now + 60)
        self.assertFalse(self.bot.process_one())
        self.assertEqual(self.store.next_job()["id"], 1)
        self.openai.answer.assert_not_called()
        self.telegram.send.assert_not_called()

    def test_retryable_generation_failure_persists_attempt_due_and_quota(self):
        self.ingest()
        self.openai.answer.side_effect = APIError("openai", 429, True, 30)
        self.bot.process_one()
        job = self.store.next_job()
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(job["due"], self.now + 30)
        self.assertIsNone(job["reply"])
        self.assertEqual(self.quota(), 1)
        self.restart()
        self.assertFalse(self.bot.process_one())
        self.now += 30
        self.openai.answer.side_effect = None
        self.bot.process_one()
        self.assertEqual(self.quota(), 2)
        self.assertEqual(self.openai.answer.call_count, 2)
        self.assertIsNone(self.store.next_job())

    def test_generation_retries_are_bounded_across_restarts(self):
        self.ingest()
        self.openai.answer.side_effect = APIError("openai", 503, True)
        for i in range(self.cfg.max_attempts):
            self.bot.process_one()
            self.now += 100
            self.restart()
        self.assertEqual(self.openai.answer.call_count, self.cfg.max_attempts)
        self.assertEqual(self.quota(), self.cfg.max_attempts)
        self.assertIsNone(self.store.next_job())
        self.assertEqual(len(self.history()), 0)
        self.telegram.send.assert_called_once()

    def test_nonretryable_generation_failure_is_not_retried(self):
        self.ingest()
        self.openai.answer.side_effect = APIError("openai", 401)
        self.bot.process_one()
        self.openai.answer.assert_called_once()
        self.assertIsNone(self.store.next_job())
        self.assertEqual(len(self.history()), 0)

    def test_reserved_crashed_attempts_count_on_restart(self):
        self.ingest()
        for _ in range(self.cfg.max_attempts):
            self.assertTrue(self.store.reserve_call(1, 50, self.now))
        self.restart()
        self.bot.process_one()
        self.openai.answer.assert_not_called()
        self.assertEqual(self.quota(), self.cfg.max_attempts)
        self.assertIsNone(self.store.next_job())

    def test_daily_quota_is_global_durable_and_resets_at_utc_midnight(self):
        self.cfg = dataclasses.replace(self.cfg, max_daily_calls=1)
        self.bot = self.make_bot()
        self.ingest(uid=1)
        self.bot.process_one()
        self.restart()
        self.ingest(uid=2, sender=202, chat=202)
        self.bot.process_one()
        self.assertEqual(self.openai.answer.call_count, 1)
        self.assertIn("midnight UTC", self.telegram.send.call_args.args[1])
        self.now += 86400
        self.ingest(uid=3, sender=202, chat=202)
        self.bot.process_one()
        self.assertEqual(self.openai.answer.call_count, 2)
        self.assertEqual(self.quota(), 2)

    def test_saved_reply_is_not_regenerated_after_restart(self):
        job = self.ingest()
        self.store.save_reply(job, "saved reply", self.cfg, remember=True)
        self.restart()
        self.bot.process_one()
        self.openai.answer.assert_not_called()
        self.telegram.send.assert_called_once_with(101, "saved reply")
        self.assertEqual(len(self.history()), 1)

    def test_chunk_send_resumes_at_unsent_chunk_after_restart(self):
        self.openai.answer.return_value = "a" * 4000 + "b" * 4000 + "c" * 12
        self.telegram.send.side_effect = [None, APIError("telegram", 429, True, 20)]
        self.ingest()
        self.bot.process_one()
        pending = self.store.next_job()
        self.assertEqual(pending["cursor"], 1)
        self.assertEqual(pending["send_attempts"], 1)
        self.assertEqual(pending["due"], self.now + 20)
        self.restart()
        self.assertFalse(self.bot.process_one())
        self.now += 20
        self.telegram.send.side_effect = None
        self.bot.process_one()
        sent = [call.args[1] for call in self.telegram.send.call_args_list]
        self.assertEqual(sent, ["a" * 4000, "b" * 4000, "b" * 4000, "c" * 12])
        self.openai.answer.assert_called_once()
        self.assertEqual(len(self.history()), 1)
        self.assertIsNone(self.store.next_job())

    def test_delivery_retries_bounded_across_restarts(self):
        self.ingest()
        self.telegram.send.side_effect = APIError("telegram", 500, True)
        for i in range(self.cfg.max_attempts):
            self.bot.process_one()
            self.now += 100
            self.restart()
        self.assertEqual(self.telegram.send.call_count, self.cfg.max_attempts)
        self.openai.answer.assert_called_once()
        self.assertIsNone(self.store.next_job())

    def test_delivery_crash_consumes_attempt_before_network(self):
        self.ingest()
        self.telegram.send.side_effect = RuntimeError("simulated process death")
        with self.assertRaises(RuntimeError):
            self.bot.process_one()
        self.restart()
        pending = self.store.next_job()
        self.assertEqual(pending["send_attempts"], 1)
        self.assertEqual(pending["cursor"], 0)
        self.assertIsNotNone(pending["reply"])
        self.telegram.send.side_effect = None
        self.bot.process_one()
        self.openai.answer.assert_called_once()

    def test_exhausted_crashed_delivery_is_not_sent_again(self):
        job = self.ingest()
        self.store.save_reply(job, "saved", self.cfg)
        self.store.update(1, send_attempts=self.cfg.max_attempts)
        self.restart()
        self.bot.process_one()
        self.telegram.send.assert_not_called()
        self.assertIsNone(self.store.next_job())

    def test_nonretryable_send_failure_discards_pending_reply(self):
        self.ingest()
        self.telegram.send.side_effect = APIError("telegram", 403)
        self.bot.process_one()
        self.assertIsNone(self.store.next_job())
        self.telegram.send.assert_called_once()

    def test_auth_and_conflict_delivery_errors_propagate_for_safe_shutdown(self):
        for code in (401, 409):
            with self.subTest(code=code):
                if self.store.next_job() is None:
                    self.ingest()
                self.telegram.send.side_effect = APIError("telegram", code)
                with self.assertRaises(APIError) as caught:
                    self.bot.process_one()
                self.assertEqual(caught.exception.code, code)


class ChunkTests(unittest.TestCase):
    def test_empty_and_boundary_text(self):
        self.assertEqual(chunks(""), [])
        self.assertEqual(chunks("a" * 4000), ["a" * 4000])
        self.assertEqual(chunks("a" * 4001), ["a" * 4000, "a"])

    def test_astral_emoji_are_not_split_and_respect_utf16_limit(self):
        text = "a" * 3999 + "\U0001f600" + "\U0001f680" * 2001 + "ending"
        parts = chunks(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 4000 for p in parts))
        self.assertEqual(parts[0], "a" * 3999)


class TelegramTests(unittest.TestCase):
    def test_updates_uses_persistent_offset_and_message_only_filter(self):
        transport = Mock(return_value={"ok": True, "result": [update(7)]})
        client = Telegram(TOKEN, transport)
        self.assertEqual(client.updates(7), [update(7)])
        url, payload = transport.call_args.args
        self.assertTrue(url.endswith("/getUpdates"))
        self.assertEqual(payload, {"offset": 7, "timeout": 30, "limit": 100, "allowed_updates": ["message"]})

    def test_send_is_plain_text_with_previews_disabled(self):
        transport = Mock(return_value={"ok": True, "result": {"message_id": 1}})
        Telegram(TOKEN, transport).send(101, "**literal** <text>")
        url, payload = transport.call_args.args
        self.assertTrue(url.endswith("/sendMessage"))
        self.assertEqual(payload["text"], "**literal** <text>")
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(payload["link_preview_options"], {"is_disabled": True})

    def test_service_errors_keep_only_safe_status_and_retry_delay(self):
        transport = Mock(return_value={"ok": False, "error_code": 429,
                                      "description": "raw body secret", "parameters": {"retry_after": 45}})
        with self.assertRaises(APIError) as caught:
            Telegram(TOKEN, transport).send(101, "hello")
        exc = caught.exception
        self.assertEqual(exc.service, "telegram")
        self.assertEqual(exc.code, 429)
        self.assertEqual(exc.retry_after, 45)
        self.assertTrue(exc.retryable)
        self.assertNotIn("raw body secret", str(exc))
        self.assertNotIn(TOKEN, str(exc))

    def test_transport_error_relabelled_without_sensitive_details(self):
        transport = Mock(side_effect=APIError("remote", "network", True, 4))
        with self.assertRaises(APIError) as caught:
            Telegram(TOKEN, transport).send(101, "hello")
        self.assertEqual(caught.exception.service, "telegram")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 4)

    def test_bad_response_and_updates_are_controlled_errors(self):
        for response in (None, [], "bad"):
            with self.subTest(response=response), self.assertRaises(APIError):
                Telegram(TOKEN, Mock(return_value=response)).updates(0)
        with self.assertRaises(APIError):
            Telegram(TOKEN, Mock(return_value={"ok": True, "result": {}})).updates(0)

    def test_malformed_error_parameters_do_not_crash_worker(self):
        for parameters in ("bad", [1], 123):
            with self.subTest(parameters=parameters), self.assertRaises(APIError):
                Telegram(TOKEN, Mock(return_value={"ok": False, "error_code": 429,
                                                  "parameters": parameters})).updates(0)


class OpenAITests(unittest.TestCase):
    def response(self, text="Answer", status="completed"):
        return {"status": status, "output": [{"type": "message", "content": [
            {"type": "output_text", "text": text}]}]}

    def test_responses_request_has_privacy_and_no_action_tools(self):
        transport = Mock(return_value=self.response())
        cfg = config()
        messages = [{"role": "user", "content": "Question"}]
        self.assertEqual(OpenAI(cfg, transport).answer(messages), "Answer")
        url, payload = transport.call_args.args
        self.assertEqual(url, "https://api.openai.com/v1/responses")
        self.assertEqual(payload["model"], cfg.model)
        self.assertEqual(payload["max_output_tokens"], cfg.max_output_tokens)
        self.assertEqual(payload["input"], messages)
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["tools"], [])
        self.assertEqual(payload["instructions"], INSTRUCTIONS)
        self.assertIn("separate from ChatGPT", INSTRUCTIONS)
        self.assertNotIn("previous_response_id", payload)
        self.assertEqual(transport.call_args.kwargs["headers"], {"Authorization": f"Bearer {KEY}"})

    def test_text_refusals_and_multiple_messages_are_read(self):
        result = {"status": "completed", "output": [
            {"type": "reasoning", "content": [{"type": "output_text", "text": "not visible"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "First"},
                                             {"type": "refusal", "refusal": "Refusal"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "Last"}]}]}
        self.assertEqual(OpenAI(config(), Mock(return_value=result)).answer([]), "First\nRefusal\nLast")

    def test_incomplete_reply_is_labelled(self):
        answer = OpenAI(config(), Mock(return_value=self.response(status="incomplete"))).answer([])
        self.assertTrue(answer.startswith("Answer"))
        self.assertIn("stopped", answer)

    def test_empty_output_has_safe_fallback(self):
        for response in ({"status": "completed", "output": []}, self.response("  ")):
            with self.subTest(response=response):
                self.assertIn("couldn't produce", OpenAI(config(), Mock(return_value=response)).answer([]))

    def test_output_is_independently_bounded(self):
        answer = OpenAI(config(), Mock(return_value=self.response("x" * 40000))).answer([])
        self.assertEqual(len(answer), 32000)

    def test_invalid_status_or_top_level_response_is_controlled(self):
        for response in (None, [], "bad", {}, {"status": "failed"}, {"status": "in_progress"}):
            with self.subTest(response=response), self.assertRaises(APIError):
                OpenAI(config(), Mock(return_value=response)).answer([])

    def test_malformed_output_collections_do_not_crash_worker(self):
        for response in ({"status": "completed", "output": None},
                         {"status": "completed", "output": 123},
                         {"status": "completed", "output": [{"type": "message", "content": None}]},
                         {"status": "completed", "output": [{"type": "message", "content": 123}]}):
            with self.subTest(response=response), self.assertRaises(APIError):
                OpenAI(config(), Mock(return_value=response)).answer([])

    def test_transport_errors_are_relabelled(self):
        with self.assertRaises(APIError) as caught:
            OpenAI(config(), Mock(side_effect=APIError("remote", 503, True, 2))).answer([])
        self.assertEqual(caught.exception.service, "openai")
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(caught.exception.retry_after, 2)


class HTTPTransportTests(unittest.TestCase):
    def response(self, raw):
        response = Mock()
        response.read.return_value = raw
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_json_request_and_size_limited_response_no_real_network(self):
        response = self.response(b'{"ok": true}')
        opener = Mock()
        opener.open.return_value = response
        with patch("urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(post_json("https://example.invalid/test", {"text": "hello"},
                                       {"Authorization": "Bearer fake"}, timeout=7), {"ok": True})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"text": "hello"})
        self.assertEqual(request.get_header("Authorization"), "Bearer fake")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)
        response.read.assert_called_once_with(2_000_001)
        no_redirect = build.call_args.args[0]()
        self.assertIsNone(no_redirect.redirect_request(request, None, 302, "Found", {}, "https://other.invalid"))

    def test_oversized_response_is_rejected(self):
        opener = Mock()
        opener.open.return_value = self.response(b" " * 2_000_001)
        with patch("urllib.request.build_opener", return_value=opener), self.assertRaises(APIError) as caught:
            post_json("https://example.invalid", {})
        self.assertEqual(caught.exception.code, "oversize")

    def test_invalid_json_and_unicode_are_controlled(self):
        for raw in (b"not-json", b"\xff"):
            opener = Mock()
            opener.open.return_value = self.response(raw)
            with self.subTest(raw=raw), patch("urllib.request.build_opener", return_value=opener):
                with self.assertRaises(APIError) as caught:
                    post_json("https://example.invalid", {})
                self.assertEqual(caught.exception.code, "invalid_json")
                self.assertTrue(caught.exception.retryable)

    def test_network_error_is_sanitized(self):
        for error in (urllib.error.URLError("secret URL"), TimeoutError("secret"), ConnectionError("secret")):
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(error=type(error)), patch("urllib.request.build_opener", return_value=opener):
                with self.assertRaises(APIError) as caught:
                    post_json("https://example.invalid/secret-token", {})
                self.assertEqual(caught.exception.code, "network")
                self.assertNotIn("secret", str(caught.exception))

    def test_http_errors_have_status_retry_policy_and_clamped_delay(self):
        for code, expected in ((301, False), (401, False), (429, True), (503, True)):
            error = urllib.error.HTTPError("https://secret.invalid", code, "secret-message",
                                           {"Retry-After": "999999"}, io.BytesIO(b"secret-body"))
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(code=code), patch("urllib.request.build_opener", return_value=opener):
                with self.assertRaises(APIError) as caught:
                    post_json("https://example.invalid", {})
                self.assertEqual(caught.exception.code, code)
                self.assertIs(caught.exception.retryable, expected)
                self.assertEqual(caught.exception.retry_after, 3600)
                self.assertNotIn("secret", str(caught.exception))

    def test_retry_after_is_bounded(self):
        self.assertEqual(APIError("remote", 429, True, -1).retry_after, 0)
        self.assertEqual(APIError("remote", 429, True, 99999).retry_after, 3600)


class MainTests(unittest.TestCase):
    def test_invalid_config_exits_before_clients_are_constructed(self):
        with patch("telegram_assistant.app.Config.from_env", side_effect=ValueError("Missing allowlist")), \
             patch("telegram_assistant.app.Telegram") as telegram, \
             patch("telegram_assistant.app.OpenAI") as openai, \
             patch("telegram_assistant.app.os.umask"), \
             self.assertLogs("telegram_assistant", level="ERROR") as logs:
            self.assertEqual(main(), 2)
        telegram.assert_not_called()
        openai.assert_not_called()
        self.assertIn("Configuration error", " ".join(logs.output))

    def test_unexpected_failure_logs_no_raw_exception_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(db_path=str(Path(tmp) / "db.sqlite3"))
            telegram = Mock()
            telegram.updates.side_effect = RuntimeError("secret-token-and-message")
            with patch("telegram_assistant.app.Config.from_env", return_value=cfg), \
                 patch("telegram_assistant.app.Telegram", return_value=telegram), \
                 patch("telegram_assistant.app.OpenAI"), \
                 patch("telegram_assistant.app.signal.signal"), \
                 patch("telegram_assistant.app.os.umask"), \
                 self.assertLogs("telegram_assistant", level="ERROR") as logs:
                self.assertEqual(main(), 1)
            self.assertNotIn("secret-token-and-message", " ".join(logs.output))
            self.assertIn("stopped safely", " ".join(logs.output))

    def test_database_lock_blocks_second_worker(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(db_path=str(Path(tmp) / "db.sqlite3"))
            with open(cfg.db_path + ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch("telegram_assistant.app.Config.from_env", return_value=cfg), \
                     patch("telegram_assistant.app.Telegram") as telegram, \
                     patch("telegram_assistant.app.os.umask"), \
                     self.assertLogs("telegram_assistant", level="ERROR"):
                    self.assertEqual(main(), 2)
                telegram.assert_not_called()


if __name__ == "__main__":
    unittest.main()
