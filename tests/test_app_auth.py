from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

import procurement.app as app_module
from procurement.auth import issue_token
from procurement.config import Settings
from procurement.db import Database
from procurement.service import ProcurementService


SECRET = "app-auth-test-secret-" + ("x" * 32)


class SsoHttpFlowTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = handle.name
        handle.close()

        self.previous_settings = app_module.settings
        self.previous_db = app_module.db
        self.previous_service = app_module.service

        app_module.settings = Settings(
            environment="production",
            api_key="internal-test-key",
            db_path=self.path,
            outbox_mode="draft_only",
            sso_secret=SECRET,
            sso_issuer="das",
            session_ttl_seconds=3600,
        )
        app_module.db = Database(self.path)
        app_module.db.initialize()
        app_module.service = ProcurementService(app_module.db)
        self.client = TestClient(app_module.app, base_url="https://procurement.test")

    def tearDown(self):
        self.client.close()
        app_module.settings = self.previous_settings
        app_module.db = self.previous_db
        app_module.service = self.previous_service
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    @staticmethod
    def _launch_token(jti: str = "http-flow-token-000001") -> str:
        return issue_token(
            SECRET,
            issuer="das",
            audience="das-procurement-agent",
            subject="django:42",
            role="staff",
            kind="launch",
            ttl_seconds=60,
            jti=jti,
        )

    def test_post_exchanges_launch_for_secure_cookie_session(self):
        response = self.client.post(
            "/auth/sso",
            data={"token": self._launch_token()},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(response.headers["cache-control"], "no-store")

        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.status_code, 200)

    def test_launch_token_is_not_accepted_in_query_string(self):
        response = self.client.get(f"/auth/sso?token={self._launch_token()}")
        self.assertEqual(response.status_code, 405)

    def test_non_ascii_token_segments_are_rejected_with_403(self):
        encoded, signature = self._launch_token().split(".", 1)
        malformed_tokens = (
            f"é{encoded}.{signature}",
            f"{encoded}.é{signature}",
        )

        for token in malformed_tokens:
            with self.subTest(token_part=token.index("é")):
                response = self.client.post(
                    "/auth/sso",
                    data={"token": token},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 403)

    def test_replayed_post_is_rejected(self):
        token = self._launch_token("http-flow-token-000002")
        first = self.client.post(
            "/auth/sso",
            data={"token": token},
            follow_redirects=False,
        )
        second = self.client.post(
            "/auth/sso",
            data={"token": token},
            follow_redirects=False,
        )

        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 403)
