"""Synthetic backend contract regressions for the human-review UI. Temp DB only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import procurement.app as application
from procurement.db import Database
from procurement.models import CampaignCreate, LotCreate, LotItemCreate, ProjectCreate, QuoteCreate, QuoteItemCreate, SupplierCreate
from procurement.service import ConflictError, NotFoundError, ProcurementService


def synthetic_workbook(suffix=''):
    workbook = Workbook()
    workbook.active.append(['Счет №314 RUB без НДС'])
    workbook.active.append(['Наименование', 'Цена', 'Ед.'])
    workbook.active.append(['Кабель тестовый' + suffix, '100', 'м'])
    workbook.active.append(['Болт тестовый' + suffix, '200', 'шт'])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


@pytest.fixture
def review(tmp_path, monkeypatch):
    database = Database(str(tmp_path / 'synthetic-review.sqlite3'))
    database.initialize()
    service = ProcurementService(database)
    monkeypatch.setattr(application, 'db', database)
    monkeypatch.setattr(application, 'service', service)
    monkeypatch.setattr(application, 'settings', replace(application.settings, environment='development',
        api_key='', auth_secret='', admin_username='', admin_password_hash='', sso_enabled=False))
    batch = service.create_import_batch([('synthetic-review.xlsx', synthetic_workbook())])
    assert len(batch['price_history_entries']) == 2
    with TestClient(application.app) as client:
        yield database, service, batch, client


def test_batch_counts_partial_selection_confirmed_filter_and_provenance(review):
    database, service, batch, client = review
    row = client.get('/api/imports').json()[0]
    assert (row['price_entry_count'], row['draft_entry_count'], row['confirmed_entry_count']) == (2, 2, 0)
    assert row['supplier_draft_count'] == len(batch['supplier_drafts'])
    eid = batch['price_history_entries'][0]['id']
    response = client.post(f"/api/imports/{batch['id']}/confirm", json={'entry_ids': [eid], 'confirmed_by': 'Synthetic reviewer'})
    assert response.status_code == 200 and response.json() == {'confirmed': 1}
    row = service.list_import_batches()[0]
    assert (row['price_entry_count'], row['draft_entry_count'], row['confirmed_entry_count']) == (2, 1, 1)
    for status, count in [('draft', 1), ('confirmed', 1), ('', 2)]:
        entries = client.get('/api/price-history-entries', params={'status': status}).json()
        assert len(entries) == count
        assert all(e['source_filename'] == 'synthetic-review.xlsx' and e['source_document_type'] == 'invoice' for e in entries)
        assert all('storage_path' not in e for e in entries)
    confirmed = client.get('/api/price-history-entries?status=confirmed').json()[0]
    assert confirmed['confirmed_by'] == 'Synthetic reviewer' and confirmed['confirmed_at']
    assert database.one('SELECT count(*) AS n FROM purchase_history')['n'] == 0


def test_foreign_entry_selection_has_no_partial_writes_or_audit(review):
    database, service, batch, _ = review
    other = service.create_import_batch([('other.xlsx', synthetic_workbook(' другого пакета'))])
    foreign_id = other['price_history_entries'][0]['id']
    assert foreign_id not in [row['id'] for row in batch['price_history_entries']]
    before = database.all('SELECT * FROM price_history_entries ORDER BY id')
    audits = database.all('SELECT * FROM audit_log ORDER BY id')
    with pytest.raises(NotFoundError):
        service.confirm_batch_entries(batch['id'], [batch['price_history_entries'][0]['id'], foreign_id], 'Reviewer')
    assert database.all('SELECT * FROM price_history_entries ORDER BY id') == before
    assert database.all('SELECT * FROM audit_log ORDER BY id') == audits


def test_repeated_and_duplicate_selection_preserves_original_actor_timestamp(review):
    database, service, batch, _ = review
    ids = [entry['id'] for entry in batch['price_history_entries']]
    assert service.confirm_batch_entries(batch['id'], ids + ids, 'First reviewer') == {'confirmed': 2}
    before = service.get_import_batch(batch['id'])
    audits = database.all('SELECT * FROM audit_log ORDER BY id')
    assert service.confirm_batch_entries(batch['id'], ids, 'Second reviewer') == {'confirmed': 0}
    assert service.get_import_batch(batch['id']) == before
    assert database.all('SELECT * FROM audit_log ORDER BY id') == audits


def test_parallel_confirmation_changes_each_entry_once(review):
    database, service, batch, _ = review
    ids = [entry['id'] for entry in batch['price_history_entries']]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: service.confirm_batch_entries(batch['id'], ids, f'Reviewer {n}'), range(4)))
    assert sorted(row['confirmed'] for row in results) == [0, 0, 0, 2]
    assert database.one("SELECT count(*) AS n FROM audit_log WHERE action='entries_confirmed'")['n'] == 1


@pytest.mark.parametrize('selection', [[], [0], [-1], [True], ['1'], list(range(1, 502))])
def test_invalid_selection_is_rejected_without_writes(review, selection):
    database, service, batch, _ = review
    before = database.all('SELECT * FROM price_history_entries')
    with pytest.raises(ValueError):
        service.confirm_batch_entries(batch['id'], selection, 'Reviewer')
    assert database.all('SELECT * FROM price_history_entries') == before


def test_rejected_row_cannot_be_reconfirmed_or_cause_partial_commit(review):
    database, service, batch, _ = review
    ids = [entry['id'] for entry in batch['price_history_entries']]
    with database.connection() as conn:
        conn.execute("UPDATE price_history_entries SET status='rejected' WHERE id=?", (ids[1],))
    with pytest.raises(ConflictError):
        service.confirm_batch_entries(batch['id'], ids, 'Reviewer')
    assert service.get_import_batch(batch['id'])['price_history_entries'][0]['status'] == 'draft'


def make_lot(service):
    project = service.create_project(ProjectCreate(name='Synthetic', region='Воронеж', delivery_address='Test'))
    lot = service.create_lot(LotCreate(project_id=project['id'], title='Synthetic cable', region='Воронеж',
        delivery_address='Test', response_deadline=date.today() + timedelta(days=3),
        items=[LotItemCreate(name='Кабель тестовый', quantity=2, unit='м')]))
    supplier = service.create_supplier(SupplierCreate(name='Synthetic supplier', region='Воронеж', tax_id='3600000022',
        email='fixture@example.invalid', telegram='test-only', max_contact='test-only'))
    return lot, supplier


@pytest.mark.parametrize('channel', ['email', 'max', 'telegram'])
def test_outbox_full_body_and_persistent_receipt_after_service_reload(review, channel):
    database, service, _, client = review
    lot, supplier = make_lot(service)
    service.create_campaign(lot['id'], CampaignCreate(supplier_ids=[supplier['id']], channel=channel))
    message = client.get('/api/outbox').json()[0]
    assert message['body'] and message['recipient'] and message['sandbox_receipt'] is None
    mid = message['id']
    assert client.post(f'/api/outbox/{mid}/simulate').status_code == 409
    assert client.post(f'/api/outbox/{mid}/approve', json={'approved_by': 'Human reviewer'}).status_code == 200
    response = client.post(f'/api/outbox/{mid}/simulate')
    assert response.status_code == 200
    receipt = response.json()
    assert receipt['external_send'] is False and receipt['channel'] == channel
    reloaded = ProcurementService(database).list_outbox()[0]
    assert reloaded['sandbox_receipt'] == receipt and reloaded['status'] == 'approved'
    assert client.post(f'/api/outbox/{mid}/simulate').json() == receipt
    assert database.one('SELECT count(*) AS n FROM sandbox_deliveries')['n'] == 1


def test_quote_comparison_exposes_unmodified_financial_inputs(review):
    _, service, _, client = review
    lot, supplier = make_lot(service)
    service.add_quote(lot['id'], QuoteCreate(supplier_id=supplier['id'], currency='RUB', vat_included=False,
        delivery_cost=17, lead_days=4, payment_terms='50% аванс, без пересчёта',
        items=[QuoteItemCreate(lot_item_id=lot['items'][0]['id'], unit_price=100)]))
    row = client.get(f"/api/lots/{lot['id']}/comparison").json()['quotes'][0]
    assert row['subtotal'] == 200 and row['delivery_cost'] == 17 and row['total_cost'] == 217
    assert row['vat_included'] is False and row['currency'] == 'RUB'
    assert row['payment_terms'] == '50% аванс, без пересчёта'


def test_invalid_filter_returns_explicit_error(review):
    assert review[3].get('/api/price-history-entries?status=paid').status_code == 422
