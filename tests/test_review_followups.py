"""Codex review follow-ups on #18 and #19: archived drafts, one-time cluster assignment, unmapped price column."""
import csv
import io

import pytest

from procurement.catalog import ALIASES, Catalog
from procurement.models import CampaignCreate, LotCreate, SupplierCreate
from procurement.service import ConflictError
from procurement.table_ingest import read_table
from test_launch_workflow import lot_payload, project, workflow  # noqa: F401 — pytest fixture


def test_archived_purchase_drafts_leave_the_work_queue(workflow):
    db, service, _ = workflow
    pr = project(service)
    supplier = service.create_supplier(SupplierCreate(name='ТЕСТ кабель', region='Воронежская область',
                                                      email='kp@example.test', categories=['кабель']))
    kept = service.create_lot(LotCreate(**lot_payload(pr['id'])))
    archived = service.create_lot(LotCreate(**{**lot_payload(pr['id']), 'title': 'ТЕСТ архив'}))
    for lot in (kept, archived):
        service.create_campaign(lot['id'], CampaignCreate(supplier_ids=[supplier['id']]))
    assert service.dashboard()['draft_messages'] == 2
    service.set_lot_archived(archived['id'], True)
    assert {m['lot_id'] for m in service.list_outbox()} == {kept['id']}
    assert service.dashboard()['draft_messages'] == 1
    assert [m['lot_id'] for m in service.list_outbox(lot_id=archived['id'])] == [archived['id']]  # history kept
    service.set_lot_archived(archived['id'], False)
    assert len(service.list_outbox()) == 2


def test_used_supplier_gets_its_missing_cluster_once(workflow):
    db, service, w = workflow
    supplier = service.create_supplier(SupplierCreate(name='ТЕСТ без кластера', region='Москва', email='a@example.test'))
    assert supplier['cluster'] == ''
    with db.connection() as conn:  # referenced by a confirmed purchase: «used»
        conn.execute("INSERT INTO price_history_entries(supplier_id,item_name,normalized_name,unit_price,created_at) "
                     "VALUES (?,?,?,?,?)", (supplier['id'], 'ТЕСТ', 'тест', '100', '2026-09-27'))
    current = w.supplier(supplier['id'])
    values = {k: current[k] for k in SupplierCreate.model_fields}
    assigned = w.edit_supplier(supplier['id'], {**values, 'cluster': 'cluster_1'}, current['revision'])
    assert assigned['cluster'] == 'cluster_1'
    current = w.supplier(supplier['id'])
    values = {k: current[k] for k in SupplierCreate.model_fields}
    with pytest.raises(ConflictError, match='смена кластера'):
        w.edit_supplier(supplier['id'], {**values, 'cluster': 'cluster_2'}, current['revision'])


def _price_csv():
    row = dict(item_name='ТЕСТ кабель', unit='м', unit_price='100', currency='RUB', vat='с НДС', region='Воронежская область',
               supplier_name='ТЕСТ поставщик', email='p@example.test', price_date='2026-09-01', valid_until='2050-01-01')
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(ALIASES), delimiter=';')
    writer.writeheader()
    writer.writerow({k: row.get(k, '') for k in ALIASES})
    return out.getvalue().encode()


def test_unmapped_price_column_asks_to_map_it(workflow):
    db, service, w = workflow
    raw = _price_csv()
    doc = service.register_source_document(filename='price.csv', content=raw, document_type='price_list')
    table = read_table(raw, 'price.csv')
    catalog = Catalog(service, w)
    mapping = {k: i for i, k in enumerate(ALIASES) if k != 'unit_price'}
    errors = catalog.price_preview(doc, table, mapping=mapping)['errors']
    assert errors and 'Колонка цены не выбрана' in str(errors[0])
    blank = read_table(raw.replace(b';100;', b';;'), 'price.csv')
    errors = catalog.price_preview(doc, blank)['errors']
    assert errors and 'не указана числом' in str(errors[0])
