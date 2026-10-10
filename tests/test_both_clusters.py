"""A supplier that works in both clusters is offered to purchases of either confirmed cluster."""
from datetime import date, timedelta

import pytest

from procurement.db import Database
from procurement.launch_workflow import LaunchWorkflow
from procurement.models import CampaignCreate, LotCreate, LotItemCreate, ProjectCreate, QuoteCreate, QuoteItemCreate, SupplierCreate
from procurement.service import ConflictError, ProcurementService


@pytest.fixture
def two_clusters(tmp_path):
    database = Database(str(tmp_path / "both.db"))
    database.initialize()
    service = ProcurementService(database)
    lots = {}
    for cluster, region in (("cluster_2", "Воронеж"), ("cluster_1", "Екатеринбург")):
        project = service.create_project(ProjectCreate(name=f"Проект {region}", region=region, delivery_address="Адрес"))
        lots[cluster] = service.create_lot(LotCreate(project_id=project["id"], title="Кабель", region=region,
            delivery_address="Адрес", response_deadline=date.today() + timedelta(days=7),
            items=[LotItemCreate(name="Тестовый кабель", quantity=10, unit="м")]))
    both = service.create_supplier(SupplierCreate(name="Оба", region="Москва", cluster="cluster_1", both_clusters=True,
        tax_id="7707083893", email="both@example.invalid", categories=["кабель"]))
    ural = service.create_supplier(SupplierCreate(name="Урал", region="Екатеринбург", tax_id="6600000001",
        email="ural@example.invalid", categories=["кабель"]))
    return database, service, lots, both, ural


def quote(supplier, lot):
    return QuoteCreate(supplier_id=supplier["id"], currency="RUB", vat_included=True,
        items=[QuoteItemCreate(lot_item_id=lot["items"][0]["id"], unit_price=10)])


def test_flag_is_stored_and_read_back(two_clusters):
    _, service, _, both, ural = two_clusters
    assert both["both_clusters"] is True and ural["both_clusters"] is False
    assert {s["name"]: s["both_clusters"] for s in service.list_suppliers()} == {"Оба": True, "Урал": False}


@pytest.mark.parametrize("cluster", ["cluster_1", "cluster_2"])
def test_both_cluster_supplier_takes_part_in_either_cluster(two_clusters, cluster):
    database, service, lots, both, ural = two_clusters
    lot = lots[cluster]
    matched = {s["id"] for s in service.match_suppliers(lot["id"])}
    assert both["id"] in matched
    assert (ural["id"] in matched) == (cluster == "cluster_1")
    service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[both["id"]]))
    service.add_quote(lot["id"], quote(both, lot))
    assert [q["supplier_id"] for q in service.comparison(lot["id"])["quotes"]] == [both["id"]]
    with database.connection() as conn:
        assert service._outbox_context(conn, service.list_outbox()[0]["id"])


def test_single_cluster_supplier_is_still_blocked(two_clusters):
    _, service, lots, _, ural = two_clusters
    with pytest.raises(ValueError, match="supplier cluster must match"):
        service.create_campaign(lots["cluster_2"]["id"], CampaignCreate(supplier_ids=[ural["id"]]))
    with pytest.raises(ValueError, match="supplier cluster must match"):
        service.add_quote(lots["cluster_2"]["id"], quote(ural, lots["cluster_2"]))


def test_unconfirmed_lot_cluster_is_not_opened_by_the_flag(two_clusters):
    database, service, lots, both, _ = two_clusters
    with database.connection() as conn:
        conn.execute("UPDATE lots SET cluster='' WHERE id=?", (lots["cluster_2"]["id"],))
    with pytest.raises(ValueError, match="cluster"):
        service.create_campaign(lots["cluster_2"]["id"], CampaignCreate(supplier_ids=[both["id"]]))


def test_second_cluster_can_be_added_to_a_used_supplier_but_not_removed(two_clusters):
    database, service, lots, both, ural = two_clusters
    workflow = LaunchWorkflow(service)
    service.create_campaign(lots["cluster_1"]["id"], CampaignCreate(supplier_ids=[ural["id"]]))
    current = workflow.supplier(ural["id"])
    values = {k: current[k] for k in SupplierCreate.model_fields}
    edited = workflow.edit_supplier(ural["id"], {**values, "both_clusters": True}, current["revision"])
    assert edited["both_clusters"] is True and edited["cluster"] == "cluster_1"

    service.create_campaign(lots["cluster_2"]["id"], CampaignCreate(supplier_ids=[ural["id"]]))
    current = workflow.supplier(ural["id"])
    before = database.all("SELECT * FROM suppliers")
    with pytest.raises(ConflictError, match="убрать второй кластер"):
        workflow.edit_supplier(ural["id"], {**values, "both_clusters": False}, current["revision"])
    assert database.all("SELECT * FROM suppliers") == before


def test_unused_supplier_can_drop_the_second_cluster(two_clusters):
    _, service, _, both, _ = two_clusters
    workflow = LaunchWorkflow(service)
    current = workflow.supplier(both["id"])
    values = {k: current[k] for k in SupplierCreate.model_fields}
    assert workflow.edit_supplier(both["id"], {**values, "both_clusters": False}, current["revision"])["both_clusters"] is False


def test_supplier_import_keeps_the_flag(two_clusters):
    from procurement.table_ingest import read_table
    _, service, _, both, _ = two_clusters
    workflow = LaunchWorkflow(service)
    table = read_table("Фирма;ИНН;Регион\nОба обновлён;7707083893;Москва\n".encode(), "updated.csv")
    preview = workflow.supplier_preview(table, {"name": 0, "tax_id": 1, "region": 2})
    workflow.apply_import(preview["preview_id"], True)
    after = service.get_supplier(both["id"])
    assert after["name"] == "Оба обновлён" and after["both_clusters"] is True


def test_price_benchmark_uses_history_of_a_both_cluster_supplier(two_clusters):
    from procurement.models import PurchaseHistoryCreate
    _, service, lots, both, ural = two_clusters
    for supplier, price in ((both, 9), (ural, 7)):
        service.add_purchase_history(PurchaseHistoryCreate(supplier_id=supplier["id"], item_name="Тестовый кабель",
            quantity=5, unit="м", unit_price=price, currency="RUB", vat_included=True, purchased_on=date(2026, 9, 1),
            region="Воронеж", confirmed_by="Руководитель снабжения"))
    item = service.lot_price_benchmark(lots["cluster_2"]["id"])["items"][0]
    assert (item["history_count"], item["median_unit_price"]) == (1, 9.0)  # the Ural-only supplier stays out
