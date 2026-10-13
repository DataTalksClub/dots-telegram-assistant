"""OAuth tests use locally generated RSA test keys; never contact an issuer."""
import json
import time
import unittest
from unittest.mock import Mock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from telegram_events.auth import AuthenticationError, Authenticator, OAuthConfig


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private.public_key()))
        cls.jwk.update(kid="key-1", alg="RS256", use="sig")

    def setUp(self):
        self.config = OAuthConfig("https://issuer.example.com/", "https://plugin.example.com/mcp",
                                  "https://issuer.example.com/.well-known/jwks.json", "owner-123")
        self.loader = Mock(return_value={"keys": [self.jwk]})
        self.auth = Authenticator(self.config, jwks_loader=self.loader)

    def token(self, changes=None, omit=(), headers=None, key=None):
        claims = {"iss": self.config.issuer, "aud": self.config.audience, "sub": self.config.subject,
                  "exp": int(time.time()) + 300, "iat": int(time.time()), "scope": "telegram:read telegram:reply"}
        claims.update(changes or {})
        for field in omit:
            del claims[field]
        return "Bearer " + jwt.encode(claims, key or self.private, algorithm="RS256",
                                      headers=headers or {"kid": "key-1"})

    def test_valid_access_and_cached_jwks(self):
        header = self.token()
        self.assertEqual(self.auth.authenticate(header, "telegram:read"), "owner-123")
        self.assertEqual(self.auth.authenticate(header, "telegram:reply"), "owner-123")
        self.loader.assert_called_once()
        self.assertEqual(self.auth.authenticate(self.token(omit=("iat",)), "telegram:read"), "owner-123")

    def test_expiry_is_returned_for_subscription_lifetime(self):
        expiry = int(time.time()) + 123
        self.assertEqual(self.auth.authenticate_with_expiry(self.token({"exp": expiry}), "telegram:read"),
                         ("owner-123", expiry))

    def test_wrong_owner_audience_issuer_expiry_and_scope(self):
        for changes in [{"sub": "someone-else"}, {"aud": "https://evil.example/mcp"},
                        {"iss": "https://evil.example/"}, {"exp": int(time.time()) - 1},
                        {"iat": int(time.time()) + 300}, {"nbf": int(time.time()) + 300},
                        {"scope": "telegram:reply"}, {"scope": ["telegram:read"]}]:
            with self.subTest(changes=changes), self.assertRaisesRegex(AuthenticationError, "^invalid access token$"):
                self.auth.authenticate(self.token(changes), "telegram:read")

    def test_read_scope_does_not_authorize_reply(self):
        header = self.token({"scope": "telegram:read"})
        self.assertEqual(self.auth.authenticate(header, "telegram:read"), "owner-123")
        with self.assertRaises(AuthenticationError):
            self.auth.authenticate(header, "telegram:reply")

    def test_required_claims(self):
        for field in ("exp", "sub", "iss", "aud"):
            with self.subTest(field=field), self.assertRaises(AuthenticationError):
                self.auth.authenticate(self.token(omit=(field,)), "telegram:read")

    def test_invalid_signature(self):
        with self.assertRaises(AuthenticationError):
            self.auth.authenticate(self.token(key=self.other), "telegram:read")

    def test_invalid_headers_and_algorithms_do_not_fetch_keys(self):
        for header in [None, "", "Basic user:password", "Bearer not-a-jwt", "Bearer " + "a" * 16384,
                       "Bearer " + jwt.encode({"sub": "owner-123"}, b"s" * 32, algorithm="HS256", headers={"kid": "key-1"}),
                       self.token(headers={"kid": "key-1", "jku": "https://127.0.0.1/keys"}),
                       self.token(headers={"kid": "key-1", "crit": ["unknown"]})]:
            with self.subTest(header=(header or "")[:30]), self.assertRaises(AuthenticationError):
                self.auth.authenticate(header, "telegram:read")
        self.loader.assert_not_called()

    def test_unknown_key_fetch_is_rate_limited(self):
        header = self.token(headers={"kid": "unknown"})
        for _ in range(3):
            with self.assertRaises(AuthenticationError):
                self.auth.authenticate(header, "telegram:read")
        self.loader.assert_called_once()
        self.assertEqual(self.auth.authenticate(self.token(), "telegram:read"), "owner-123")

    def test_expired_cache_not_used_after_provider_failure(self):
        self.auth.authenticate(self.token(), "telegram:read")
        self.auth._expires = 0
        self.auth._last_refresh = float("-inf")
        self.loader.side_effect = OSError("secret provider details")
        with self.assertRaisesRegex(AuthenticationError, "^invalid access token$"):
            self.auth.authenticate(self.token(), "telegram:read")

    def test_bad_jwks_rejected(self):
        for document in [{}, {"keys": []}, {"keys": [self.jwk, self.jwk]},
                         {"keys": [dict(self.jwk, use="enc")]}, {"keys": [dict(self.jwk, alg="HS256")]},
                         {"keys": [dict(self.jwk, key_ops=["sign"])]}]:
            auth = Authenticator(self.config, jwks_loader=lambda: document)
            with self.subTest(document=document), self.assertRaises(AuthenticationError):
                auth.authenticate(self.token(), "telegram:read")

    def test_jwks_fetch_uses_pinned_client_and_rejects_redirect(self):
        auth = Authenticator(self.config)
        with patch.object(auth._client, "get", return_value=(200, json.dumps({"keys": [self.jwk]}).encode())) as get:
            self.assertEqual(auth.authenticate(self.token(), "telegram:read"), "owner-123")
            get.assert_called_once_with(self.config.jwks_url)
        auth = Authenticator(self.config)
        with patch.object(auth._client, "get", return_value=(302, b"{}")), self.assertRaises(AuthenticationError):
            auth.authenticate(self.token(), "telegram:read")

    def test_metadata_and_challenge(self):
        metadata = self.auth.resource_metadata()
        self.assertEqual(metadata["resource"], self.config.audience)
        self.assertEqual(metadata["authorization_servers"], [self.config.issuer])
        self.assertEqual(metadata["bearer_methods_supported"], ["header"])
        self.assertEqual(self.auth.challenge("telegram:read"),
                         'Bearer resource_metadata="https://plugin.example.com/.well-known/oauth-protected-resource/mcp", scope="telegram:read"')

    def test_config_environment_validation(self):
        env = dict(OAUTH_ISSUER=self.config.issuer, OAUTH_AUDIENCE=self.config.audience,
                   OAUTH_JWKS_URL=self.config.jwks_url, OAUTH_SUBJECT=self.config.subject)
        self.assertEqual(OAuthConfig.from_env(env), self.config)
        for field, value in [("OAUTH_ISSUER", "http://issuer.example.com"),
                             ("OAUTH_AUDIENCE", "https://plugin.example.com/wrong"),
                             ("OAUTH_AUDIENCE", "https://plugin.example.com/nested/mcp"),
                             ("OAUTH_JWKS_URL", "https://user:pass@issuer.example.com/keys"),
                             ("OAUTH_JWKS_URL", "https://127.0.0.1/keys"),
                             ("OAUTH_JWKS_URL", "https://issuer.example.com/keys?redirect=1"),
                             ("OAUTH_SUBJECT", "")]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                OAuthConfig.from_env(dict(env, **{field: value}))
        with self.assertRaises(ValueError):
            OAuthConfig.from_env({})


if __name__ == "__main__":
    unittest.main()
