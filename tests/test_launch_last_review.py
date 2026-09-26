"""Conservative supplier matching, source provenance and finite PDF prices."""
from pathlib import Path

import pytest

from procurement.db import Database
from procurement.imports import extract_document
from procurement.models import SupplierCreate,SupplierDraftConfirm,LotCreate,ProjectCreate
from procurement.service import ProcurementService,ConflictError
from test_launch_import_review import SYNTHETIC_PRICELIST
from test_launch_workflow import lot_payload


@pytest.fixture
def service(tmp_path):
    db=Database(str(tmp_path/'review.sqlite3'));db.initialize();return ProcurementService(db)


@pytest.mark.parametrize('draft,suppliers,expected',[
    ({'name':'ТЕСТ','email':'shared@example.test'},[{'id':1,'name':'A','email':'shared@example.test'},{'id':2,'name':'B','email':'SHARED@example.test'}],'ambiguous'),
    ({'name':'ООО ТЕСТ'},[{'id':1,'name':'ООО «ТЕСТ»'},{'id':2,'name':'ооо тест'}],'ambiguous'),
    ({'name':'ТЕСТ','email':'one@example.test'},[{'id':1,'name':'A','email':'one@example.test'},{'id':2,'name':'A','email':'two@example.test'}],1),
    ({'name':'ООО ТЕСТ'},[{'id':1,'name':'ООО «ТЕСТ»'},{'id':2,'name':'Другой'}],1),
    ({'tax_id':'7707083893','email':'shared@example.test'},[{'id':1,'tax_id':'7707083893','email':'shared@example.test'},{'id':2,'tax_id':'7736050003','email':'shared@example.test'}],1),
    ({'tax_id':'7707083893','name':'ТЕСТ','email':'shared@example.test'},[{'id':2,'tax_id':'7736050003','name':'ТЕСТ','email':'shared@example.test'}],None),
])
def test_supplier_match_requires_unique_evidence(draft,suppliers,expected):
    if expected=='ambiguous':
        with pytest.raises(ConflictError,match='ambiguous'):ProcurementService._match_existing_supplier(draft,suppliers)
    else:
        result=ProcurementService._match_existing_supplier(draft,suppliers)
        assert (result['id'] if result else None)==expected


@pytest.mark.parametrize('by_email',[False,True])
def test_ambiguous_supplier_approval_is_atomic_and_list_is_honest(service,by_email):
    for n in range(2):
        service.create_supplier(SupplierCreate(name='ООО «ТЕСТ поставщик»' if not by_email else f'ТЕСТ {n}',
            region='Воронежская область',cluster='cluster_2',email='shared@example.test' if by_email else ''))
    batch=service.create_import_batch([('synthetic.xlsx',SYNTHETIC_PRICELIST)])
    draft=batch['supplier_drafts'][0]
    if by_email:
        with service.db.connection() as conn:conn.execute('UPDATE supplier_drafts SET email=? WHERE id=?',('shared@example.test',draft['id']))
    listed=service.list_supplier_drafts()[0]
    assert listed['match_error'] and listed['matched_supplier_id'] is None
    before={t:service.db.all('SELECT * FROM '+t) for t in ('suppliers','supplier_drafts','price_history_entries','audit_log')}
    with pytest.raises(ConflictError,match='ambiguous'):
        service.confirm_supplier_draft(draft['id'],SupplierDraftConfirm(confirmed_by='ТЕСТ',region='Воронежская область',cluster='cluster_2'))
    assert all(service.db.all('SELECT * FROM '+t)==v for t,v in before.items())


@pytest.mark.parametrize('with_supplier',[False,True])
def test_clean_zero_item_extraction_is_terminal_failure(service,monkeypatch,with_supplier):
    result=extract_document(SYNTHETIC_PRICELIST,'synthetic.xlsx');result.items=[];result.errors=[]
    if not with_supplier:result.supplier_name='';result.supplier_tax_id=''
    monkeypatch.setattr('procurement.imports.extract_document',lambda *args:result)
    batch=service.create_import_batch([('synthetic.xlsx',SYNTHETIC_PRICELIST)])
    assert batch['status']=='failed' and 'no price items' in batch['errors'][0]
    assert not batch['supplier_drafts'] and not batch['price_history_entries']
    assert not service.list_source_documents()


@pytest.mark.parametrize('price,expected',[
    ('-10',None),('0',None),('NaN',None),('inf',None),('1e3',None),('+10',None),
    ('10.25','10.25'),('1 234,50','1234.50'),('001.25','1.25'),
])
def test_structured_pdf_price_uses_positive_finite_decimal(monkeypatch,price,expected):
    import pdfplumber
    from procurement.imports import _extract_items_from_pdf_tables
    class Page:
        chars=[]
        def extract_tables(self):return [[['Наименование','Цена'],['ТЕСТ кабель',price]]]
        def close(self):pass
    class PDF:
        pages=[Page()]
        def __enter__(self):return self
        def __exit__(self,*args):pass
    monkeypatch.setattr(pdfplumber,'open',lambda *args:PDF())
    if expected is None:
        with pytest.raises(ValueError):_extract_items_from_pdf_tables(b'%PDF-synthetic','RUB',False)
    else:
        rows,found=_extract_items_from_pdf_tables(b'%PDF-synthetic','RUB',False)
        assert found and rows[0].unit_price==expected


def test_pdf_text_zero_price_does_not_become_confirmable():
    from procurement.imports import _extract_items_from_pdf_text
    with pytest.raises(ValueError):_extract_items_from_pdf_text(['ТЕСТ кабель 1 0.00'],'RUB',False)
    assert _extract_items_from_pdf_text(['ТЕСТ кабель 1 10.25'],'RUB',False)[0].unit_price=='10.25'


@pytest.mark.parametrize('source',['same','other','missing','corrupt'])
def test_lot_item_source_must_belong_to_project_and_match_bytes(service,source):
    project=service.create_project(ProjectCreate(name='ТЕСТ A',region='Воронеж',delivery_address='ТЕСТ'))
    other=service.create_project(ProjectCreate(name='ТЕСТ B',region='Воронеж',delivery_address='ТЕСТ'))
    document=service.register_source_document(filename='source.xlsx',content=SYNTHETIC_PRICELIST,
        document_type='project_section',project_id=other['id'] if source=='other' else project['id'])
    payload=lot_payload(project['id']);payload['items'][0]['source_document_id']=999999 if source=='missing' else document['id']
    if source=='corrupt':Path(document['storage_path']).write_bytes(b'synthetic corruption')
    before=service.db.all('SELECT * FROM audit_log')
    if source=='same':
        assert service.create_lot(LotCreate(**payload))['items'][0]['source_document_id']==document['id']
    else:
        with pytest.raises((ValueError,ConflictError)):service.create_lot(LotCreate(**payload))
        assert not service.list_lots() and not service.db.all('SELECT * FROM lot_items')
        assert service.db.all('SELECT * FROM audit_log')==before
