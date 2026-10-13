"""Bounded, SSRF-resistant HTTPS transport for signed MCP event callbacks.

No environment proxy configuration, redirects, connection reuse, or insecure TLS
modes are supported. The operator supplies exact trusted callback DNS hostnames.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import queue
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

MAX_BODY = 256 * 1024
MAX_RESPONSE = 64 * 1024
MAX_WIRE_RESPONSE = 128 * 1024
_DNS_SLOTS = threading.BoundedSemaphore(4)
_HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


def validate_secret(secret: str) -> bytes:
    """Decode a Standard Webhooks secret; reject malformed or weak keys."""
    if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 100:
        raise ValueError("invalid signing secret")
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid signing secret") from None
    if not 24 <= len(key) <= 64:
        raise ValueError("signing key must contain 24 to 64 bytes")
    return key


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("callback deadline exceeded")
    return remaining


def _public_address(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        # Keep special-purpose handling stable across Python ipaddress versions.
        if (address in ipaddress.ip_network("192.0.0.0/24")
                or address in ipaddress.ip_network("192.88.99.0/24")):
            return False
    if isinstance(address, ipaddress.IPv6Address):
        # Reject translation/tunneling ranges, including public IPv4 embeddings.
        if address not in ipaddress.ip_network("2000::/3"):
            return False
        if address in ipaddress.ip_network("2001::/23") or address.sixtofour or address.teredo:
            return False
    return True


def _resolve(host: str, deadline: float) -> list:
    # getaddrinfo itself has no stdlib timeout. Use a bounded number of daemon
    # workers, so a stalled system resolver cannot exhaust threads or block exit.
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("callback DNS busy")
    result: queue.Queue = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put((socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM), None))
        except Exception as exc:
            result.put((None, exc))
        finally:
            _DNS_SLOTS.release()

    try:
        threading.Thread(target=resolve, daemon=True).start()
    except Exception:
        _DNS_SLOTS.release()
        raise
    try:
        records, error = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TimeoutError("callback DNS timeout") from None
    if error:
        raise OSError("callback DNS failed") from None
    if not records or len(records) > 64:
        raise ValueError("invalid callback DNS result")
    for family, socktype, proto, _, address in records:
        if (family not in (socket.AF_INET, socket.AF_INET6)
                or socktype != socket.SOCK_STREAM or address[1] != 443
                or not _public_address(address[0])
                or (family == socket.AF_INET6 and (address[2] or address[3]))):
            raise ValueError("callback resolves to a prohibited address")
    return records


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline):
        super().__init__()
        self.sock, self.deadline, self.received = sock, deadline, 0

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        count = self.sock.recv_into(buffer, min(len(buffer), MAX_WIRE_RESPONSE + 1 - self.received))
        self.received += count
        if self.received > MAX_WIRE_RESPONSE:
            raise ValueError("callback response exceeds wire limit")
        return count


class _ResponseSocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def makefile(self, mode):
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))


class CallbackClient:
    """HTTPS client for a nonempty exact operator-controlled hostname allowlist."""

    def __init__(self, allowed_hosts: set[str], *, timeout: float = 10.0):
        if not allowed_hosts or not 0 < timeout <= 30:
            raise ValueError("callback allowlist and bounded timeout required")
        hosts = frozenset(allowed_hosts)
        if any(not isinstance(host, str) or not _HOST.fullmatch(host) for host in hosts):
            raise ValueError("allowlist requires exact lowercase DNS hostnames")
        self.allowed_hosts, self.timeout = hosts, timeout

    def validate(self, url: str) -> None:
        """Validate URL syntax/allowlist. DNS is checked afresh when connecting."""
        self._target(url)

    def _target(self, url):
        if (not isinstance(url, str) or len(url) > 4096 or not url.isascii()
                or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in url)
                or "\\" in url or "#" in url):
            raise ValueError("invalid callback URL")
        try:
            parts = urlsplit(url)
            host, port = parts.hostname, parts.port
        except ValueError:
            raise ValueError("invalid callback URL") from None
        if (parts.scheme != "https" or host not in self.allowed_hosts
                or parts.username is not None or parts.password is not None
                or port not in (None, 443)
                or parts.netloc not in (host, host + ":443")):
            raise ValueError("callback must use an allowed HTTPS hostname on port 443")
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        return host, target

    def post(self, url: str, secret: str, subscription_id: str, event_id: str,
             body: bytes, old_secret: str | None = None) -> tuple[int, bytes]:
        host, target = self._target(url)
        if not isinstance(body, bytes) or len(body) > MAX_BODY:
            raise ValueError("callback body must be bytes, at most 256 KiB")
        if any(not isinstance(value, str) or not _ID.fullmatch(value)
               for value in (subscription_id, event_id)):
            raise ValueError("invalid webhook identifier")
        timestamp = str(int(time.time()))
        signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
        keys = [validate_secret(secret)]
        if old_secret is not None and old_secret != secret:
            keys.append(validate_secret(old_secret))
        signature = " ".join("v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
                             for key in keys)
        headers = {"Host": host, "Content-Type": "application/json",
                   "Content-Length": str(len(body)), "Connection": "close",
                   "webhook-id": event_id, "webhook-timestamp": timestamp,
                   "webhook-signature": signature, "X-MCP-Subscription-Id": subscription_id}
        request = (f"POST {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                   + "\r\n").encode("ascii") + body
        return self._exchange(host, request)

    def get(self, url: str) -> tuple[int, bytes]:
        """Fetch a fixed operator-configured public HTTPS endpoint (e.g. JWKS)."""
        host, target = self._target(url)
        request = (f"GET {target} HTTP/1.1\r\nHost: {host}\r\n"
                   "Accept: application/json\r\nConnection: close\r\n\r\n").encode("ascii")
        return self._exchange(host, request)

    def _exchange(self, host: str, request: bytes) -> tuple[int, bytes]:
        deadline = time.monotonic() + self.timeout
        records = _resolve(host, deadline)
        # Connect directly to a checked numeric sockaddr: never re-resolve host.
        family, socktype, proto, _, address = records[0]
        raw = socket.socket(family, socktype, proto)
        tls = None
        response = None
        try:
            raw.settimeout(_remaining(deadline))
            raw.connect(address)
            raw.settimeout(_remaining(deadline))
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            raw.settimeout(_remaining(deadline))
            tls = context.wrap_socket(raw, server_hostname=host)
            tls.settimeout(_remaining(deadline))
            tls.sendall(request)
            response = http.client.HTTPResponse(_ResponseSocket(tls, deadline))
            response.begin()
            if response.length is not None and response.length > MAX_RESPONSE:
                raise ValueError("callback response too large")
            data = response.read(MAX_RESPONSE + 1)
            _remaining(deadline)
            if len(data) > MAX_RESPONSE:
                raise ValueError("callback response too large")
            if response.length not in (None, 0):
                raise ValueError("incomplete callback response")
            return response.status, data
        finally:
            if response is not None:
                response.close()
            if tls is not None:
                tls.close()
            raw.close()

    def verify(self, url: str, secret: str, subscription_id: str) -> None:
        challenge = secrets.token_urlsafe(32)
        body = json.dumps({"type": "verification", "challenge": challenge},
                          separators=(",", ":")).encode()
        started = time.monotonic()
        try:
            status, data = self.post(url, secret, subscription_id,
                                     "msg_verification_" + secrets.token_hex(16), body)
            value = json.loads(data)
            echoed = value.get("challenge") if isinstance(value, dict) else None
            if (not 200 <= status < 300 or not isinstance(echoed, str)
                    or not hmac.compare_digest(echoed.encode("utf-8"), challenge.encode())
                    or time.monotonic() - started > self.timeout):
                raise ValueError("callback challenge failed")
        except (OSError, ValueError, UnicodeError, RecursionError, http.client.HTTPException):
            raise ValueError("callback verification failed") from None
