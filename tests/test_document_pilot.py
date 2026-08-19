from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from procurement.db import Database
from procurement.document_analysis import (
    compare_fence_documents,
    extract_bulat_fence_schedule,
    extract_bulat_invoice,
)
from procurement.models import (
    CampaignCreate,
    LotCreate,
    LotItemCreate,
    ProcurementSuggestionApproval,
    ProcurementSuggestionCreate,
    ProjectCreate,
    SupplierCreate,
)
from procurement.ranking import rank_quotes
from procurement.regions import infer_cluster, resolve_cluster
from procurement.service import ProcurementService


PROJECT_PAGE_13 = """
СТРМ-ННП.24126-ННП-001-АС01-ГЧ-011
Схема расположения ограждения и площадки обслуживания ПО1 УЗА-2 камеры приема СОД
Спецификация элементов
П1 Заводской поставки Панель ограждения типа "Булат", высота 2,5м 9 l=2960
П2 Панель ограждения типа "Булат", высота 2,5м 12 l=2460
П3 Панель ограждения типа "Булат", высота 2,5м 6 l=1960
Ст1 Комплект опоры заграждения 28 шт.
К1 Калитка (высота 2,2 м, длина 0,96 м) 1 комплект
В1 Ворота (высота 2,5 м, длина 2,9 м) 1 комплект
"""


REFERENCE_INVOICE = """
Счет на оплату № 1929 от 2 июня 2026 г.
Поставщик Общество с ограниченной ответственностью "КАПИТАЛ-ТЕХНО",
ИНН 6678062559, КПП 667801001
1 3D панель "Булат-Стандарт" В2030хШ3000мм (пруток 4мм, ячейка
50х200мм), цинк+ПП, RAL 5010 НФ-00010913 70 шт. 2 885,30 201 971,00
2 Столб 60х60х2мм, L=2500мм, фланец 5х150х150мм
НФ-00005038 80 шт. 1 958,00 156 640,00
3 Заглушка ПВХ 60х60 НФ-00003015 80 шт. 30,00 2 400,00
4 Скоба монтажная 40х35х10мм
5 Саморез кровельный 6.3х38мм
6 Кронштейн Y-500/600
"""


class FenceDocumentPilotTests(unittest.TestCase):
    def test_page_13_extracts_six_traceable_positions(self):
        result = extract_bulat_fence_schedule(
            PROJECT_PAGE_13, page_number=13, source_document_id=42
        )

        self.assertEqual(len(result.items), 6)
        self.assertEqual(
            [str(item.quantity) for item in result.items],
            ["9", "12", "6", "28", "1", "1"],
        )
        self.assertEqual(result.items[0].specification, "высота 2500 мм; длина 2960 мм")
        self.assertTrue(all(item.source_document_id == 42 for item in result.items))
        self.assertTrue(all(item.source_page == 13 for item in result.items))
        self.assertTrue(
            all("Спецификация элементов" in item.source_reference for item in result.items)
        )
        self.assertIn("СТРМ-ННП.24126-ННП-001-АС01-ГЧ-011", result.evidence)

    def test_incomplete_schedule_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "missing positions"):
            extract_bulat_fence_schedule(
                'П1 Панель ограждения типа "Булат", высота 2,5м 9 l=2960',
                page_number=13,
            )

    def test_invoice_is_price_history_not_current_quote(self):
        schedule = extract_bulat_fence_schedule(PROJECT_PAGE_13, page_number=13)
        invoice = extract_bulat_invoice(REFERENCE_INVOICE)
        result = compare_fence_documents(
            [item.model_dump(mode="json") for item in schedule.items], invoice
        )

        self.assertEqual(result["status"], "mismatch")
        self.assertFalse(result["can_use_as_current_quote"])
        self.assertEqual(result["allowed_use"], ["supplier_registry", "price_history"])
        self.assertEqual(
            {row["code"] for row in result["discrepancies"]},
            {
                "panel_quantity",
                "panel_height_mm",
                "panel_lengths_mm",
                "support_quantity",
                "wicket_presence",
                "gate_presence",
            },
        )
        self.assertEqual(invoice["supplier_tax_id"], "6678062559")
        self.assertEqual(invoice["panel"]["quantity"], 70)

    def test_existing_database_gets_additive_columns(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = handle.name
        handle.close()
        try:
            with sqlite3.connect(path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE projects (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL, region TEXT NOT NULL,
                        delivery_address TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL
                    );
                    CREATE TABLE suppliers (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                        tax_id TEXT NOT NULL DEFAULT '', region TEXT NOT NULL,
                        email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
                        telegram TEXT NOT NULL DEFAULT '', categories_json TEXT NOT NULL DEFAULT '[]',
                        rating REAL NOT NULL DEFAULT 3, verified INTEGER NOT NULL DEFAULT 0,
                        active INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'manual',
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE lots (
                        id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
                        section_id INTEGER, title TEXT NOT NULL, region TEXT NOT NULL,
                        delivery_address TEXT NOT NULL, response_deadline TEXT NOT NULL,
                        desired_delivery_date TEXT, currency TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL
                    );
                    CREATE TABLE lot_items (
                        id INTEGER PRIMARY KEY, lot_id INTEGER NOT NULL, name TEXT NOT NULL,
                        quantity TEXT NOT NULL, unit TEXT NOT NULL,
                        specification TEXT NOT NULL DEFAULT ''
                    );
                    """
                )
            database = Database(path)
            database.initialize()
            with database.connection() as conn:
                columns = {
                    table: {
                        row["name"]
                        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                    }
                    for table in ("projects", "suppliers", "lots", "lot_items")
                }
            self.assertIn("cluster", columns["projects"])
            self.assertIn("max_contact", columns["suppliers"])
            self.assertIn("cluster", columns["suppliers"])
            self.assertIn("cluster", columns["lots"])
            self.assertIn("source_page", columns["lot_items"])
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(path + suffix)
                except FileNotFoundError:
                    pass


class ClusterAndApprovalTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = handle.name
        handle.close()
        self.db = Database(self.path)
        self.db.initialize()
        self.service = ProcurementService(self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def _lot(self):
        project = self.service.create_project(
            ProjectCreate(
                name="Ограждение УЗА-2",
                region="Свердловская область",
                delivery_address="адрес уточняется перед рассылкой",
            )
        )
        return self.service.create_lot(
            LotCreate(
                project_id=project["id"],
                title="Ограждение Булат",
                region=project["region"],
                delivery_address=project["delivery_address"],
                response_deadline=date.today() + timedelta(days=7),
                items=[
                    LotItemCreate(name="Панель Булат", quantity=27, unit="шт.")
                ],
            )
        )

    def test_cluster_inference_and_cross_cluster_campaign_block(self):
        lot = self._lot()
        same = self.service.create_supplier(
            SupplierCreate(
                name="Урал Ограждения",
                region="Екатеринбург",
                email="sales@ural.example",
                categories=["ограждение"],
            )
        )
        other = self.service.create_supplier(
            SupplierCreate(
                name="Черноземье Ограждения",
                region="Воронеж",
                email="sales@voronezh.example",
                categories=["ограждение"],
            )
        )

        self.assertEqual(lot["cluster"], "cluster_2")
        self.assertEqual(same["cluster"], "cluster_2")
        self.assertEqual(other["cluster"], "cluster_1")
        self.assertEqual(
            [row["id"] for row in self.service.match_suppliers(lot["id"])],
            [same["id"]],
        )
        with self.assertRaisesRegex(ValueError, "cluster must match"):
            self.service.create_campaign(
                lot["id"], CampaignCreate(supplier_ids=[same["id"], other["id"]])
            )
        self.assertEqual(self.service.list_outbox(), [])

    def test_max_channel_creates_draft_but_never_sends(self):
        lot = self._lot()
        supplier = self.service.create_supplier(
            SupplierCreate(
                name="Урал MAX",
                region="Свердловская область",
                max_contact="max://ural-fence",
                categories=["ограждение"],
            )
        )
        campaign = self.service.create_campaign(
            lot["id"],
            CampaignCreate(supplier_ids=[supplier["id"]], channel="max"),
        )
        self.assertEqual(campaign["messages"][0]["channel"], "max")
        self.assertEqual(campaign["messages"][0]["status"], "draft")

    def test_item_evidence_survives_human_approval(self):
        project = self.service.create_project(
            ProjectCreate(
                name="Проект",
                region="Свердловская область",
                delivery_address="адрес уточняется",
            )
        )
        document = self.service.register_source_document(
            filename="project.pdf",
            content=b"%PDF-1.4\ntraceable schedule",
            document_type="project_section",
            project_id=project["id"],
        )
        suggestion = self.service.register_procurement_suggestions(
            document["id"],
            [
                ProcurementSuggestionCreate(
                    section_code="АС",
                    section_name="Ограждение",
                    lot_title="Панели",
                    confidence=0.99,
                    items=[
                        LotItemCreate(
                            name="Панель Булат",
                            quantity=9,
                            unit="шт.",
                            source_document_id=document["id"],
                            source_page=13,
                            source_reference="Спецификация, поз. П1",
                        )
                    ],
                )
            ],
        )[0]
        approved = self.service.approve_procurement_suggestion(
            suggestion["id"],
            ProcurementSuggestionApproval(
                response_deadline=date.today() + timedelta(days=7),
                approved_by="Начальник снабжения",
            ),
        )
        item = approved["lot"]["items"][0]
        self.assertEqual(item["source_document_id"], document["id"])
        self.assertEqual(item["source_page"], 13)
        self.assertEqual(item["source_reference"], "Спецификация, поз. П1")

    def test_region_helpers_cover_two_non_crossing_clusters(self):
        self.assertEqual(infer_cluster("Тамбовская область"), "cluster_1")
        self.assertEqual(infer_cluster("ЯНАО"), "cluster_2")
        self.assertEqual(infer_cluster("ХМАО — Югра"), "cluster_2")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            resolve_cluster("Омская область", "cluster_1")


class RankingPolicyTests(unittest.TestCase):
    def test_price_delivery_vat_weights_are_explainable(self):
        rows = [
            {
                "supplier_name": "A",
                "compliant": True,
                "total_cost": 100,
                "lead_days": 0,
                "vat_included": True,
            },
            {
                "supplier_name": "B",
                "compliant": True,
                "total_cost": 100,
                "lead_days": 0,
                "vat_included": False,
            },
        ]
        ranked = rank_quotes(rows)
        self.assertEqual(ranked[0]["score"], 100.0)
        self.assertEqual(
            ranked[0]["score_breakdown"], {"price": 60.0, "delivery": 25.0, "vat": 15.0}
        )
        self.assertEqual(ranked[1]["score"], 85.0)

    def test_ui_exposes_services_certificates_and_max_without_whatsapp(self):
        html = (
            Path(__file__).parents[1] / "procurement" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('data-view="services"', html)
        self.assertIn('data-view="certificates"', html)
        self.assertIn('<option value="max">MAX</option>', html)
        self.assertNotIn('value="whatsapp"', html)


if __name__ == "__main__":
    unittest.main()
