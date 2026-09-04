"""Synthetic regressions: never touch live files, credentials, or databases."""
from __future__ import annotations

import hashlib
import io
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from pydantic import ValidationError

import procurement.app as application
from procurement.db import Database
from procurement.imports import extract_document
from procurement.models import (LotCreate, LotItemCreate, ProjectCreate, PurchaseHistoryCreate,
    QuoteCreate, QuoteItemCreate, SupplierCreate, SupplierDraftConfirm)
from procurement.service import ProcurementService


@pytest.fixture
def workflow(tmp_path):
    database = Database(str(tmp_path / 'test.sqlite3'))
    database.initialize()
    service = ProcurementService(database)
    project = service.create_project(ProjectCreate(name='Synthetic project', region='Воронеж', delivery_address='Test address'))
    lot = service.create_lot(LotCreate(project_id=project['id'], title='Synthetic lot', region='Воронеж',
        delivery_address='Test address', response_deadline=date.today() + timedelta(days=1),
        items=[LotItemCreate(name='Кабель тестовый', unit='м', quantity=1)]))
    supplier = service.create_supplier(SupplierCreate(name='Synthetic supplier', region='Воронеж', tax_id='3600000011'))
    return database, service, lot, supplier


@pytest.fixture
def client(workflow, monkeypatch):
    database, service, _, _ = workflow
    monkeypatch.setattr(application, 'db', database)
    monkeypatch.setattr(application, 'service', service)
    monkeypatch.setattr(application, 'settings', replace(application.settings,
        environment='development', api_key='', auth_secret='', admin_username='', admin_password_hash='', sso_enabled=False))
    with TestClient(application.app) as test_client:
        yield test_client


def xlsx():
    book = Workbook()
    book.active.append(['Счет №42 RUB без НДС'])
    book.active.append(['Наименование', 'Цена', 'Ед.'])
    book.active.append(['Кабель тестовый', '100', 'м'])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue()


@pytest.mark.parametrize('payload', [b'\xd0\xcf\x11\xe0legacy', b'PKfake-xlsx-in-xls'])
def test_xls_is_rejected_before_parser_or_batch_write(workflow, monkeypatch, payload):
    def forbidden(*_):
        pytest.fail('XLS must never enter the XLSX parser')
    monkeypatch.setattr('procurement.imports.extract_from_xlsx', forbidden)
    with pytest.raises(ValueError, match=r'legacy \.xls'):
        extract_document(payload, 'legacy.XLS')
    database, service, _, _ = workflow
    with pytest.raises(ValueError, match=r'legacy \.xls'):
        service.create_import_batch([('legacy.xls', payload)])
    assert database.one('SELECT count(*) AS n FROM import_batches')['n'] == 0
    assert database.one('SELECT count(*) AS n FROM source_documents')['n'] == 0


def test_xls_api_returns_actionable_422_without_records(client, workflow):
    response = client.post('/api/imports/batch', files={'files': ('legacy.xls', xlsx())})
    assert response.status_code == 422 and 'convert to .xlsx' in response.json()['detail']
    assert workflow[0].one('SELECT count(*) AS n FROM import_batches')['n'] == 0


def money_model_data(model):
    if model is QuoteCreate:
        return {'supplier_id': 1, 'currency': 'RUB', 'vat_included': False,
            'items': [{'lot_item_id': 1, 'unit_price': 100}]}
    return {'item_name': 'Synthetic cable', 'quantity': 1, 'unit': 'm', 'unit_price': 100,
        'currency': 'RUB', 'vat_included': False, 'purchased_on': date.today(), 'confirmed_by': 'Reviewer'}


@pytest.mark.parametrize('model', [QuoteCreate, PurchaseHistoryCreate])
@pytest.mark.parametrize('mutation', ['missing_currency', 'missing_vat', 'null_vat', 'string_vat', 'integer_vat', 'empty_currency'])
def test_money_basis_must_be_explicit_and_strict(model, mutation):
    values = money_model_data(model)
    if mutation == 'missing_currency':
        values.pop('currency')
    elif mutation == 'missing_vat':
        values.pop('vat_included')
    elif mutation == 'empty_currency':
        values['currency'] = ''
    else:
        values['vat_included'] = {'null_vat': None, 'string_vat': 'false', 'integer_vat': 0}[mutation]
    with pytest.raises(ValidationError):
        model(**values)


@pytest.mark.parametrize('vat', [False, True])
def test_explicit_quote_and_purchase_vat_choice_is_preserved(client, workflow, vat):
    database, _, lot, supplier = workflow
    quote = money_model_data(QuoteCreate)
    quote.update(supplier_id=supplier['id'], vat_included=vat,
        items=[{'lot_item_id': lot['items'][0]['id'], 'unit_price': 100}])
    response = client.post(f"/api/lots/{lot['id']}/quotes", json=quote)
    assert response.status_code == 201 and response.json()['vat_included'] == int(vat)
    purchase = money_model_data(PurchaseHistoryCreate)
    purchase.update(vat_included=vat, purchased_on=date.today().isoformat())
    assert client.post('/api/price-history', json=purchase).status_code == 201
    assert database.one('SELECT vat_included FROM purchase_history')['vat_included'] == int(vat)


def test_missing_quote_money_basis_is_rejected_by_api(client, workflow):
    database, _, lot, supplier = workflow
    body = {'supplier_id': supplier['id'], 'items': [{'lot_item_id': lot['items'][0]['id'], 'unit_price': 100}]}
    assert client.post(f"/api/lots/{lot['id']}/quotes", json=body).status_code == 422
    assert database.one('SELECT count(*) AS n FROM quotes')['n'] == 0


def history(service, supplier, *, price=100, vat=True, region='Воронеж', invoice='SYNTHETIC-1'):
    return service.add_purchase_history(PurchaseHistoryCreate(supplier_id=supplier['id'] if supplier else None,
        item_name='Кабель тестовый', unit='м', quantity=1, unit_price=price, currency='RUB',
        vat_included=vat, purchased_on=date.today(), region=region, invoice_number=invoice, confirmed_by='Reviewer'))


@pytest.mark.parametrize('region', ['', 'Москва', 'Краснодар', 'Воронежская область', 'Воронеж Краснодар', 'Екатеринбург'])
def test_history_from_missing_other_or_alias_region_is_excluded(workflow, region):
    _, service, lot, supplier = workflow
    valid = history(service, supplier)
    history(service, supplier, price=1, region=region, invoice='SYNTHETIC-OTHER')
    item = service.lot_price_benchmark(lot['id'])['items'][0]
    assert item['median_unit_price'] == 100
    assert item['source_purchase_ids'] == [valid['id']]


@pytest.mark.parametrize('cluster', ['', 'cluster_1'])
def test_history_supplier_cluster_drift_is_excluded(workflow, cluster):
    database, service, lot, supplier = workflow
    history(service, supplier)
    with database.connection() as connection:
        connection.execute('UPDATE suppliers SET cluster=? WHERE id=?', (cluster, supplier['id']))
    assert service.lot_price_benchmark(lot['id'])['items'][0]['history_count'] == 0


def test_history_without_supplier_cannot_bypass_cluster_check(workflow):
    _, service, lot, _ = workflow
    history(service, None)
    assert service.lot_price_benchmark(lot['id'])['matched_items'] == 0


def test_invalid_legacy_vat_value_is_not_coerced_to_true(workflow):
    database, service, lot, supplier = workflow
    row = history(service, supplier)
    with database.connection() as connection:
        connection.execute('UPDATE purchase_history SET vat_included=2 WHERE id=?', (row['id'],))
    assert service.list_purchase_history()[0]['vat_included'] is None
    assert service.lot_price_benchmark(lot['id'])['matched_items'] == 0


def test_mixed_vat_history_has_no_aggregate_but_each_quote_uses_its_basis(workflow):
    _, service, lot, supplier = workflow
    gross = history(service, supplier, price=120, vat=True, invoice='SYNTHETIC-GROSS')
    net = history(service, supplier, price=100, vat=False, invoice='SYNTHETIC-NET')
    ambiguous = service.lot_price_benchmark(lot['id'])['items'][0]
    assert ambiguous['basis_status'] == 'ambiguous_vat'
    assert ambiguous['median_unit_price'] is None and ambiguous['history_count'] == 0
    for vat, price, source in [(True, 120, gross), (False, 100, net)]:
        benchmark = service.lot_price_benchmark(lot['id'], vat_included=vat)['items'][0]
        assert benchmark['median_unit_price'] == price and benchmark['source_purchase_ids'] == [source['id']]
        service.add_quote(lot['id'], QuoteCreate(supplier_id=supplier['id'], currency='RUB', vat_included=vat,
            items=[QuoteItemCreate(lot_item_id=lot['items'][0]['id'], unit_price=price)]))
    comparison = service.comparison(lot['id'])
    assert all(row['history_variance_pct'] == 0 for row in comparison['quotes'])
    assert all(row['history_coverage'] == '1/1' for row in comparison['quotes'])


def test_batch_source_type_and_path_describe_actual_original_bytes(workflow):
    database, service, _, _ = workflow
    content = xlsx()
    batch = service.create_import_batch([('../../SYNTHETIC-invoice.xlsx', content)])
    assert len(batch['price_history_entries']) == 1
    source = database.one('SELECT * FROM source_documents')
    assert source['document_type'] == 'invoice' and source['document_type'] != 'paid_invoice'
    assert source['extraction_status'] == 'extracted_needs_review'
    path = Path(source['storage_path'])
    assert path.is_relative_to(Path(database.path).parent) and path.is_file()
    assert source['filename'] == 'SYNTHETIC-invoice.xlsx'
    assert path.read_bytes() == content
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256']
    service.create_import_batch([('renamed.xlsx', content)])
    assert database.one('SELECT count(*) AS n FROM source_documents')['n'] == 1


def test_supplier_identity_confirmation_does_not_approve_prices_or_payment(workflow):
    database, service, _, _ = workflow
    batch = service.create_import_batch([('SYNTHETIC-invoice.xlsx', xlsx())])
    entry = batch['price_history_entries'][0]
    with database.connection() as connection:
        draft_id = connection.execute("""INSERT INTO supplier_drafts(name,region,dedup_key,status,created_at)
            VALUES ('Synthetic imported supplier','Воронеж','synthetic-import','needs_review','2026-09-04')""").lastrowid
        connection.execute('UPDATE price_history_entries SET supplier_draft_id=? WHERE id=?', (draft_id, entry['id']))
    supplier = service.confirm_supplier_draft(draft_id,
        SupplierDraftConfirm(confirmed_by='Identity reviewer', region='Воронеж', cluster='cluster_2'))
    stored = database.one('SELECT * FROM price_history_entries WHERE id=?', (entry['id'],))
    assert stored['supplier_id'] == supplier['id'] and stored['status'] == 'draft'
    assert stored['confirmed_by'] == '' and stored['confirmed_at'] is None
    assert database.one('SELECT count(*) AS n FROM purchase_history')['n'] == 0
    service.confirm_batch_entries(batch['id'], [entry['id']], 'Price reviewer')
    assert database.one('SELECT status FROM price_history_entries WHERE id=?', (entry['id'],))['status'] == 'confirmed'
    assert database.one('SELECT count(*) AS n FROM purchase_history')['n'] == 0
    # Source is only an extracted invoice, not an independently approved paid invoice.
    with pytest.raises(ValueError, match='paid invoice'):
        service.add_purchase_history(PurchaseHistoryCreate(**{**money_model_data(PurchaseHistoryCreate),
            'source_document_id': entry['source_document_id']}))


def test_ui_requires_explicit_currency_and_vat_and_does_not_offer_xls():
    html = (Path(__file__).parents[1] / 'procurement/static/index.html').read_text(encoding='utf-8')
    for prefix in ('q', 'h'):
        assert f'id="{prefix}Currency"><option value="" selected disabled>' in html
        assert f'id="{prefix}Vat"><option value="" selected disabled>' in html
        assert f"...explicitMoneyBasis('{prefix}')" in html
    assert "currency:'RUB',vat_included:true" not in html
    assert 'accept=".pdf,.xlsx,.xls"' not in html
    assert "!['true','false'].includes(vat)" in html


def test_lot_ui_requires_currency_instead_of_hardcoding_rub():
    html = (Path(__file__).parents[1] / 'procurement/static/index.html').read_text(encoding='utf-8')
    assert 'id="lCurrency"><option value="" selected disabled>' in html
    assert 'currency:explicitLotCurrency(),items:' in html
    assert "currency:'RUB',items:" not in html
    assert "function explicitLotCurrency()" in html
