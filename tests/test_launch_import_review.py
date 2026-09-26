"""Bounded imports and atomic supplier-review regressions; synthetic data only."""
import hashlib
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from openpyxl import Workbook

from procurement.db import Database
from procurement.imports import extract_from_xlsx
from procurement.models import SupplierDraftConfirm
from procurement.service import ConflictError, ProcurementService


def workbook(item='ТЕСТ кабель'):
    book = Workbook()
    book.active.append(['ООО «ТЕСТ поставщик» RUB без НДС'])
    book.active.append(['Наименование', 'Цена', 'Ед.'])
    book.active.append([item, '100', 'м'])
    buffer = io.BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue()


def replace_part(content, name, payload):
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as target:
        for part in source.namelist():
            target.writestr(part, payload if part == name else source.read(part))
    return buffer.getvalue()


@pytest.mark.parametrize('payload', [
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:XFD1048576"/><sheetData/></worksheet>',
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1"/><sheetData><row r="10101"><c r="A10101"/></row></sheetData></worksheet>',
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1"/><sheetData><row r="1"><c r="CW1"/></row></sheetData></worksheet>',
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>' + 'X' * 8001 + '</t></is></c></row></sheetData></worksheet>',
])
def test_actual_or_declared_xlsx_bounds_fail_without_items(payload):
    content = replace_part(workbook(), 'xl/worksheets/sheet1.xml', payload)
    result = extract_from_xlsx(content, 'synthetic.xlsx')
    assert result.errors and not result.items


def test_archive_expansion_rejected_before_openpyxl(monkeypatch):
    content = replace_part(workbook(), 'xl/worksheets/sheet1.xml', b'X' * (21 * 1024 * 1024))
    assert len(content) < 25 * 1024 * 1024
    monkeypatch.setattr('openpyxl.load_workbook', lambda *a, **k: pytest.fail('unbounded workbook opened'))
    result = extract_from_xlsx(content, 'synthetic.xlsx')
    assert result.errors and not result.items


@pytest.fixture
def workflow(tmp_path):
    db = Database(str(tmp_path / 'bounded.sqlite3'))
    db.initialize()
    service = ProcurementService(db)
    batch = service.create_import_batch([('synthetic.xlsx', workbook())])
    assert len(batch['supplier_drafts']) == 1 and len(batch['price_history_entries']) == 1
    return db, service, batch


def confirmation():
    return SupplierDraftConfirm(confirmed_by='ТЕСТ reviewer', region='Воронеж', cluster='cluster_2')


def test_parallel_no_inn_draft_confirmation_claims_once(workflow):
    db, service, batch = workflow
    draft = batch['supplier_drafts'][0]
    assert not draft['tax_id']
    def approve(_):
        try:
            return service.confirm_supplier_draft(draft['id'], confirmation())['id']
        except ConflictError:
            return None
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(approve, range(32)))
    assert len([v for v in results if v is not None]) == 1
    supplier_id = next(v for v in results if v is not None)
    assert db.one('SELECT count(*) n FROM suppliers')['n'] == 1
    stored = db.one('SELECT * FROM supplier_drafts')
    assert stored['approved_supplier_id'] == supplier_id and stored['status'] == 'approved'
    assert db.one('SELECT supplier_id,status FROM price_history_entries') == {'supplier_id': supplier_id, 'status': 'draft'}
    assert db.one("SELECT count(*) n FROM audit_log WHERE action='approved' AND entity_type='supplier_draft'")['n'] == 1


def test_approval_failure_rolls_back_supplier_claim_and_audit(workflow, monkeypatch):
    db, service, batch = workflow
    before = db.all('SELECT * FROM audit_log')
    audit = db.audit
    def fail(action, *args, **kwargs):
        if action == 'approved':
            raise RuntimeError('synthetic failure')
        return audit(action, *args, **kwargs)
    monkeypatch.setattr(db, 'audit', fail)
    with pytest.raises(RuntimeError):
        service.confirm_supplier_draft(batch['supplier_drafts'][0]['id'], confirmation())
    assert db.one('SELECT count(*) n FROM suppliers')['n'] == 0
    assert db.one('SELECT status,approved_supplier_id FROM supplier_drafts') == {'status':'needs_review','approved_supplier_id':None}
    assert db.all('SELECT * FROM audit_log') == before


def test_later_document_retains_approved_supplier_but_requires_price_review(workflow):
    db, service, batch = workflow
    supplier = service.confirm_supplier_draft(batch['supplier_drafts'][0]['id'], confirmation())
    later = service.create_import_batch([('later.xlsx', workbook('ТЕСТ болт'))])
    assert later['status'] == 'needs_review' and not later['supplier_drafts']
    entry = later['price_history_entries'][0]
    assert entry['supplier_id'] == supplier['id'] and entry['status'] == 'draft'
    service.confirm_batch_entries(later['id'], [entry['id']], 'ТЕСТ price reviewer')
    assert db.one('SELECT supplier_id,status FROM price_history_entries WHERE id=?', (entry['id'],)) == {'supplier_id':supplier['id'],'status':'confirmed'}


@pytest.mark.parametrize('approved', [False, True])
def test_duplicate_source_is_done_not_empty_review_and_keeps_original_rows(workflow, approved):
    db, service, batch = workflow
    if approved:
        service.confirm_supplier_draft(batch['supplier_drafts'][0]['id'], confirmation())
        service.confirm_batch_entries(batch['id'], [batch['price_history_entries'][0]['id']], 'ТЕСТ reviewer')
    before = db.all('SELECT * FROM price_history_entries')
    repeated = service.create_import_batch([('renamed.xlsx', workbook())])
    assert repeated['status'] == 'done' and not repeated['price_history_entries'] and not repeated['supplier_drafts']
    assert db.all('SELECT * FROM price_history_entries') == before


def test_legacy_approved_link_backfills_from_authoritative_rows_and_audit(workflow):
    db, service, batch = workflow
    supplier = service.confirm_supplier_draft(batch['supplier_drafts'][0]['id'], confirmation())
    with db.connection() as conn:
        conn.execute('UPDATE supplier_drafts SET approved_supplier_id=NULL')
    db.initialize()
    assert db.one('SELECT approved_supplier_id FROM supplier_drafts')['approved_supplier_id'] == supplier['id']
    with db.connection() as conn:
        conn.execute('UPDATE supplier_drafts SET approved_supplier_id=NULL')
        conn.execute('UPDATE price_history_entries SET supplier_id=NULL')
    db.initialize()
    assert db.one('SELECT approved_supplier_id FROM supplier_drafts')['approved_supplier_id'] == supplier['id']
