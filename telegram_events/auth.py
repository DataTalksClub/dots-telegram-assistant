"""OAuth protected resource backed by an existing operator-selected issuer.

This module never issues credentials or implements an authorization server.
Only RS256 access tokens for the configured audience and owner are accepted.
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
import json
import math
import os
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import jwt

from .network import CallbackClient

SCOPES = ("telegram:read", "telegram:reply")
_KID = re.compile(r"[A-Za-z0-9_.:/+=-]{1,256}\Z")
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\Z", re.IGNORECASE)


class AuthenticationError(ValueError):
    """Sanitized rejection; callers should respond 401 with a Bearer challenge."""


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    audience: str
    jwks_url: str
    subject: str

    def __post_init__(self):
        for value in (self.issuer, self.audience, self.jwks_url):
            try:
                parsed = urlsplit(value)
                CallbackClient({parsed.hostname}).validate(value)
                if parsed.query or any(ch in value for ch in ('"', "<", ">")):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError("OAuth endpoints require fixed HTTPS URLs without queries or fragments") from None
        if urlsplit(self.audience).path != "/mcp":
            raise ValueError("OAuth audience must be the canonical HTTPS /mcp resource URL")
        if (not isinstance(self.subject, str) or not 1 <= len(self.subject) <= 256
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in self.subject)):
            raise ValueError("OAuth owner subject is required")

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        return cls(*(env.get(name, "") for name in (
            "OAUTH_ISSUER", "OAUTH_AUDIENCE", "OAUTH_JWKS_URL", "OAUTH_SUBJECT")))

    @property
    def metadata_url(self):
        parts = urlsplit(self.audience)
        return urlunsplit((parts.scheme, parts.netloc,
                           "/.well-known/oauth-protected-resource" + parts.path, "", ""))

    def resource_metadata(self):
        return {"resource": self.audience, "authorization_servers": [self.issuer],
                "bearer_methods_supported": ["header"], "scopes_supported": list(SCOPES)}

    def challenge(self, required_scope=None):
        value = f'Bearer resource_metadata="{self.metadata_url}"'
        if required_scope in SCOPES:
            value += f', scope="{required_scope}"'
        return value


class Authenticator:
    def __init__(self, config: OAuthConfig, *, jwks_loader=None):
        self.config = config
        self._client = CallbackClient({urlsplit(config.jwks_url).hostname})
        # Injection is exclusively for offline tests, never request input.
        self._load = jwks_loader or self._fetch_jwks
        self._keys = {}
        self._expires = 0.0
        self._last_refresh = float("-inf")
        self._lock = threading.Lock()

    def resource_metadata(self):
        return self.config.resource_metadata()

    def challenge(self, required_scope=None):
        return self.config.challenge(required_scope)

    def _fetch_jwks(self):
        status, body = self._client.get(self.config.jwks_url)
        if status != 200:
            raise ValueError("JWKS unavailable")
        return json.loads(body)

    def _key(self, kid):
        with self._lock:
            now = time.monotonic()
            if self._expires > now and kid in self._keys:
                return self._keys[kid]
            # Bound unknown-key and issuer-failure refresh storms. Never use an
            # expired cache when fetching fails; a stale key can be revoked.
            if now - self._last_refresh < 30:
                raise AuthenticationError("invalid access token")
            self._last_refresh = now
            document = self._load()
            if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
                raise ValueError("invalid JWKS")
            if not 1 <= len(document["keys"]) <= 32:
                raise ValueError("invalid JWKS key count")
            keys = {}
            for entry in document["keys"]:
                if not isinstance(entry, dict):
                    raise ValueError("invalid JWK")
                if entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig" or entry.get("alg", "RS256") != "RS256":
                    continue
                if "key_ops" in entry and entry["key_ops"] != ["verify"]:
                    continue
                key_id = entry.get("kid")
                if not isinstance(key_id, str) or not _KID.fullmatch(key_id) or key_id in keys:
                    raise ValueError("invalid or duplicate JWK identifier")
                # Explicit fields only: token headers and remote x5u/jku URLs
                # never select endpoints or override the configured algorithm.
                key = jwt.PyJWK.from_dict({"kty": "RSA", "n": entry.get("n"),
                                          "e": entry.get("e"), "alg": "RS256"}, algorithm="RS256").key
                if not 2048 <= key.key_size <= 8192:
                    raise ValueError("unsupported RSA key size")
                keys[key_id] = key
            if not keys:
                raise ValueError("no usable signing keys")
            self._keys, self._expires = keys, time.monotonic() + 300
            if kid not in keys:
                raise AuthenticationError("invalid access token")
            return keys[kid]

    def authenticate(self, header: str | None, required_scope: str) -> str:
        return self.authenticate_with_expiry(header, required_scope)[0]

    def authenticate_with_expiry(self, header: str | None, required_scope: str) -> tuple[str, int]:
        """Return owner and expiration; subscriptions must never outlive this grant.

        Offline JWT validation does not check provider token revocation. Existing
        subscriptions must be capped to token expiry and support owner shutdown.
        """
        if required_scope not in SCOPES:
            raise ValueError("unsupported required OAuth scope")
        try:
            if not isinstance(header, str) or len(header) > 16384:
                raise AuthenticationError("invalid access token")
            match = _BEARER.fullmatch(header)
            if not match:
                raise AuthenticationError("invalid access token")
            token = match.group(1)
            protected = jwt.get_unverified_header(token)
            kid = protected.get("kid")
            if (protected.get("alg") != "RS256" or not isinstance(kid, str)
                    or not _KID.fullmatch(kid) or protected.get("crit")
                    or "jku" in protected or "x5u" in protected or "jwk" in protected):
                raise AuthenticationError("invalid access token")
            claims = jwt.decode(token, self._key(kid), algorithms=["RS256"],
                                audience=self.config.audience, issuer=self.config.issuer,
                                options={"require": ["exp", "sub", "iss", "aud"],
                                         "verify_signature": True, "verify_exp": True,
                                         "verify_nbf": True, "verify_iat": True})
            subject = claims.get("sub")
            if (not isinstance(subject, str)
                    or not hmac.compare_digest(subject.encode(), self.config.subject.encode())):
                raise AuthenticationError("invalid access token")
            scope = claims.get("scope")
            if not isinstance(scope, str) or required_scope not in scope.split():
                raise AuthenticationError("invalid access token")
            expiry = claims["exp"]
            if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                raise AuthenticationError("invalid access token")
            return subject, int(expiry)
        except Exception:
            # Never expose token contents, claims, provider details, or URLs.
            raise AuthenticationError("invalid access token") from None
