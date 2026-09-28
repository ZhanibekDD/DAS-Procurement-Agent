from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import procurement.app as app_module
from procurement.config import Settings
from procurement.db import Database
from procurement.passwords import hash_password
from procurement.service import ProcurementService


class StaffInterfaceTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = handle.name
        handle.close()
        self.before = (app_module.settings, app_module.db, app_module.service)
        app_module.settings = Settings(
            environment="production", api_key="staff-test-key", db_path=self.path,
            outbox_mode="draft_only", auth_secret="staff-ui-test-secret-" + "x" * 32,
            admin_username="admin-test", admin_password_hash=hash_password("safe-test-password", salt=b"1" * 16),
            session_ttl_seconds=3600,
        )
        app_module.db = Database(self.path)
        app_module.db.initialize()
        app_module.service = ProcurementService(app_module.db)
        self.client = TestClient(app_module.app, base_url="https://procurement.test")

    def tearDown(self):
        self.client.close()
        app_module.settings, app_module.db, app_module.service = self.before
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_staff_sees_named_activity_but_not_raw_audit(self):
        headers = {"x-api-key": "staff-test-key"}
        supplier = self.client.post("/api/suppliers", headers=headers, json={
            "name": "Тестовый поставщик", "region": "Воронежская область",
        })
        self.assertEqual(supplier.status_code, 201)
        supplier_id = supplier.json()["id"]
        app_module.db.audit("supplier_edited", "supplier", supplier_id,
                            details={"internal_sha256": "secret-in-audit"})
        app_module.db.audit("reference_checked", "source_document", 42,
                            details={"raw_table": "internal-only"})

        context = self.client.get("/api/ui-context", headers=headers)
        self.assertEqual(context.json(), {"role": "staff"})
        activity = self.client.get("/api/activity", headers=headers)
        self.assertEqual(activity.status_code, 200)
        self.assertEqual(activity.json()[0]["label"], "Поставщик изменён")
        self.assertEqual(activity.json()[0]["name"], "Тестовый поставщик")
        self.assertEqual(activity.json()[0]["target_id"], supplier_id)
        self.assertNotIn("secret-in-audit", activity.text)
        self.assertNotIn("reference_checked", activity.text)
        self.assertEqual(self.client.get("/api/audit", headers=headers).status_code, 403)

        login = self.client.post("/auth/login", data={"username": "admin-test", "password": "safe-test-password"}, follow_redirects=False)
        self.assertEqual(login.status_code, 303)
        self.assertEqual(self.client.get("/api/ui-context").json(), {"role": "admin"})
        audit = self.client.get("/api/audit")
        self.assertEqual(audit.status_code, 200)
        self.assertIn("secret-in-audit", audit.text)

    def test_five_primary_sections_and_human_statuses(self):
        html = (Path(__file__).parents[1] / "procurement" / "static" / "index.html").read_text(encoding="utf-8")
        nav = html.split('<nav class="nav" id="nav">', 1)[1].split("</nav>", 1)[0]
        for name in ("Закупки", "Поставщики", "Прайсы и цены", "Проекты", "Документы"):
            self.assertIn(name, nav)
        self.assertEqual(nav.count("data-view="), 5)
        self.assertNotIn("PROCUREMENT CONTROL", html)
        script = (Path(__file__).parents[1] / "procurement" / "static" / "staff-ui.js").read_text(encoding="utf-8")
        for status in ("Черновик", "Готов к отправке", "Отправляется", "Отправлен", "Ошибка отправки"):
            self.assertIn(status, script)
        self.assertIn("accepted_at", script)


if __name__ == "__main__":
    unittest.main()
