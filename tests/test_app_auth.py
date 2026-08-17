from __future__ import annotations

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

import procurement.app as app_module
from procurement.config import Settings
from procurement.db import Database
from procurement.passwords import hash_password
from procurement.service import ProcurementService


SECRET = "app-auth-test-secret-" + ("x" * 32)
ADMIN_USERNAME = "snab-admin"
ADMIN_PASSWORD = "correct horse battery staple"
ADMIN_HASH = hash_password(ADMIN_PASSWORD, salt=b"\x01" * 16)


class StandaloneAuthHttpFlowTests(unittest.TestCase):
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
            auth_secret=SECRET,
            admin_username=ADMIN_USERNAME,
            admin_password_hash=ADMIN_HASH,
            session_ttl_seconds=3600,
        )
        app_module.db = Database(self.path)
        app_module.db.initialize()
        app_module.service = ProcurementService(app_module.db)
        app_module._LOGIN_FAILURES.clear()
        self.client = TestClient(app_module.app, base_url="https://procurement.test")

    def tearDown(self):
        self.client.close()
        app_module._LOGIN_FAILURES.clear()
        app_module.settings = self.previous_settings
        app_module.db = self.previous_db
        app_module.service = self.previous_service
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_valid_login_issues_secure_cookie_and_grants_api_access(self):
        response = self.client.post(
            "/auth/login",
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        cookie = response.headers["set-cookie"]
        self.assertIn("procurement_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(response.headers["cache-control"], "no-store")

        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.status_code, 200)

    def test_unauthenticated_browser_is_redirected_to_login_and_api_fails_closed(self):
        root = self.client.get("/", follow_redirects=False)
        dashboard = self.client.get("/api/dashboard")

        self.assertEqual(root.status_code, 303)
        self.assertEqual(root.headers["location"], "/login")
        self.assertEqual(dashboard.status_code, 403)

    def test_invalid_credentials_are_generic_and_do_not_set_cookie(self):
        response = self.client.post(
            "/auth/login",
            data={"username": "someone-else", "password": "definitely-wrong"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("set-cookie", response.headers)
        self.assertIn("Неверный логин или пароль", response.text)
        self.assertNotIn("someone-else", response.text)

    def test_login_rate_limit_fails_closed(self):
        for _ in range(app_module.LOGIN_MAX_FAILURES):
            response = self.client.post(
                "/auth/login",
                data={"username": ADMIN_USERNAME, "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 403)

        limited = self.client.post(
            "/auth/login",
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)

    def test_logout_clears_cookie_and_returns_to_login(self):
        self.client.post(
            "/auth/login",
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        response = self.client.post("/auth/logout", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/dashboard").status_code, 403)

    def test_das_sso_endpoint_no_longer_exists(self):
        self.assertEqual(self.client.post("/auth/sso", data={"token": "x" * 32}).status_code, 404)
