"""Isolated fixtures for cluster enforcement, human approval and no-send adapters."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from procurement.db import Database
from procurement.models import CampaignCreate, LotCreate, LotItemCreate, ProjectCreate, QuoteCreate, QuoteItemCreate, SupplierCreate, TemplateUpsert
from procurement.service import ConflictError, ProcurementService


@pytest.fixture
def procurement(tmp_path):
    database = Database(str(tmp_path / "sandbox.db"))
    database.initialize()
    service = ProcurementService(database)
    project = service.create_project(ProjectCreate(name="Synthetic project", region="Воронеж", delivery_address="Test address"))
    lot = service.create_lot(LotCreate(project_id=project["id"], title="Synthetic materials", region="Воронеж",
        delivery_address="Test address", response_deadline=date.today() + timedelta(days=7),
        items=[LotItemCreate(name="Тестовый кабель", quantity=10, unit="м")]))
    suppliers = [service.create_supplier(SupplierCreate(name=name, region=region, tax_id=tax,
        email="fixture@example.invalid", telegram="test-only", max_contact="test-only", categories=["кабель"]))
        for name, region, tax in [("South", "Воронеж", "3600000001"), ("Ural", "Екатеринбург", "6600000001")]]
    return database, service, project, lot, suppliers


def quote(supplier, lot):
    return QuoteCreate(supplier_id=supplier["id"], currency="RUB", vat_included=True,
        items=[QuoteItemCreate(lot_item_id=lot["items"][0]["id"], unit_price=10)])


@pytest.mark.parametrize("lot_cluster,project_cluster", [("", "cluster_2"), ("cluster_2", ""), ("", ""), ("cluster_1", "cluster_2")])
@pytest.mark.parametrize("operation", ["match", "campaign", "quote", "comparison"])
def test_missing_or_conflicting_cluster_fails_without_business_writes(procurement, lot_cluster, project_cluster, operation):
    database, service, project, lot, suppliers = procurement
    with database.connection() as connection:
        connection.execute("UPDATE lots SET cluster=? WHERE id=?", (lot_cluster, lot["id"]))
        connection.execute("UPDATE projects SET cluster=? WHERE id=?", (project_cluster, project["id"]))
    audit_before = database.one("SELECT count(*) AS n FROM audit_log")["n"]
    operations = {"match": lambda: service.match_suppliers(lot["id"]),
        "campaign": lambda: service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]])),
        "quote": lambda: service.add_quote(lot["id"], quote(suppliers[0], lot)),
        "comparison": lambda: service.comparison(lot["id"])}
    with pytest.raises(ValueError, match="cluster"):
        operations[operation]()
    for table in ("campaigns", "outbox_messages", "quotes"):
        assert database.one(f"SELECT count(*) AS n FROM {table}")["n"] == 0
    assert database.one("SELECT count(*) AS n FROM audit_log")["n"] == audit_before


def test_cross_cluster_cannot_enter_matching_campaign_or_quote(procurement):
    database, service, _, lot, suppliers = procurement
    assert [supplier["id"] for supplier in service.match_suppliers(lot["id"])] == [suppliers[0]["id"]]
    with pytest.raises(ValueError, match="supplier cluster must match"):
        service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[supplier["id"] for supplier in suppliers]))
    with pytest.raises(ValueError, match="supplier cluster must match"):
        service.add_quote(lot["id"], quote(suppliers[1], lot))
    assert database.one("SELECT count(*) AS n FROM outbox_messages")["n"] == 0


def test_legacy_cross_cluster_quote_cannot_be_ranked(procurement):
    database, service, _, lot, suppliers = procurement
    row = service.add_quote(lot["id"], quote(suppliers[0], lot))
    with database.connection() as connection:
        connection.execute("UPDATE quotes SET supplier_id=? WHERE id=?", (suppliers[1]["id"], row["id"]))
    with pytest.raises(ValueError, match="stored quote supplier cluster"):
        service.comparison(lot["id"])


@pytest.mark.parametrize("channel", ["email", "max", "telegram"])
def test_channel_sandbox_requires_approval_never_sends_and_reuses_receipt(procurement, monkeypatch, channel):
    import socket
    database, service, _, lot, suppliers = procurement
    def no_network(*args, **kwargs):
        raise AssertionError("sandbox must not open a network connection")
    monkeypatch.setattr(socket, "socket", no_network)
    campaign = service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]], channel=channel))
    message = campaign["messages"][0]
    with pytest.raises(ConflictError, match="human approval"):
        service.simulate_outbox(message["id"])
    assert database.one("SELECT count(*) AS n FROM sandbox_deliveries")["n"] == 0
    service.approve_message(message["id"], "human-test-reviewer")
    service.approve_message(message["id"], "human-test-reviewer")
    first = service.simulate_outbox(message["id"])
    assert first["channel"] == channel and first["mode"] == "sandbox"
    assert first["status"] == "simulated" and first["external_send"] is False
    # New service/connection simulates a worker replacement; state is not process-local.
    second = ProcurementService(Database(database.path)).simulate_outbox(message["id"])
    assert second == first
    assert database.one("SELECT count(*) AS n FROM sandbox_deliveries")["n"] == 1
    assert database.one("SELECT status FROM outbox_messages WHERE id=?", (message["id"],))["status"] == "approved"


def test_repeated_and_parallel_campaigns_create_one_outbox(procurement):
    database, service, _, lot, suppliers = procurement
    request = CampaignCreate(supplier_ids=[suppliers[0]["id"], suppliers[0]["id"]])
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: service.create_campaign(lot["id"], request), range(10)))
    assert len({result["id"] for result in results}) == 1
    assert database.one("SELECT count(*) AS n FROM campaigns")["n"] == 1
    assert database.one("SELECT count(*) AS n FROM outbox_messages")["n"] == 1
    service = ProcurementService(Database(database.path))
    assert service.create_campaign(lot["id"], request)["id"] == results[0]["id"]


def test_explicit_key_conflict_and_changed_content_gets_new_human_review(procurement):
    database, service, _, lot, suppliers = procurement
    data = CampaignCreate(supplier_ids=[suppliers[0]["id"]], idempotency_key="fixture-request")
    first = service.create_campaign(lot["id"], data)
    service.upsert_template("rfq-email", TemplateUpsert(name="Revised test RFQ", subject="Revised subject",
                                                       body="Revised body requiring a new human review."))
    with pytest.raises(ConflictError, match="different campaign content"):
        service.create_campaign(lot["id"], data)
    second = service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]]))
    assert first["id"] != second["id"] and second["messages"][0]["status"] == "draft"
    assert database.one("SELECT count(*) AS n FROM sandbox_deliveries")["n"] == 0


@pytest.mark.parametrize("change", ["body", "recipient", "supplier_cluster", "lot_cluster", "project_cluster", "approved_actor"])
def test_post_approval_drift_cannot_be_simulated(procurement, change):
    database, service, project, lot, suppliers = procurement
    message = service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]]))["messages"][0]
    service.approve_message(message["id"], "human-test-reviewer")
    with database.connection() as connection:
        if change in {"body", "recipient"}:
            connection.execute(f"UPDATE outbox_messages SET {change}='changed' WHERE id=?", (message["id"],))
        elif change == "approved_actor":
            connection.execute("UPDATE outbox_messages SET approved_by='forged' WHERE id=?", (message["id"],))
        elif change == "supplier_cluster":
            connection.execute("UPDATE suppliers SET cluster='cluster_1' WHERE id=?", (suppliers[0]["id"],))
        elif change == "lot_cluster":
            connection.execute("UPDATE lots SET cluster='' WHERE id=?", (lot["id"],))
        else:
            connection.execute("UPDATE projects SET cluster='' WHERE id=?", (project["id"],))
    with pytest.raises((ValueError, ConflictError)):
        service.simulate_outbox(message["id"])
    assert database.one("SELECT count(*) AS n FROM sandbox_deliveries")["n"] == 0


def test_parallel_simulations_reuse_one_persisted_receipt(procurement):
    database, service, _, lot, suppliers = procurement
    message = service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]]))["messages"][0]
    service.approve_message(message["id"], "human-test-reviewer")
    with ThreadPoolExecutor(max_workers=10) as pool:
        receipts = list(pool.map(lambda _: service.simulate_outbox(message["id"]), range(10)))
    assert len({receipt["receipt_id"] for receipt in receipts}) == 1
    assert database.one("SELECT count(*) AS n FROM sandbox_deliveries")["n"] == 1
    assert database.one("SELECT count(*) AS n FROM audit_log WHERE action='simulated'")["n"] == 1


def test_legacy_approved_without_content_fingerprint_is_not_simulated(procurement):
    database, service, _, lot, suppliers = procurement
    message = service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]]))["messages"][0]
    with database.connection() as connection:
        connection.execute("UPDATE outbox_messages SET status='approved',approved_by='legacy',approved_at='2026-09-04' WHERE id=?", (message["id"],))
    with pytest.raises(ConflictError):
        service.simulate_outbox(message["id"])
    assert database.one("SELECT count(*) AS n FROM sandbox_deliveries")["n"] == 0


def test_different_request_keys_for_identical_content_do_not_duplicate_outbox(procurement):
    database, service, _, lot, suppliers = procurement
    first = service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]], idempotency_key="first"))
    second = service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]], idempotency_key="second"))
    assert first["id"] == second["id"]
    assert database.one("SELECT count(*) AS n FROM outbox_messages")["n"] == 1
    assert database.one("SELECT count(*) AS n FROM campaign_requests")["n"] == 2


def test_matching_legacy_campaign_requires_review_instead_of_duplicate_or_backfill(procurement):
    database, service, _, lot, suppliers = procurement
    data = CampaignCreate(supplier_ids=[suppliers[0]["id"]])
    first = service.create_campaign(lot["id"], data)
    with database.connection() as connection:
        connection.execute("DELETE FROM campaign_requests WHERE campaign_id=?", (first["id"],))
    before = service.get_campaign(first["id"])
    with pytest.raises(ConflictError, match="matching legacy campaign"):
        service.create_campaign(lot["id"], data)
    assert service.get_campaign(first["id"]) == before
    assert database.one("SELECT count(*) AS n FROM outbox_messages")["n"] == 1
    assert database.one("SELECT count(*) AS n FROM campaign_requests")["n"] == 0


@pytest.mark.parametrize("actor", ["", " ", "system"])
def test_automatic_or_missing_actor_is_not_human_approval(procurement, actor):
    database, service, _, lot, suppliers = procurement
    message = service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]]))["messages"][0]
    with pytest.raises(ValueError, match="human approval actor"):
        service.approve_message(message["id"], actor)
    assert database.one("SELECT count(*) AS n FROM outbox_approvals")["n"] == 0


def test_repeated_campaign_does_not_reuse_drifted_outbox_content(procurement):
    database, service, _, lot, suppliers = procurement
    data = CampaignCreate(supplier_ids=[suppliers[0]["id"]])
    first = service.create_campaign(lot["id"], data)
    with database.connection() as connection:
        connection.execute("UPDATE outbox_messages SET body='changed after creation' WHERE campaign_id=?", (first["id"],))
    with pytest.raises(ConflictError, match="stored campaign content changed"):
        service.create_campaign(lot["id"], data)
    assert database.one("SELECT count(*) AS n FROM outbox_messages")["n"] == 1


@pytest.mark.parametrize("operation", ["campaign", "quote"])
def test_cluster_is_rechecked_under_write_lock(procurement, monkeypatch, operation):
    database, service, _, lot, suppliers = procurement
    original = service._supplier_cluster
    calls = 0
    def race(supplier, cluster):
        nonlocal calls
        calls += 1
        original(supplier, cluster)
        if calls == 1:
            # Simulate drift after preliminary validation, before the write lock.
            with database.connection() as connection:
                connection.execute("UPDATE suppliers SET cluster='cluster_1' WHERE id=?", (suppliers[0]["id"],))
    monkeypatch.setattr(service, "_supplier_cluster", race)
    with pytest.raises(ValueError, match="supplier cluster must match"):
        if operation == "campaign":
            service.create_campaign(lot["id"], CampaignCreate(supplier_ids=[suppliers[0]["id"]]))
        else:
            service.add_quote(lot["id"], quote(suppliers[0], lot))
    assert database.one("SELECT count(*) AS n FROM outbox_messages")["n"] == 0
    assert database.one("SELECT count(*) AS n FROM quotes")["n"] == 0
