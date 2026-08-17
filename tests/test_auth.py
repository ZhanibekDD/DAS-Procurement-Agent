from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from procurement.auth import TokenError, issue_token, verify_token
from procurement.config import Settings
from procurement.passwords import hash_password, validate_password_hash, verify_password


SECRET = "integration-test-secret-" + ("x" * 32)
PASSWORD = "correct horse battery staple"
PASSWORD_HASH = hash_password(PASSWORD, salt=b"\x02" * 16)


class SignedSessionTokenTests(unittest.TestCase):
    def _issue(self, *, now: int = 1_700_000_000, ttl: int = 60) -> str:
        return issue_token(
            SECRET,
            issuer="das-procurement-agent",
            audience="das-procurement-web",
            subject="snab-admin",
            role="admin",
            kind="session",
            ttl_seconds=ttl,
            now=now,
            jti="test-session-id-000001",
        )

    def _verify(self, token: str, *, now: int = 1_700_000_010):
        return verify_token(
            SECRET,
            token,
            issuer="das-procurement-agent",
            audience="das-procurement-web",
            kind="session",
            max_ttl_seconds=3600,
            now=now,
        )

    def test_round_trip_preserves_authoritative_identity(self):
        claims = self._verify(self._issue())
        self.assertEqual(claims["sub"], "snab-admin")
        self.assertEqual(claims["role"], "admin")
        self.assertEqual(claims["jti"], "test-session-id-000001")

    def test_tampered_token_is_rejected(self):
        token = self._issue()
        encoded, signature = token.split(".", 1)
        tampered = ("A" if encoded[0] != "A" else "B") + encoded[1:]
        with self.assertRaisesRegex(TokenError, "signature"):
            self._verify(f"{tampered}.{signature}")

    def test_non_ascii_token_segments_are_rejected(self):
        encoded, signature = self._issue().split(".", 1)
        for malformed in (f"é{encoded}.{signature}", f"{encoded}.é{signature}"):
            with self.subTest(malformed=malformed.index("é")):
                with self.assertRaises(TokenError):
                    self._verify(malformed)

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
                audience="das-procurement-web",
                kind="session",
                max_ttl_seconds=3600,
                now=1_700_000_010,
            )


class PasswordHashTests(unittest.TestCase):
    def test_scrypt_round_trip(self):
        self.assertTrue(validate_password_hash(PASSWORD_HASH))
        self.assertTrue(verify_password(PASSWORD, PASSWORD_HASH))
        self.assertFalse(verify_password("wrong password", PASSWORD_HASH))

    def test_unicode_password_round_trip(self):
        encoded = hash_password("Надёжный пароль 2026!", salt=b"\x03" * 16)
        self.assertTrue(verify_password("Надёжный пароль 2026!", encoded))

    def test_malformed_hash_and_oversized_password_fail_closed(self):
        self.assertFalse(validate_password_hash("not-a-hash"))
        self.assertFalse(verify_password(PASSWORD, "scrypt:999:8:1:bad:bad"))
        self.assertFalse(verify_password("x" * 257, PASSWORD_HASH))


class StandaloneSettingsTests(unittest.TestCase):
    def test_valid_production_settings(self):
        with patch.dict(
            os.environ,
            {
                "PROCUREMENT_ENV": "production",
                "PROCUREMENT_API_KEY": "internal-only",
                "PROCUREMENT_AUTH_SECRET": SECRET,
                "PROCUREMENT_ADMIN_USERNAME": "snab-admin",
                "PROCUREMENT_ADMIN_PASSWORD_HASH": PASSWORD_HASH,
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertTrue(settings.local_auth_configured)

    def test_production_requires_standalone_auth(self):
        with patch.dict(
            os.environ,
            {
                "PROCUREMENT_ENV": "production",
                "PROCUREMENT_API_KEY": "internal-only",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "authentication"):
                Settings.from_env()

    def test_partial_auth_configuration_is_rejected(self):
        with patch.dict(
            os.environ,
            {
                "PROCUREMENT_ENV": "development",
                "PROCUREMENT_AUTH_SECRET": SECRET,
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "configured together"):
                Settings.from_env()

    def test_short_auth_secret_is_rejected(self):
        with patch.dict(
            os.environ,
            {
                "PROCUREMENT_ENV": "development",
                "PROCUREMENT_AUTH_SECRET": "too-short",
                "PROCUREMENT_ADMIN_USERNAME": "snab-admin",
                "PROCUREMENT_ADMIN_PASSWORD_HASH": PASSWORD_HASH,
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "AUTH_SECRET"):
                Settings.from_env()
