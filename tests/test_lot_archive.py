from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from procurement.db import Database
from procurement.models import CampaignCreate, LotCreate, LotItemCreate, ProjectCreate, SupplierCreate
from procurement.service import ConflictError, ProcurementService


class LotArchiveTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self.db = Database(self.db_path)
        self.db.initialize()
        self.service = ProcurementService(self.db)
        project = self.service.create_project(ProjectCreate(
            name="Склад Воронеж", region="Воронежская область", delivery_address="г. Воронеж, ул. Монтажная, 10"))
        self.lot = self.service.create_lot(LotCreate(
            project_id=project["id"], title="Окна ПВХ", region="Воронеж",
            delivery_address=project["delivery_address"],
            response_deadline=date.today() + timedelta(days=7),
            items=[LotItemCreate(name="Окно ПВХ 1400×1200", quantity=Decimal("84"), unit="шт.")]))
        self.other = self.service.create_lot(LotCreate(
            project_id=project["id"], title="Двери", region="Воронеж",
            delivery_address=project["delivery_address"],
            response_deadline=date.today() + timedelta(days=7),
            items=[LotItemCreate(name="Дверь", quantity=Decimal("4"), unit="шт.")]))
        self.project_id = project["id"]
        self.supplier = self.service.create_supplier(SupplierCreate(
            name="Окна Регион", tax_id="3666000001", region="Воронеж", email="sales@okna.example",
            categories=["окна"], rating=4.8, verified=True))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def campaign(self):
        return self.service.create_campaign(self.lot["id"], CampaignCreate(supplier_ids=[self.supplier["id"]]))

    def test_archived_purchase_leaves_lists_and_comes_back(self):
        archived = self.service.set_lot_archived(self.lot["id"], True)
        self.assertTrue(archived["archived_at"])
        self.assertEqual([lot["id"] for lot in self.service.list_lots()], [self.other["id"]])
        self.assertEqual([lot["id"] for lot in self.service.list_lots("only")], [self.lot["id"]])
        self.assertEqual(len(self.service.list_lots("all")), 2)
        self.assertEqual([lot["id"] for lot in self.service.get_project(self.project_id)["lots"]], [self.other["id"]])
        self.assertEqual(self.service.dashboard()["active_lots"], 1)
        again = self.service.set_lot_archived(self.lot["id"], True)  # repeated click changes nothing
        self.assertEqual(again["archived_at"], archived["archived_at"])

        restored = self.service.set_lot_archived(self.lot["id"], False)
        self.assertEqual(restored["archived_at"], "")
        self.assertEqual({lot["id"] for lot in self.service.list_lots()}, {self.lot["id"], self.other["id"]})
        actions = [row["action"] for row in self.db.all(
            "SELECT action FROM audit_log WHERE entity_type='lot' AND entity_id=? ORDER BY id", (str(self.lot["id"]),))]
        self.assertEqual(actions[-2:], ["archived", "restored"])

    def test_nothing_is_sent_from_an_archived_purchase(self):
        draft = self.campaign()["messages"][0]
        self.service.set_lot_archived(self.lot["id"], True)
        with self.assertRaisesRegex(ConflictError, "Закупка в архиве"):
            self.campaign()
        with self.assertRaisesRegex(ConflictError, "Закупка в архиве"):
            self.service.approve_message(draft["id"], "manager@example")
        with self.assertRaisesRegex(ConflictError, "Закупка в архиве"):
            self.service.simulate_outbox(draft["id"])
        self.service.set_lot_archived(self.lot["id"], False)
        self.assertEqual(self.service.approve_message(draft["id"], "manager@example")["status"], "approved")

    def test_approved_message_does_not_block_archive_but_cannot_be_mailed(self):
        from procurement import mail_delivery
        from procurement.launch_workflow import LaunchWorkflow

        message = self.campaign()["messages"][0]
        self.service.approve_message(message["id"], "manager@example")
        self.service.set_lot_archived(self.lot["id"], True)
        env = {"PROCUREMENT_SMTP_HOST": "smtp.invalid", "PROCUREMENT_SMTP_FROM": "snab@example.test"}
        with patch.dict(os.environ, env):
            with self.assertRaisesRegex(ConflictError, "Закупка в архиве"):
                mail_delivery.send(LaunchWorkflow(self.service), message["id"], True)
        status = self.db.one("SELECT status FROM outbox_messages WHERE id=?", (message["id"],))["status"]
        self.assertEqual(status, "approved")

    def test_purchase_with_mail_in_flight_is_not_archived(self):
        message = self.campaign()["messages"][0]
        for status in ("queued", "sending"):
            with self.db.connection() as conn:
                conn.execute("UPDATE outbox_messages SET status=? WHERE id=?", (status, message["id"]))
            with self.assertRaisesRegex(ConflictError, "ещё отправляются"):
                self.service.set_lot_archived(self.lot["id"], True)
        with self.db.connection() as conn:
            conn.execute("UPDATE outbox_messages SET status='sent' WHERE id=?", (message["id"],))
        self.assertTrue(self.service.set_lot_archived(self.lot["id"], True)["archived_at"])


class LotArchiveApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        with patch.dict(os.environ, {
            "PROCUREMENT_DB_PATH": self.db_path, "PROCUREMENT_ENV": "development", "PROCUREMENT_API_KEY": "",
            "PROCUREMENT_OUTBOX_MODE": "draft_only", "PROCUREMENT_AUTH_SECRET": "",
            "PROCUREMENT_ADMIN_USERNAME": "", "PROCUREMENT_ADMIN_PASSWORD_HASH": "",
        }):
            import importlib
            import procurement.app as app_module
            importlib.reload(app_module)
            app_module.db.initialize()
            self.client = TestClient(app_module.app, raise_server_exceptions=True)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def test_archive_endpoints(self):
        project = self.client.post("/api/projects", json={
            "name": "Склад", "region": "Воронежская область", "delivery_address": "г. Воронеж"}).json()
        lot = self.client.post("/api/lots", json={
            "project_id": project["id"], "title": "Окна", "region": "Воронеж", "delivery_address": "г. Воронеж",
            "response_deadline": (date.today() + timedelta(days=7)).isoformat(),
            "items": [{"name": "Окно", "quantity": "2", "unit": "шт."}]}).json()
        response = self.client.post(f"/api/lots/{lot['id']}/archive")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["archived_at"])
        self.assertEqual(self.client.get("/api/lots").json(), [])
        self.assertEqual([row["id"] for row in self.client.get("/api/lots?archived=only").json()], [lot["id"]])
        self.assertEqual(self.client.get("/api/lots?archived=bogus").status_code, 422)
        self.assertEqual(self.client.post(f"/api/lots/{lot['id']}/restore").json()["archived_at"], "")
        self.assertEqual([row["id"] for row in self.client.get("/api/lots").json()], [lot["id"]])
        self.assertEqual(self.client.post("/api/lots/999/archive").status_code, 404)


if __name__ == "__main__":
    unittest.main()
