"""Offline transport tests: DNS, sockets and TLS are all injected mocks."""
import base64
import hashlib
import hmac
import json
import socket
import ssl
import unittest
import threading
import time
from unittest.mock import Mock, patch

from telegram_events.network import CallbackClient, MAX_BODY, MAX_RESPONSE, _DeadlineReader, _public_address, _resolve, validate_secret

SECRET = "whsec_" + base64.b64encode(b"a" * 32).decode()
OLD = "whsec_" + base64.b64encode(b"b" * 32).decode()
URL = "https://callbacks.example.com/events?token=opaque"
PUBLIC = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))


class WireSocket:
    def __init__(self, response):
        self.response = response
        self.sent = b""
        self.closed = False
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, data):
        self.sent += data

    def recv_into(self, buffer, count):
        data, self.response = self.response[:count], self.response[count:]
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        self.closed = True


def response(body=b"{}", status=200, extra=b""):
    return (f"HTTP/1.1 {status} Result\r\nContent-Length: {len(body)}\r\n".encode()
            + extra + b"\r\n" + body)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.client = CallbackClient({"callbacks.example.com"})

    def run_wire(self, data, **kwargs):
        wire, raw = WireSocket(data), Mock()
        context = Mock()
        context.wrap_socket.return_value = wire
        with patch("telegram_events.network.socket.getaddrinfo", return_value=[PUBLIC]) as dns, \
                patch("telegram_events.network.socket.socket", return_value=raw) as factory, \
                patch("telegram_events.network.ssl.create_default_context", return_value=context), \
                patch("telegram_events.network.time.time", return_value=1234):
            result = self.client.post(URL, SECRET, "sub_1", "evt_1", b'{"x":1}', **kwargs)
        return result, wire, raw, context, dns, factory

    def test_pinned_tls_and_standard_webhooks_rotation(self):
        result, wire, raw, context, dns, factory = self.run_wire(response(), old_secret=OLD)
        self.assertEqual(result, (200, b"{}"))
        dns.assert_called_once_with("callbacks.example.com", 443, type=socket.SOCK_STREAM)
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        raw.connect.assert_called_once_with(("8.8.8.8", 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname="callbacks.example.com")
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        head, body = wire.sent.split(b"\r\n\r\n", 1)
        self.assertEqual(body, b'{"x":1}')
        self.assertTrue(head.startswith(b"POST /events?token=opaque HTTP/1.1\r\n"))
        signatures = []
        for key in (b"a" * 32, b"b" * 32):
            signatures.append("v1," + base64.b64encode(hmac.new(key, b'evt_1.1234.{"x":1}', hashlib.sha256).digest()).decode())
        self.assertIn(("webhook-signature: " + " ".join(signatures)).encode(), head)
        self.assertIn(b"X-MCP-Subscription-Id: sub_1", head)
        self.assertTrue(wire.closed)
        raw.close.assert_called_once()
        self.assertTrue(all(0 < timeout <= 10 for timeout in wire.timeouts))

    def test_url_rejections(self):
        for url in ["http://callbacks.example.com/", "https://callbacks.example.com:8443/",
                    "https://callbacks.example.com:0443/", "https://callbacks.example.com./",
                    "https://other.example.com/", "https://callbacks.example.com.evil.test/",
                    "https://x@callbacks.example.com/", "https://callbacks.example.com/#",
                    "https://callbacks.example.com/\nfoo", " https://callbacks.example.com/",
                    "https://callbacks.example.com/\\foo", "https://127.0.0.1/",
                    "https://[::1]/", "https://callbacks.example.com/é", "https://%63allbacks.example.com/"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.client.validate(url)
        self.client.validate(URL)
        self.client.validate("https://callbacks.example.com:443/")

    def test_invalid_allowlists(self):
        for hosts in [set(), {"*.example.com"}, {"EXAMPLE.com"}, {"127.0.0.1"}, {"localhost"}]:
            with self.subTest(hosts=hosts), self.assertRaises(ValueError):
                CallbackClient(hosts)

    def test_secret_validation(self):
        self.assertEqual(validate_secret(SECRET), b"a" * 32)
        for key in [None, "", "raw_secret", "whsec_" + "!" * 32,
                    "whsec_" + base64.b64encode(b"x" * 23).decode(),
                    "whsec_" + base64.b64encode(b"x" * 65).decode()]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_secret(key)

    def test_public_only_all_addresses_checked(self):
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1",
                        "192.168.0.1", "192.0.0.8", "192.88.99.1", "0.0.0.0", "224.0.0.1", "240.0.0.1", "::1", "fc00::1",
                        "fe80::1", "ff0e::1", "::ffff:8.8.8.8", "2002:0808:0808::1", "64:ff9b::808:808"]:
            self.assertFalse(_public_address(address), address)
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
            records = [PUBLIC, (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]
            with patch("telegram_events.network.socket.getaddrinfo", return_value=records), \
                    patch("telegram_events.network.socket.socket") as create, self.assertRaises(ValueError):
                self.client.post(URL, SECRET, "sub", "evt", b"{}")
            create.assert_not_called()
        self.assertTrue(_public_address("2606:4700:4700::1111"))

    def test_dns_rechecked_for_every_delivery(self):
        wire, raw, context = WireSocket(response()), Mock(), Mock()
        context.wrap_socket.return_value = wire
        private = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
        with patch("telegram_events.network.socket.getaddrinfo", side_effect=[[PUBLIC], [private]]) as dns, \
                patch("telegram_events.network.socket.socket", return_value=raw) as create, \
                patch("telegram_events.network.ssl.create_default_context", return_value=context):
            self.client.post(URL, SECRET, "sub", "evt", b"{}")
            with self.assertRaises(ValueError):
                self.client.post(URL, SECRET, "sub", "evt", b"{}")
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(create.call_count, 1)

    def test_get_uses_same_pinned_transport(self):
        wire, raw, context = WireSocket(response(b'{"keys":[]}')), Mock(), Mock()
        context.wrap_socket.return_value = wire
        with patch("telegram_events.network.socket.getaddrinfo", return_value=[PUBLIC]), \
                patch("telegram_events.network.socket.socket", return_value=raw), \
                patch("telegram_events.network.ssl.create_default_context", return_value=context):
            result = self.client.get(URL)
        self.assertEqual(result, (200, b'{"keys":[]}'))
        self.assertTrue(wire.sent.startswith(b"GET /events?token=opaque HTTP/1.1\r\n"))
        raw.connect.assert_called_once_with(("8.8.8.8", 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname="callbacks.example.com")

    def test_redirect_not_followed(self):
        result, _, _, _, dns, factory = self.run_wire(response(status=302, extra=b"Location: https://127.0.0.1/\r\n"))
        self.assertEqual(result[0], 302)
        self.assertEqual(dns.call_count, 1)
        self.assertEqual(factory.call_count, 1)

    def test_body_and_identifier_limits_before_network(self):
        for body, event in [(b"a" * (MAX_BODY + 1), "evt"), ("{}", "evt"), (b"{}", "bad\r\nheader")]:
            with patch("telegram_events.network.socket.getaddrinfo") as dns, self.assertRaises(ValueError):
                self.client.post(URL, SECRET, "sub", event, body)
            dns.assert_not_called()

    def test_response_limit(self):
        with self.assertRaises(ValueError):
            self.run_wire(response(b"a" * (MAX_RESPONSE + 1)))
        with self.assertRaises(ValueError):
            self.run_wire(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"a" * (MAX_RESPONSE + 1))

    def test_truncated_response_rejected(self):
        with self.assertRaises(ValueError):
            self.run_wire(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{}")

    def test_wire_deadline_checked_for_each_read(self):
        wire = WireSocket(b"abc")
        reader = _DeadlineReader(wire, deadline=100)
        with patch("telegram_events.network.time.monotonic", side_effect=[99, 101]):
            self.assertEqual(reader.readinto(bytearray(1)), 1)
            with self.assertRaises(TimeoutError):
                reader.readinto(bytearray(1))
        self.assertEqual(wire.response, b"bc")

    def test_dns_timeout_is_bounded(self):
        release = threading.Event()
        finished = threading.Event()
        def delayed(*args, **kwargs):
            try:
                release.wait(1)
                return [PUBLIC]
            finally:
                finished.set()
        try:
            with patch("telegram_events.network.socket.getaddrinfo", side_effect=delayed):
                with self.assertRaises(TimeoutError):
                    _resolve("callbacks.example.com", time.monotonic() + 0.01)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_verify_signed_control_and_fresh_challenges(self):
        challenges, identifiers = [], []
        def echo(url, secret, subscription, event, body):
            value = json.loads(body)
            self.assertEqual(value["type"], "verification")
            self.assertEqual((url, secret, subscription), (URL, SECRET, "sub"))
            challenges.append(value["challenge"])
            identifiers.append(event)
            return 200, json.dumps({"challenge": value["challenge"]}).encode()
        with patch.object(self.client, "post", side_effect=echo):
            self.client.verify(URL, SECRET, "sub")
            self.client.verify(URL, SECRET, "sub")
        self.assertNotEqual(challenges[0], challenges[1])
        self.assertNotEqual(identifiers[0], identifiers[1])

    def test_verify_rejects_bad_response(self):
        for result in [(200, b'{}'), (200, b'[]'), (200, b'bad'), (200, b'{"challenge":12}'),
                       (200, b'{"challenge":"wrong"}'), (302, b'{}')]:
            with patch.object(self.client, "post", return_value=result), self.assertRaises(ValueError):
                self.client.verify(URL, SECRET, "sub")
        with patch.object(self.client, "post", side_effect=TimeoutError()), self.assertRaises(ValueError):
            self.client.verify(URL, SECRET, "sub")

    def test_tls_certificate_failure_is_not_downgraded(self):
        raw = Mock()
        context = Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("untrusted")
        with patch("telegram_events.network.socket.getaddrinfo", return_value=[PUBLIC]), \
                patch("telegram_events.network.socket.socket", return_value=raw), \
                patch("telegram_events.network.ssl.create_default_context", return_value=context), \
                self.assertRaises(ssl.SSLCertVerificationError):
            self.client.post(URL, SECRET, "sub", "evt", b"{}")
        raw.close.assert_called_once()
        raw.sendall.assert_not_called()


if __name__ == "__main__":
    unittest.main()
