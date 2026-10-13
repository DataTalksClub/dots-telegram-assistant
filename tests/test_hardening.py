"""Focused tests for shutdown, ID lookup, and transient HTTP hardening."""
import http.client
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from telegram_assistant import identify
from telegram_assistant.api import APIError, OpenAI, Telegram, post_json
from telegram_assistant.app import Bot
from test_service import ENV, TOKEN, StoreFixture, config, update


class ShutdownTests(StoreFixture):
    def test_stop_before_processing_never_generates_or_sends(self):
        self.ingest()
        bot = Bot(self.cfg, self.store, self.telegram, self.openai,
                  clock=lambda: self.now, stop_requested=lambda: True)
        self.assertFalse(bot.process_one())
        self.openai.answer.assert_not_called()
        self.telegram.send.assert_not_called()
        self.assertEqual(self.quota(), 0)
        self.assertEqual(self.store.next_job()["text"], "Hello")

    def test_stop_between_chunks_keeps_cursor_for_restart(self):
        self.ingest()
        self.openai.answer.return_value = "a" * 4000 + "b" * 5
        state = {"stop": False}
        def stop_after_send(*_args):
            state["stop"] = True
        self.telegram.send.side_effect = stop_after_send
        bot = Bot(self.cfg, self.store, self.telegram, self.openai,
                  clock=lambda: self.now, stop_requested=lambda: state["stop"])
        self.assertFalse(bot.process_one())
        self.assertEqual(self.store.next_job()["cursor"], 1)
        self.assertEqual(self.store.next_job()["send_attempts"], 0)
        self.restart()
        self.telegram.send.side_effect = None
        self.bot.process_one()
        self.assertEqual([call.args[1] for call in self.telegram.send.call_args_list], ["a" * 4000, "b" * 5])
        self.openai.answer.assert_called_once()
        self.assertIsNone(self.store.next_job())


class IdentifyTests(unittest.TestCase):
    def run_lookup(self, batches, times=(0, 0, 121)):
        client = Mock()
        client.updates.side_effect = batches
        output = io.StringIO()
        with patch.dict("os.environ", ENV, clear=True), \
             patch("telegram_assistant.identify.secrets.token_hex", return_value="local-nonce"), \
             patch("telegram_assistant.identify.Telegram", return_value=client), \
             patch("telegram_assistant.identify.time.monotonic", side_effect=times), \
             patch("telegram_assistant.identify.time.sleep"), \
             patch("sys.stdout", output):
            status = identify.main()
        client.send.assert_not_called()
        return status, output.getvalue(), client

    def test_invalid_token_stops_without_client_or_lookup(self):
        with patch.dict("os.environ", {}, clear=True), patch("sys.stdout", io.StringIO()), \
             patch("telegram_assistant.identify.Telegram") as constructor:
            self.assertEqual(identify.main(), 2)
        constructor.assert_not_called()

    def test_exact_nonce_from_private_human_chat_yields_only_matching_ids(self):
        status, output, client = self.run_lookup([[update(1, "/pair local-nonce")]])
        self.assertEqual(status, 0)
        self.assertIn("ALLOWED_TELEGRAM_USER_IDS=101", output)
        self.assertIn("ALLOWED_TELEGRAM_CHAT_IDS=101", output)
        self.assertNotIn(TOKEN, output)
        client.updates.assert_called_once_with(0, timeout=5)

    def test_unrelated_and_malformed_updates_are_ignored(self):
        bad = [None, [], "invalid", {}, {"message": None}, {"message": {"from": [], "chat": {}}},
               update(1, "/pair wrong"), update(2, "/pair local-nonce", chat_type="group"),
               update(3, "/pair local-nonce", is_bot=True),
               update(4, "/pair local-nonce", sender=101, chat=202),
               update(5, "/pair local-nonce", sender="101", chat="101")]
        status, output, client = self.run_lookup([bad])
        self.assertEqual(status, 1)
        self.assertNotIn("ALLOWED_TELEGRAM_USER_IDS=", output)
        self.assertIn("Timed out", output)
        client.updates.assert_called_once_with(0, timeout=5)

    def test_lookup_never_acknowledges_pending_updates(self):
        status, output, client = self.run_lookup([[update(1, "old")], [update(2, "/pair local-nonce")]],
                                                times=(0, 0, 1))
        self.assertEqual(status, 0)
        self.assertEqual([call.args[0] for call in client.updates.call_args_list], [0, 0])

    def test_controlled_api_failure_is_safe_and_actionable(self):
        status, output, client = self.run_lookup([APIError("telegram", 409)])
        self.assertEqual(status, 1)
        self.assertIn("409", output)
        self.assertIn("competing", output)
        self.assertNotIn(TOKEN, output)


class ErrorHardeningTests(unittest.TestCase):
    def http_error(self, code, headers=None, raw=b"{}"):
        error = urllib.error.HTTPError("https://secret.invalid", code, "private failure", headers or {}, io.BytesIO(raw))
        opener = Mock()
        opener.open.side_effect = error
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(APIError) as caught:
                post_json("https://example.invalid", {})
        return caught.exception

    def test_http_408_is_retryable(self):
        self.assertTrue(self.http_error(408).retryable)

    def test_429_json_retry_after_is_used_without_header(self):
        exc = self.http_error(429, raw=json.dumps({"parameters": {"retry_after": 32}}).encode())
        self.assertEqual(exc.retry_after, 32)
        self.assertTrue(exc.retryable)

    def test_429_header_retry_after_takes_precedence(self):
        exc = self.http_error(429, headers={"Retry-After": "12"},
                              raw=json.dumps({"parameters": {"retry_after": 32}}).encode())
        self.assertEqual(exc.retry_after, 12)

    def test_429_malformed_body_or_parameters_is_safe(self):
        for raw in (b"invalid", b"\xff", b"[]", b'{"parameters":"invalid"}',
                    b'{"parameters":{"retry_after":"secret"}}',
                    b'{"parameters":{"retry_after":true}}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.http_error(429, raw=raw).retry_after, 0)

    def test_incomplete_http_body_is_retryable_without_leaking_payload(self):
        opener = Mock()
        opener.open.side_effect = http.client.IncompleteRead(b"secret-body")
        with patch("urllib.request.build_opener", return_value=opener), self.assertRaises(APIError) as caught:
            post_json("https://example.invalid", {})
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.code, "network")
        self.assertNotIn("secret", str(caught.exception))

    def test_openai_409_is_retryable_but_telegram_409_is_terminal(self):
        transport = Mock(side_effect=APIError("remote", 409))
        with self.assertRaises(APIError) as caught:
            OpenAI(config(), transport).answer([])
        self.assertTrue(caught.exception.retryable)
        with self.assertRaises(APIError) as caught:
            Telegram(TOKEN, transport).updates(0)
        self.assertFalse(caught.exception.retryable)

    def test_telegram_arbitrary_error_code_cannot_leak_payload(self):
        for code in ("secret-token", {"secret": "token"}, ["secret"], True, None):
            transport = Mock(return_value={"ok": False, "error_code": code})
            with self.subTest(code=code), self.assertRaises(APIError) as caught:
                Telegram(TOKEN, transport).updates(0)
            self.assertEqual(caught.exception.code, "unknown")
            self.assertNotIn("secret", str(caught.exception))

    def test_telegram_non_boolean_success_is_not_trusted(self):
        for success in ("false", "true", 1, [], {}):
            transport = Mock(return_value={"ok": success, "result": []})
            with self.subTest(success=success), self.assertRaises(APIError):
                Telegram(TOKEN, transport).updates(0)


if __name__ == "__main__":
    unittest.main()
