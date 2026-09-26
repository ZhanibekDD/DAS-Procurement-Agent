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


@pytest.mark.parametrize('basis,currency,vat',[
    ('RUB без НДС','RUB',False),('USD с НДС','USD',True),
    ('EUR включая НДС','EUR',True),('KZT НДС включён','KZT',True),
    ('руб. без НДС','RUB',False),('₽ с НДС','RUB',True),
    ('без НДС',None,None),('RUB',None,None),('',None,None),
    ('RUB USD без НДС',None,None),('EUR KZT с НДС',None,None),
    ('RUB без НДС и с НДС',None,None),
    ('USD без НДС; НДС включен',None,None),
])
def test_pdf_requires_unique_explicit_currency_and_vat(service,monkeypatch,basis,currency,vat):
    from procurement.imports import extract_from_pdf
    text='ООО «ТЕСТ поставщик»\n'+basis+'\nТЕСТ кабель 1 10.25'
    monkeypatch.setattr('procurement.imports._extract_pdf_text',lambda *args:([text],[]))
    table_calls=[]
    def tables(*args):table_calls.append(True);return [],False
    monkeypatch.setattr('procurement.imports._extract_items_from_pdf_tables',tables)
    result=extract_from_pdf(b'%PDF-synthetic','synthetic.pdf')
    if currency is not None:
        assert not result.errors and table_calls
        assert len(result.items)==1 and result.currency==currency
        assert result.items[0].currency==currency and result.items[0].vat_included is vat
    else:
        assert result.errors and not result.items and not table_calls
        monkeypatch.setattr('procurement.imports.extract_document',lambda *args:result)
        batch=service.create_import_batch([('synthetic.pdf',b'%PDF-synthetic')])
        assert batch['status']=='failed' and batch['errors']
        assert not batch['supplier_drafts'] and not batch['price_history_entries']
        assert not service.list_source_documents()
        assert service.get_import_batch(batch['id'])['errors']==batch['errors']


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


@pytest.mark.parametrize('heading',[
    '', 'RUB', 'без НДС', 'USD EUR без НДС', 'RUB с НДС и без НДС',
])
def test_xlsx_missing_or_conflicting_financial_basis_has_no_rows(service,heading):
    from test_import_money_safety import workbook_bytes
    content=workbook_bytes([('Prices',[(heading,),('Наименование','Цена'),('ТЕСТ кабель','100')])])
    result=extract_document(content,'synthetic.xlsx')
    assert result.errors and not result.items and result.currency==''
    batch=service.create_import_batch([('synthetic.xlsx',content)])
    assert batch['status']=='failed' and batch['errors']
    assert not batch['supplier_drafts'] and not batch['price_history_entries']
    assert not service.list_source_documents()


def test_xlsx_financial_basis_is_not_inherited_from_another_sheet():
    from test_import_money_safety import workbook_bytes
    content=workbook_bytes([
        ('Valid',[('USD без НДС',),('Наименование','Цена'),('ТЕСТ A','100')]),
        ('Missing',[('без НДС',),('Наименование','Цена'),('ТЕСТ B','200')]),
    ])
    result=extract_document(content,'synthetic.xlsx')
    assert result.errors and len(result.items)==1
    assert result.items[0].item_name=='ТЕСТ A' and result.items[0].currency=='USD'


def test_reject_selected_price_rows_completes_batch_without_confirming_them(service):
    from test_review_ui_backend import synthetic_workbook
    batch=service.create_import_batch([('synthetic.xlsx',synthetic_workbook())])
    good,bad=[row['id'] for row in batch['price_history_entries']]
    assert service.confirm_batch_entries(batch['id'],[good],'ТЕСТ reviewer')=={'confirmed':1}
    assert service.get_import_batch(batch['id'])['status']=='needs_review'
    assert service.reject_batch_entries(batch['id'],[bad],'ТЕСТ rejecting')=={'rejected':1}
    assert service.get_import_batch(batch['id'])['status']=='done'
    assert [r['id'] for r in service.list_price_history_entries(status='confirmed')]==[good]
    before={t:service.db.all('SELECT * FROM '+t) for t in ('price_history_entries','import_batches','audit_log')}
    assert service.reject_batch_entries(batch['id'],[bad,bad],'ТЕСТ stale')=={'rejected':0}
    with pytest.raises(ConflictError):service.confirm_batch_entries(batch['id'],[bad],'ТЕСТ stale')
    with pytest.raises(ConflictError):service.reject_batch_entries(batch['id'],[good],'ТЕСТ stale')
    assert all(service.db.all('SELECT * FROM '+t)==rows for t,rows in before.items())
    audit=service.db.one("SELECT * FROM audit_log WHERE action='entries_rejected'")
    assert audit['actor']=='ТЕСТ rejecting'


def test_reject_foreign_price_row_is_atomic(service):
    from test_review_ui_backend import synthetic_workbook
    own=service.create_import_batch([('own.xlsx',synthetic_workbook())])
    other=service.create_import_batch([('other.xlsx',synthetic_workbook(' another'))])
    ids=[own['price_history_entries'][0]['id'],other['price_history_entries'][0]['id']]
    before={t:service.db.all('SELECT * FROM '+t) for t in ('price_history_entries','import_batches','audit_log')}
    with pytest.raises(Exception,match='not in batch'):service.reject_batch_entries(own['id'],ids,'ТЕСТ')
    assert all(service.db.all('SELECT * FROM '+t)==rows for t,rows in before.items())


def test_parallel_confirm_or_reject_price_rows_has_one_winner(service):
    from concurrent.futures import ThreadPoolExecutor
    batch=service.create_import_batch([('synthetic.xlsx',SYNTHETIC_PRICELIST)])
    ids=[batch['price_history_entries'][0]['id']]
    def review(n):
        try:
            fn=service.confirm_batch_entries if n%2 else service.reject_batch_entries
            result=fn(batch['id'],ids,'ТЕСТ')
            return sum(result.values())
        except ConflictError:return 0
    with ThreadPoolExecutor(max_workers=8) as pool:assert sum(pool.map(review,range(32)))==1
    assert service.get_import_batch(batch['id'])['status']=='done'
    assert service.db.one("SELECT count(*) n FROM audit_log WHERE action IN ('entries_confirmed','entries_rejected')")['n']==1


@pytest.mark.parametrize('ttl',[-1,0,299,300,86400,86401])
def test_sso_session_ttl_is_validated_before_startup(monkeypatch,ttl):
    import os
    from procurement.config import Settings
    from test_sso_adapter import DAS,BASE
    for key in list(os.environ):
        if key.startswith(('PROCUREMENT_','DAS_SSO_')):monkeypatch.delenv(key)
    for key,value in {
        'DAS_SSO_AUTHORIZE_URL':DAS+'/access/sso/authorize/',
        'DAS_SSO_INTERNAL_BASE_URL':'http://das-identity.test:8000',
        'DAS_SSO_CLIENT_ID':'procurement','DAS_SSO_CLIENT_SECRET':'test-service-'+'x'*32,
        'DAS_SSO_REDIRECT_URI':BASE+'/auth/sso/callback',
        'PROCUREMENT_AUTH_SECRET':'test-state-'+'s'*32,'PROCUREMENT_ENV':'production',
        'PROCUREMENT_SESSION_TTL_SECONDS':str(ttl),
    }.items():monkeypatch.setenv(key,value)
    if ttl in (300,86400):assert Settings.from_env().session_ttl_seconds==ttl
    else:
        with pytest.raises(RuntimeError,match='between 300 and 86400'):Settings.from_env()


@pytest.mark.parametrize('kind,status',[
    ('paid_invoice','approved'),('project_section','approved'),
    ('project_section','needs_review'),('invoice','pending_ai_extraction'),
])
def test_price_import_preserves_reused_document_workflow(service,kind,status):
    document=service.register_source_document(filename='synthetic.xlsx',content=SYNTHETIC_PRICELIST,document_type=kind)
    with service.db.connection() as conn:conn.execute('UPDATE source_documents SET extraction_status=? WHERE id=?',(status,document['id']))
    before=service.db.one('SELECT * FROM source_documents WHERE id=?',(document['id'],))
    original=Path(before['storage_path']).read_bytes()
    batch=service.create_import_batch([('renamed.xlsx',SYNTHETIC_PRICELIST)])
    assert batch['status']=='needs_review' and len(batch['price_history_entries'])==1
    assert service.db.one('SELECT * FROM source_documents WHERE id=?',(document['id'],))==before
    assert Path(before['storage_path']).read_bytes()==original
    service.confirm_batch_entries(batch['id'],[batch['price_history_entries'][0]['id']],'ТЕСТ')
    assert service.db.one('SELECT * FROM source_documents WHERE id=?',(document['id'],))==before


def test_source_created_concurrently_is_not_reclassified_by_price_import(service,monkeypatch):
    document=service.register_source_document(filename='synthetic.xlsx',content=SYNTHETIC_PRICELIST,document_type='paid_invoice')
    with service.db.connection() as conn:conn.execute("UPDATE source_documents SET extraction_status='approved' WHERE id=?",(document['id'],))
    before=service.db.one('SELECT * FROM source_documents')
    one=service.db.one
    def lookup(sql,*args):
        # Simulate another registration winning between the optimistic lookup
        # and the locked register_source_document lookup.
        if sql=='SELECT id FROM source_documents WHERE sha256=?':return None
        return one(sql,*args)
    monkeypatch.setattr(service.db,'one',lookup)
    batch=service.create_import_batch([('renamed.xlsx',SYNTHETIC_PRICELIST)])
    assert batch['status']=='needs_review' and len(batch['price_history_entries'])==1
    assert one('SELECT * FROM source_documents')==before


def rejected_batch(service):
    from procurement.models import SupplierDraftReject
    batch=service.create_import_batch([('old.xlsx',SYNTHETIC_PRICELIST)])
    draft=batch['supplier_drafts'][0]
    service.reject_supplier_draft(draft['id'],SupplierDraftReject(rejected_by='ТЕСТ',review_notes='Недостоверные старые данные'))
    return batch,service.db.one('SELECT * FROM supplier_drafts WHERE id=?',(draft['id'],))


def test_corrected_source_gets_fresh_review_without_reusing_rejected_draft(service):
    from test_launch_import_review import workbook,confirmation
    old,rejected=rejected_batch(service)
    old_rows=service.db.all('SELECT * FROM price_history_entries')
    new=service.create_import_batch([('corrected.xlsx',workbook('ТЕСТ исправлено'))])
    fresh=new['supplier_drafts'][0]
    assert fresh['id']!=rejected['id'] and fresh['status']=='needs_review'
    assert new['price_history_entries'][0]['supplier_draft_id']==fresh['id']
    after=service.db.one('SELECT * FROM supplier_drafts WHERE id=?',(rejected['id'],))
    assert all(after[k]==value for k,value in rejected.items() if k!='dedup_key')
    assert service.db.all('SELECT * FROM price_history_entries WHERE import_batch_id=?',(old['id'],))==old_rows
    supplier=service.confirm_supplier_draft(fresh['id'],confirmation())
    third=service.create_import_batch([('third.xlsx',workbook('ТЕСТ третий документ'))])
    assert not third['supplier_drafts'] and third['price_history_entries'][0]['supplier_id']==supplier['id']
    assert service.db.one("SELECT count(*) n FROM audit_log WHERE action='superseded' AND entity_type='supplier_draft'")['n']==1
    assert service.db.one('SELECT supplier_id FROM price_history_entries WHERE import_batch_id=?',(old['id'],))['supplier_id'] is None


def test_parallel_corrected_sources_create_one_new_supplier_review(service):
    from concurrent.futures import ThreadPoolExecutor
    from test_launch_import_review import workbook
    _,rejected=rejected_batch(service);content=workbook('ТЕСТ исправлено')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda n:service.create_import_batch([(f'corrected-{n}.xlsx',content)]),range(8)))
    assert sum(len(b['price_history_entries']) for b in results)==1
    assert sum(len(b['supplier_drafts']) for b in results)==1
    assert service.db.one("SELECT count(*) n FROM supplier_drafts WHERE status='needs_review'")['n']==1
    assert service.db.one('SELECT status FROM supplier_drafts WHERE id=?',(rejected['id'],))['status']=='rejected'
    assert service.db.one("SELECT count(*) n FROM audit_log WHERE action='superseded'")['n']==1


def test_failed_corrected_import_preserves_rejection_and_key_atomically(service):
    from test_launch_import_review import workbook
    _,rejected=rejected_batch(service)
    before=service.db.all('SELECT * FROM price_history_entries')
    with service.db.connection() as conn:
        conn.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON price_history_entries BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    batch=service.create_import_batch([('corrected.xlsx',workbook('ТЕСТ исправлено'))])
    assert batch['status']=='failed' and not batch['supplier_drafts']
    assert service.db.all('SELECT * FROM supplier_drafts')==[rejected]
    assert service.db.all('SELECT * FROM price_history_entries')==before
    assert not service.db.all("SELECT * FROM audit_log WHERE action='superseded'")


@pytest.mark.parametrize('ordering',['project_first','project_wins_registration'])
def test_atomic_price_source_reuse_preserves_project_ownership(service,monkeypatch,ordering):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    project=service.create_project(ProjectCreate(name='ТЕСТ проект',region='Воронеж',delivery_address='ТЕСТ'))
    register=service.register_source_document
    entered,released=Event(),Event()
    def registration(**kwargs):
        if kwargs.get('_price_import') and ordering=='project_wins_registration':
            entered.set();assert released.wait(10)
        return register(**kwargs)
    monkeypatch.setattr(service,'register_source_document',registration)
    def project_document():return register(filename='project.xlsx',content=SYNTHETIC_PRICELIST,
        document_type='project_section',project_id=project['id'])
    if ordering=='project_first':
        document=project_document()
        batch=service.create_import_batch([('prices.xlsx',SYNTHETIC_PRICELIST)])
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(service.create_import_batch,[('prices.xlsx',SYNTHETIC_PRICELIST)])
            assert entered.wait(10);document=project_document();released.set();batch=future.result(timeout=20)
    assert not batch['errors'] and batch['status']=='needs_review' and len(batch['price_history_entries'])==1
    assert batch['price_history_entries'][0]['source_document_id']==document['id']
    assert service.db.one('SELECT * FROM source_documents')==document
    # Ordinary document uploads still cannot reassign another project's source.
    with pytest.raises(ConflictError,match='different project'):
        register(filename='copy.xlsx',content=SYNTHETIC_PRICELIST,document_type='project_section')
