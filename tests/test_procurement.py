from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from openpyxl import Workbook

from procurement.config import Settings
from procurement.db import Database
from procurement.imports import parse_supplier_table
from procurement.models import (
    CampaignCreate,
    LotCreate,
    LotItemCreate,
    ProjectCreate,
    QuoteCreate,
    QuoteItemCreate,
    SupplierCreate,
)
from procurement.service import ConflictError, ProcurementService
from procurement.templates import render_template


class ProcurementTestCase(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self.db = Database(self.db_path)
        self.db.initialize()
        self.service = ProcurementService(self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def _project_and_lot(self):
        project = self.service.create_project(
            ProjectCreate(
                name="Склад Воронеж",
                region="Воронежская область",
                delivery_address="г. Воронеж, ул. Монтажная, 10",
            )
        )
        lot = self.service.create_lot(
            LotCreate(
                project_id=project["id"],
                title="Окна ПВХ",
                region="Воронеж",
                delivery_address=project["delivery_address"],
                response_deadline=date.today() + timedelta(days=7),
                desired_delivery_date=date.today() + timedelta(days=30),
                items=[
                    LotItemCreate(
                        name="Окно ПВХ 1400×1200",
                        quantity=Decimal("84"),
                        unit="шт.",
                        specification="двухкамерный стеклопакет, белый профиль",
                    )
                ],
            )
        )
        return project, lot

    def test_full_workflow_drafts_rfq_and_ranks_landed_cost(self):
        _, lot = self._project_and_lot()
        supplier_a = self.service.create_supplier(
            SupplierCreate(
                name="Окна Регион",
                tax_id="3666000001",
                region="Воронеж",
                email="sales@okna.example",
                categories=["окна"],
                rating=4.8,
                verified=True,
            )
        )
        supplier_b = self.service.create_supplier(
            SupplierCreate(
                name="Строй Комплект",
                tax_id="3666000002",
                region="Воронежская область",
                email="tender@stroy.example",
                categories=["окна", "двери"],
                rating=4.0,
                verified=True,
            )
        )

        matches = self.service.match_suppliers(lot["id"])
        self.assertEqual({row["id"] for row in matches}, {supplier_a["id"], supplier_b["id"]})

        campaign = self.service.create_campaign(
            lot["id"],
            CampaignCreate(supplier_ids=[supplier_a["id"], supplier_b["id"]]),
        )
        self.assertEqual(len(campaign["messages"]), 2)
        self.assertTrue(all(message["status"] == "draft" for message in campaign["messages"]))
        self.assertIn("84", campaign["messages"][0]["body"])

        approved = self.service.approve_message(campaign["messages"][0]["id"], "manager@example")
        self.assertEqual(approved["status"], "approved")
        self.assertNotEqual(approved["status"], "sent")

        item_id = lot["items"][0]["id"]
        self.service.add_quote(
            lot["id"],
            QuoteCreate(
                supplier_id=supplier_a["id"],
                delivery_cost=Decimal("40000"),
                lead_days=14,
                payment_terms="50% аванс, 50% после поставки",
                items=[QuoteItemCreate(lot_item_id=item_id, unit_price=Decimal("12500"))],
            ),
        )
        self.service.add_quote(
            lot["id"],
            QuoteCreate(
                supplier_id=supplier_b["id"],
                delivery_cost=Decimal("0"),
                lead_days=25,
                payment_terms="100% предоплата",
                items=[QuoteItemCreate(lot_item_id=item_id, unit_price=Decimal("12800"))],
            ),
        )

        comparison = self.service.comparison(lot["id"])
        self.assertEqual(comparison["decision"], "human_approval_required")
        self.assertEqual(comparison["quotes"][0]["rank"], 1)
        self.assertEqual(comparison["quotes"][0]["supplier_name"], "Окна Регион")
        self.assertEqual(comparison["quotes"][0]["total_cost"], 1_090_000.0)

    def test_incomplete_quote_is_disqualified(self):
        project = self.service.create_project(
            ProjectCreate(name="Проект", region="Алматы", delivery_address="ул. Абая, 1")
        )
        lot = self.service.create_lot(
            LotCreate(
                project_id=project["id"],
                title="Песок",
                region="Алматы",
                delivery_address="ул. Абая, 1",
                response_deadline=date.today() + timedelta(days=3),
                items=[
                    LotItemCreate(name="Песок", quantity=10, unit="т"),
                    LotItemCreate(name="Щебень", quantity=5, unit="т"),
                ],
            )
        )
        supplier = self.service.create_supplier(
            SupplierCreate(name="Карьер", region="Алматы", email="a@b.kz")
        )
        self.service.add_quote(
            lot["id"],
            QuoteCreate(
                supplier_id=supplier["id"],
                items=[QuoteItemCreate(lot_item_id=lot["items"][0]["id"], unit_price=1000)],
            ),
        )
        row = self.service.comparison(lot["id"])["quotes"][0]
        self.assertEqual(row["rank_status"], "disqualified")
        self.assertEqual(row["coverage"], "1/2")

    def test_duplicate_supplier_tax_id_is_rejected(self):
        supplier = SupplierCreate(name="Поставщик", tax_id="123", region="Астана")
        self.service.create_supplier(supplier)
        with self.assertRaises(ConflictError):
            self.service.create_supplier(supplier)

    def test_template_requires_every_variable(self):
        with self.assertRaisesRegex(ValueError, "supplier_name"):
            render_template("Здравствуйте, {supplier_name}", {})

    def test_paid_invoice_is_stored_once_and_queued_for_review(self):
        payload = b"%PDF-1.4\n% safe test invoice"
        first = self.service.register_source_document(
            filename="invoice-42.pdf",
            content=payload,
            document_type="paid_invoice",
            content_type="application/pdf",
        )
        second = self.service.register_source_document(
            filename="copy.pdf",
            content=payload,
            document_type="paid_invoice",
            content_type="application/pdf",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["extraction_status"], "pending_ai_extraction")
        self.assertTrue(os.path.exists(first["storage_path"]))
        os.unlink(first["storage_path"])

    def test_invalid_pdf_payload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid PDF"):
            self.service.register_source_document(
                filename="invoice.pdf",
                content=b"not a pdf",
                document_type="paid_invoice",
            )


class ImportTests(unittest.TestCase):
    def test_csv_supplier_preview(self):
        content = (
            "Поставщик;ИНН;Регион;Email;Категории;Проверен\n"
            "Окна Регион;3666000001;Воронеж;sales@example.ru;окна, двери;да\n"
        ).encode("utf-8")
        preview = parse_supplier_table(content, "suppliers.csv")
        self.assertEqual(len(preview.rows), 1)
        self.assertEqual(preview.rows[0].categories, ["окна", "двери"])
        self.assertTrue(preview.rows[0].verified)

    def test_xlsx_supplier_preview_and_invalid_row(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Компания", "Город", "Почта", "Рейтинг"])
        sheet.append(["Карьер №1", "Воронеж", "tender@example.ru", 4.5])
        sheet.append(["", "", "bad", 9])
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()

        preview = parse_supplier_table(buffer.getvalue(), "suppliers.xlsx")
        self.assertEqual(len(preview.rows), 1)
        self.assertEqual(preview.rows[0].name, "Карьер №1")
        self.assertEqual(len(preview.errors), 1)


class SettingsTests(unittest.TestCase):
    def test_production_requires_api_key(self):
        with patch.dict(
            os.environ,
            {"PROCUREMENT_ENV": "production", "PROCUREMENT_API_KEY": ""},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "API_KEY"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
