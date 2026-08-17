from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace

from fastapi.testclient import TestClient
from starlette.requests import Request

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

    def test_valid_credentials_clear_failures_instead_of_locking_out_admin(self):
        for _ in range(app_module.LOGIN_MAX_FAILURES):
            response = self.client.post(
                "/auth/login",
                data={"username": ADMIN_USERNAME, "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 403)

        valid = self.client.post(
            "/auth/login",
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
            follow_redirects=False,
        )
        self.assertEqual(valid.status_code, 303)

        self.client.cookies.clear()
        after_reset = self.client.post(
            "/auth/login",
            data={"username": ADMIN_USERNAME, "password": "wrong-password"},
        )
        self.assertEqual(after_reset.status_code, 403)

    def test_login_rate_limit_rejects_only_invalid_credentials(self):
        for _ in range(app_module.LOGIN_MAX_FAILURES):
            response = self.client.post(
                "/auth/login",
                data={"username": ADMIN_USERNAME, "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 403)

        limited = self.client.post(
            "/auth/login",
            data={"username": ADMIN_USERNAME, "password": "still-wrong"},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)

    def test_forwarded_ip_is_used_only_for_an_explicitly_trusted_proxy(self):
        app_module.settings = replace(
            app_module.settings,
            trusted_proxy_networks=("10.0.0.0/8",),
        )

        untrusted = Request(
            {
                "type": "http",
                "headers": [(b"x-forwarded-for", b"203.0.113.8")],
                "client": ("198.51.100.20", 1234),
            }
        )
        trusted = Request(
            {
                "type": "http",
                "headers": [
                    (b"x-forwarded-for", b"192.0.2.99, 198.51.100.45")
                ],
                "client": ("10.0.0.5", 1234),
            }
        )
        malformed = Request(
            {
                "type": "http",
                "headers": [(b"x-forwarded-for", b"not-an-ip")],
                "client": ("10.0.0.5", 1234),
            }
        )

        self.assertEqual(app_module._client_ip(untrusted), "198.51.100.20")
        self.assertEqual(app_module._client_ip(trusted), "198.51.100.45")
        self.assertEqual(app_module._client_ip(malformed), "10.0.0.5")

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
        self.assertEqual(
            self.client.post("/auth/sso", data={"token": "x" * 32}).status_code,
            404,
        )
