"""Synthetic fixtures only: no business files, credentials, network, or live DB."""
from __future__ import annotations

import io
from dataclasses import replace
from datetime import date, timedelta

import pytest
from openpyxl import Workbook

from procurement.db import Database
from procurement.imports import extract_from_xlsx
from procurement.models import LotCreate, LotItemCreate, ProjectCreate, QuoteCreate, QuoteItemCreate, SupplierCreate
from procurement.ranking import rank_quotes
from procurement.service import ProcurementService


def workbook_bytes(sheets):
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets:
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


@pytest.mark.parametrize("heading,currency,vat,headers,price,expected", [
    ("Прайс-лист RUB с НДС", "RUB", True, ("Наименование", "Цена", "Ед."), "120.50", "120.50"),
    ("Прайс-лист USD без НДС", "USD", False, ("Наименование", "Цена", "Ед."), "123,45", "123.45"),
    ("Коммерческое предложение EUR с НДС", "EUR", True, ("Товар", "Стоимость", "Ед."), "99.99", "99.99"),
    ("Счет №100 KZT без НДС", "KZT", False, ("Материал", "Цена", "Ед."), "5 000,00", "5000.00"),
    ("Price list USD без НДС", "USD", False, ("Item", "Unit price", "Unit"), "70.00", "70.00"),
    ("Прайс-лист в тенге с НДС", "KZT", True, ("Номенклатура", "Цена", "Единица"), "3\u00a0123,00", "3123.00"),
    ("Прайс-лист € без НДС", "EUR", False, ("Описание", "Стоимость", "Unit"), "2\u202f123,70", "2123.70"),
    ("Счет №101 ₽ включая НДС", "RUB", True, ("Товар", "Цена за ед", "Ед.изм."), 45, "45"),
    ("Прайс-лист $ НДС включён", "USD", True, ("Продукция", "Цена", "Ед."), 2.5, "2.5"),
    ("Прайс-лист руб. без НДС", "RUB", False, ("Name", "Price", "Unit"), "12", "12"),
])
def test_ten_import_money_fixtures(heading, currency, vat, headers, price, expected):
    payload = workbook_bytes([("Prices", [(heading,), headers, ("Кабель тестовый", price, "м")])])
    result = extract_from_xlsx(payload, "synthetic-price.xlsx")
    assert not result.errors
    assert len(result.items) == 1
    item = result.items[0]
    assert (item.currency, item.vat_included, item.unit_price) == (currency, vat, expected)
    assert (result.currency, result.vat_included) == (currency, vat)
    assert (item.source_sheet, item.source_row) == ("Prices", 3)


def test_distinct_sheet_currency_and_vat_are_not_overwritten_by_document_summary():
    payload = workbook_bytes([
        ("USD", [("USD без НДС",), ("Наименование", "Цена"), ("Кабель A", 100)]),
        ("EUR", [("EUR с НДС",), ("Наименование", "Цена"), ("Кабель B", 90)]),
    ])
    result = extract_from_xlsx(payload, "multi.xlsx")
    assert result.currency == "MIXED"
    assert [(item.currency, item.vat_included) for item in result.items] == [("USD", False), ("EUR", True)]


@pytest.mark.parametrize("heading,price,expected_error", [
    ("USD EUR без НДС", "12", "ambiguous currency"),
    ("RUB с НДС и без НДС", "12", "conflicting VAT"),
    ("RUB с НДС", "-12", "invalid or ambiguous price"),
    ("RUB с НДС", "1,234.56", "invalid or ambiguous price"),
    ("RUB с НДС", "0", "must be positive"),
    ("RUB с НДС", "12 USD", "currency conflicts"),
])
def test_ambiguous_or_invalid_import_money_requires_review(heading, price, expected_error):
    payload = workbook_bytes([("Prices", [(heading,), ("Наименование", "Цена"), ("Кабель тестовый", price)])])
    result = extract_from_xlsx(payload, "review.xlsx")
    assert result.items == []
    assert any(expected_error in error for error in result.errors)


def test_missing_price_header_and_malformed_xlsx_do_not_invent_prices():
    payload = workbook_bytes([("Spec", [("Наименование", "Количество"), ("Кабель тестовый", 12)])])
    assert extract_from_xlsx(payload, "spec.xlsx").items == []
    result = extract_from_xlsx(b"not-an-office-file", "broken.xlsx")
    assert result.items == [] and result.errors


@pytest.fixture
def workflow(tmp_path):
    database = Database(str(tmp_path / "synthetic.db"))
    database.initialize()
    service = ProcurementService(database)
    project = service.create_project(ProjectCreate(name="Synthetic project", region="Воронеж", delivery_address="Test address"))
    supplier = service.create_supplier(SupplierCreate(name="Synthetic supplier", region="Воронеж", tax_id="3600000001"))
    lot = service.create_lot(LotCreate(project_id=project["id"], title="Synthetic lot", region="Воронеж",
        delivery_address="Test address", response_deadline=date.today() + timedelta(days=7), currency="RUB",
        items=[LotItemCreate(name="Кабель тестовый", quantity=10, unit="м")]))
    return database, service, supplier, lot


def quote_for(supplier, lot, currency):
    return QuoteCreate(supplier_id=supplier["id"], currency=currency, vat_included=True,
        items=[QuoteItemCreate(lot_item_id=lot["items"][0]["id"], unit_price=100)])


def test_batch_persists_extracted_money_and_deduplicates(workflow):
    database, service, _, _ = workflow
    payload = workbook_bytes([("Prices", [("USD без НДС",), ("Наименование", "Цена"), ("Кабель тестовый", "120,50")])])
    batch = service.create_import_batch([("synthetic.xlsx", payload)], created_by="test-actor")
    entry = batch["price_history_entries"][0]
    assert (entry["currency"], entry["vat_included"], entry["unit_price"], entry["status"]) == ("USD", 0, "120.50", "draft")
    service.create_import_batch([("renamed.xlsx", payload)], created_by="test-actor")
    assert database.one("SELECT count(*) AS n FROM price_history_entries")["n"] == 1


def test_backend_rejects_foreign_currency_before_any_write(workflow):
    database, service, supplier, lot = workflow
    audit_count = database.one("SELECT count(*) AS n FROM audit_log")["n"]
    with pytest.raises(ValueError, match="currency must match"):
        service.add_quote(lot["id"], quote_for(supplier, lot, "USD"))
    assert database.one("SELECT count(*) AS n FROM quotes")["n"] == 0
    assert database.one("SELECT count(*) AS n FROM audit_log")["n"] == audit_count


def test_existing_incompatible_quote_is_not_ranked(workflow):
    database, service, supplier, lot = workflow
    quote = service.add_quote(lot["id"], quote_for(supplier, lot, "RUB"))
    # Deliberate legacy data fixture, never a production repair.
    with database.connection() as connection:
        connection.execute("UPDATE quotes SET currency='USD' WHERE id=?", (quote["id"],))
    with pytest.raises(ValueError, match="stored quote currency"):
        service.comparison(lot["id"])


@pytest.mark.parametrize("currency,total", [
    ("RUB", 100), ("RUB", 500), ("USD", 100), ("USD", 300), ("EUR", 80),
    ("EUR", 800), ("KZT", 2000), ("KZT", 5000), ("RUB", 0.01), ("USD", 0.25),
])
def test_ten_same_currency_comparison_fixtures(currency, total):
    common = {"currency": currency, "compliant": True, "lead_days": 0, "vat_included": True}
    ranked = rank_quotes([{**common, "total_cost": total * 2}, {**common, "total_cost": total}])
    assert ranked[0]["total_cost"] == total
    assert ranked[0]["score"] == 100
    assert [row["rank"] for row in ranked] == [1, 2]


@pytest.mark.parametrize("currency", ["USD", "EUR", "KZT", None])
def test_ranking_rejects_mixed_or_partially_missing_currency(currency):
    common = {"compliant": True, "lead_days": 0, "vat_included": True, "total_cost": 100}
    with pytest.raises(ValueError, match="one currency"):
        rank_quotes([{**common, "currency": "RUB"}, {**common, "currency": currency}])


def test_quote_api_returns_422_for_foreign_currency(workflow, monkeypatch):
    from fastapi.testclient import TestClient
    import procurement.app as application
    database, service, supplier, lot = workflow
    monkeypatch.setattr(application, "db", database)
    monkeypatch.setattr(application, "service", service)
    monkeypatch.setattr(application, "settings", replace(application.settings, environment="development",
        api_key="", auth_secret="", admin_username="", admin_password_hash=""))
    with TestClient(application.app) as client:
        response = client.post(f"/api/lots/{lot['id']}/quotes", json=quote_for(supplier, lot, "USD").model_dump(mode="json"))
    assert response.status_code == 422
    assert "currency must match" in response.json()["detail"]
    assert database.one("SELECT count(*) AS n FROM quotes")["n"] == 0
