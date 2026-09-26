"""Aggregate work budgets, atomic source claims and honest extraction failures."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from pathlib import Path

import pytest

from procurement.db import Database
from procurement.imports import extract_document
from procurement.service import ProcurementService
from tests.test_launch_import_review import SYNTHETIC_PRICELIST


@pytest.fixture
def service(tmp_path):
    db=Database(str(tmp_path/'batch.sqlite3'));db.initialize()
    return ProcurementService(db)


@pytest.mark.parametrize('count,accepted',[(10_000,True),(10_001,False)])
def test_aggregate_item_boundary_and_bounded_transactions(service,monkeypatch,count,accepted):
    result=extract_document(SYNTHETIC_PRICELIST,'synthetic.xlsx')
    result.items=result.items*count
    monkeypatch.setattr('procurement.imports.extract_document',lambda *args:result)
    opened=[];connection=service.db.connection
    @contextmanager
    def counted():
        opened.append(True)
        with connection() as conn:yield conn
    monkeypatch.setattr(service.db,'connection',counted)
    if not accepted:
        with pytest.raises(ValueError,match='aggregate limit'):
            service.create_import_batch([('synthetic.xlsx',SYNTHETIC_PRICELIST)])
        assert not opened
        assert not (Path(service.db.path).parent/'uploads').exists()
    else:
        batch=service.create_import_batch([('synthetic.xlsx',SYNTHETIC_PRICELIST)])
        assert len(batch['price_history_entries'])==10_000 and batch['status']=='needs_review'
        assert len(opened)<20  # no per-item transactions


def test_aggregate_budget_across_files_has_no_partial_write(service,monkeypatch):
    result=extract_document(SYNTHETIC_PRICELIST,'synthetic.xlsx');result.items*=6000
    monkeypatch.setattr('procurement.imports.extract_document',lambda *args:result)
    with pytest.raises(ValueError,match='aggregate limit'):
        service.create_import_batch([('one.xlsx',SYNTHETIC_PRICELIST),('two.xlsx',SYNTHETIC_PRICELIST)])
    for table in ('source_documents','import_batches','supplier_drafts','price_history_entries','audit_log'):
        assert service.db.one('SELECT count(*) n FROM '+table)['n']==0


@pytest.mark.parametrize('pre_registered',[False,True])
def test_parallel_same_source_claims_one_full_set(service,monkeypatch,pre_registered):
    content=SYNTHETIC_PRICELIST
    if pre_registered:
        service.register_source_document(filename='synthetic.xlsx',content=content,document_type='price_list')
    barrier=Barrier(8)
    def extract(*args):
        result=extract_document(*args);barrier.wait(timeout=20);return result
    monkeypatch.setattr('procurement.imports.extract_document',extract)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda n:service.create_import_batch([(f'{n}.xlsx',content)]),range(8)))
    assert sum(len(b['price_history_entries']) for b in results)==1
    assert sum(b['status']=='needs_review' for b in results)==1
    assert all(not b['errors'] for b in results)
    for table in ('source_documents','supplier_drafts','price_history_entries'):
        assert service.db.one('SELECT count(*) n FROM '+table)['n']==1
    source=service.db.one('SELECT * FROM source_documents')
    assert hashlib.sha256(Path(source['storage_path']).read_bytes()).hexdigest()==hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize('filename,content',[('broken.xlsx',b'PK broken'),('broken.pdf',b'%PDF-broken'),('unknown.bin',b'unsupported')])
def test_wholly_failed_extraction_is_terminal_and_errors_persist(service,filename,content):
    batch=service.create_import_batch([(filename,content)])
    assert batch['status']=='failed' and batch['errors']
    assert not batch['supplier_drafts'] and not batch['price_history_entries']
    assert service.get_import_batch(batch['id'])['errors']==batch['errors']
    assert service.list_import_batches()[0]['errors']==batch['errors']
    assert service.db.one('SELECT count(*) n FROM source_documents')['n']==0


def test_mixed_extraction_exposes_errors_and_only_reviewable_rows(service):
    batch=service.create_import_batch([('synthetic.xlsx',SYNTHETIC_PRICELIST),('broken.xlsx',b'PK broken')])
    assert batch['status']=='needs_review' and batch['errors']
    assert len(batch['price_history_entries'])==1 and len(batch['supplier_drafts'])==1
    assert service.db.one('SELECT count(*) n FROM source_documents')['n']==1


def test_item_insert_failure_rolls_back_entire_source_claim(service):
    with service.db.connection() as conn:
        conn.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON price_history_entries BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    batch=service.create_import_batch([('synthetic.xlsx',SYNTHETIC_PRICELIST)])
    assert batch['status']=='failed' and batch['errors']
    assert not batch['supplier_drafts'] and not batch['price_history_entries']
    assert service.db.one('SELECT count(*) n FROM supplier_drafts')['n']==0
    assert service.db.one('SELECT count(*) n FROM price_history_entries')['n']==0
