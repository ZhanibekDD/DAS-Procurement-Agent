from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from procurement.auth import TokenError, issue_token, verify_token
from procurement.config import Settings
from procurement.db import Database


SECRET = "integration-test-secret-" + ("x" * 32)


class SignedTokenTests(unittest.TestCase):
    def _issue(self, *, now: int = 1_700_000_000, ttl: int = 60) -> str:
        return issue_token(
            SECRET,
            issuer="das",
            audience="das-procurement-agent",
            subject="user-42",
            role="staff",
            kind="launch",
            ttl_seconds=ttl,
            now=now,
            jti="test-token-id-00000001",
        )

    def _verify(self, token: str, *, now: int = 1_700_000_010):
        return verify_token(
            SECRET,
            token,
            issuer="das",
            audience="das-procurement-agent",
            kind="launch",
            max_ttl_seconds=120,
            now=now,
        )

    def test_round_trip_preserves_authoritative_identity(self):
        claims = self._verify(self._issue())
        self.assertEqual(claims["sub"], "user-42")
        self.assertEqual(claims["role"], "staff")
        self.assertEqual(claims["jti"], "test-token-id-00000001")

    def test_tampered_token_is_rejected(self):
        token = self._issue()
        encoded, signature = token.split(".", 1)
        tampered = ("A" if encoded[0] != "A" else "B") + encoded[1:]
        with self.assertRaisesRegex(TokenError, "signature"):
            self._verify(f"{tampered}.{signature}")

    def test_expired_token_is_rejected(self):
        with self.assertRaisesRegex(TokenError, "expired"):
            self._verify(self._issue(), now=1_700_000_061)

    def test_wrong_scope_is_rejected(self):
        token = self._issue()
        with self.assertRaisesRegex(TokenError, "scope"):
            verify_token(
                SECRET,
                token,
                issuer="another-system",
                audience="das-procurement-agent",
                kind="launch",
                max_ttl_seconds=120,
                now=1_700_000_010,
            )

    def test_launch_token_cannot_exceed_policy_ttl(self):
        with self.assertRaisesRegex(TokenError, "ttl exceeds"):
            self._verify(self._issue(ttl=121))


class SsoNonceTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = handle.name
        handle.close()
        self.db = Database(self.path)
        self.db.initialize()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_launch_token_is_accepted_only_once(self):
        self.assertTrue(self.db.consume_sso_jti("nonce-000000000001", 200, 100))
        self.assertFalse(self.db.consume_sso_jti("nonce-000000000001", 200, 100))

    def test_expired_nonce_is_pruned(self):
        self.assertTrue(self.db.consume_sso_jti("nonce-000000000002", 100, 50))
        self.assertTrue(self.db.consume_sso_jti("nonce-000000000002", 300, 101))


class SsoSettingsTests(unittest.TestCase):
    def test_short_sso_secret_is_rejected(self):
        with patch.dict(
            os.environ,
            {
                "PROCUREMENT_ENV": "development",
                "PROCUREMENT_SSO_SECRET": "too-short",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "SSO_SECRET"):
                Settings.from_env()
