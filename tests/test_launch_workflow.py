import concurrent.futures
import hashlib
from pathlib import Path

import pytest

from procurement.db import Database
from procurement.identity import authenticated_actor
from procurement.launch_workflow import LaunchWorkflow
from procurement.models import SupplierCreate, ProjectCreate, LotCreate, CampaignCreate
from procurement.service import ProcurementService, ConflictError
from procurement.table_ingest import read_table, safe_upload, contacts, valid_inn, MAX_FILE

FIXTURES=Path(__file__).parent/'fixtures'


@pytest.fixture
def workflow(tmp_path):
    db=Database(str(tmp_path/'procurement.db'));db.initialize()
    service=ProcurementService(db)
    token=authenticated_actor.set('staff-a')
    yield db,service,LaunchWorkflow(service)
    authenticated_actor.reset(token)


def project(service):
    return service.create_project(ProjectCreate(name='ТЕСТ Воронеж',region='Воронежская область',delivery_address='Воронеж, тестовая 1'))


def lot_payload(pid, attachments=()):
    return dict(project_id=pid,title='ТЕСТ заявка',region='Воронежская область',delivery_address='Воронеж, тестовая 1',
        response_deadline='2026-10-15',currency='RUB',attachment_document_ids=list(attachments),
        items=[dict(name='Кабель 001230040500',quantity='10',unit='м',specification='Исполнение 0',delivery_date='2026-10-20')])


@pytest.mark.parametrize('filename',['suppliers.xlsx','suppliers.csv'])
def test_supplier_import_real_formats_preview_apply_repeat_rollback(workflow,filename):
    db,service,w=workflow
    table=read_table((FIXTURES/filename).read_bytes(),filename)
    preview=w.supplier_preview(table)
    assert preview['report']==dict(added=2,updated=0,skipped=1,error=1)
    assert db.one('SELECT count(*) n FROM suppliers')['n']==0
    with pytest.raises(ValueError):w.apply_import(preview['preview_id'],False)
    result=w.apply_import(preview['preview_id'],True)
    assert w.apply_import(preview['preview_id'],True)==result
    assert len(service.list_suppliers())==2
    assert all(x['region']=='Воронежская область' for x in service.list_suppliers())
    assert w.rollback_import(preview['preview_id'],True)['changed']==2
    assert not service.list_suppliers()
    assert db.one('SELECT count(*) n FROM suppliers')['n']==2
    assert w.rollback_import(preview['preview_id'],True)['changed']==0
    assert {'supplier_import_applied','supplier_import_rolled_back'} <= {x['action'] for x in service.list_audit()}


def test_mapping_update_preserves_unmapped_fields_and_stale_rollback(workflow):
    db,service,w=workflow
    s=service.create_supplier(SupplierCreate(name='Прежнее имя',region='Воронеж',tax_id='7707083893',
        email='kept@example.test',phone='+74732000001',categories=['окна'],rating=4.5,verified=True,telegram='kept'))
    table=read_table('Фирма;Код\nНовое имя;7707083893\n'.encode(),'mapping.csv')
    assert w.supplier_preview(table)['needs_mapping']
    p=w.supplier_preview(table,{'name':0,'tax_id':1})
    assert p['report']['updated']==1
    w.apply_import(p['preview_id'],True)
    updated=w.supplier(s['id'])
    assert updated['name']=='Новое имя' and updated['email']=='kept@example.test'
    assert updated['categories']==['окна'] and updated['verified']==1 and updated['rating']==4.5
    assert w.rollback_import(p['preview_id'],True)['changed']==1
    assert w.supplier(s['id'])['name']=='Прежнее имя'
    p=w.supplier_preview(table,{'name':0,'tax_id':1});w.apply_import(p['preview_id'],True)
    current=w.supplier(s['id'])
    values={k:current[k] for k in SupplierCreate.model_fields};values['name']='Правка после импорта'
    w.edit_supplier(s['id'],values,current['revision'])
    with pytest.raises(ConflictError):w.rollback_import(p['preview_id'],True)
    assert w.supplier(s['id'])['name']=='Правка после импорта'


def test_supplier_all_fields_soft_delete_restore_optimistic_lock(workflow):
    db,service,w=workflow
    s=service.create_supplier(SupplierCreate(name='Прежнее имя',region='Воронеж'))
    before=w.supplier(s['id'])
    values=dict(name='ТЕСТ все поля',tax_id='7707083893',region='Воронежская область',email='edited@example.test',
        phone='+7 (473) 200-00-01',telegram='@test',max_contact='test-max',cluster='cluster_2',categories=['окна','двери'],rating=4.5,verified=True)
    after=w.edit_supplier(s['id'],values,before['revision'])
    assert all(after[k]==v for k,v in values.items() if k!='phone')
    assert after['phone']=='+74732000001'
    with pytest.raises(ConflictError):w.edit_supplier(s['id'],values,before['revision'])
    with pytest.raises(ValueError):w.supplier_state(s['id'],False,after['revision'],False)
    deleted=w.supplier_state(s['id'],False,after['revision'],True)
    assert deleted['active']==0 and not service.list_suppliers()
    restored=w.supplier_state(s['id'],True,deleted['revision'],True)
    assert restored['active']==1 and len(service.list_suppliers())==1
    assert all(x['actor']=='staff-a' for x in service.list_audit())


def test_previews_cross_user_denied_atomic_concurrent_apply(workflow):
    db,service,w=workflow
    p=w.supplier_preview(read_table((FIXTURES/'suppliers.csv').read_bytes(),'suppliers.csv'))
    token=authenticated_actor.set('staff-b')
    try:
        with pytest.raises(Exception,match='другому пользователю'):w.apply_import(p['preview_id'],True)
    finally:authenticated_actor.reset(token)
    def apply(_):
        token=authenticated_actor.set('staff-a')
        try:return w.apply_import(p['preview_id'],True)
        finally:authenticated_actor.reset(token)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(apply,range(8)))
    assert all(r==results[0] for r in results) and len(service.list_suppliers())==2


@pytest.mark.parametrize('status',['draft','confirmed'])
def test_import_rollback_blocks_financial_history_reference_atomically(workflow,status):
    db,service,w=workflow
    p=w.supplier_preview(read_table((FIXTURES/'suppliers.csv').read_bytes(),'suppliers.csv'))
    w.apply_import(p['preview_id'],True)
    sid=service.list_suppliers()[0]['id']
    with db.connection() as conn:
        conn.execute('INSERT INTO price_history_entries(supplier_id,item_name,normalized_name,unit_price,status,created_at) VALUES (?,?,?,?,?,?)',
            (sid,'ТЕСТ материал','тест материал','100',status,'2026-09-26'))
    with pytest.raises(ConflictError,match='уже используется'):w.rollback_import(p['preview_id'],True)
    assert len(service.list_suppliers())==2
    assert db.one('SELECT status FROM launch_previews WHERE id=?',(p['preview_id'],))['status']=='applied'


@pytest.mark.parametrize('use',['campaign','quote','price','document'])
def test_updated_supplier_import_cannot_rollback_after_use(workflow,use):
    from procurement.models import QuoteCreate,QuoteItemCreate
    db,service,w=workflow
    supplier=service.create_supplier(SupplierCreate(name='ТЕСТ прежний',region='Москва',cluster='cluster_1',
        tax_id='7707083893',email='fixture@example.test'))
    table=read_table('Фирма;ИНН;Регион;Кластер\nТЕСТ обновлён;7707083893;Воронежская область;cluster_2\n'.encode(),'updated.csv')
    p=w.supplier_preview(table,{'name':0,'tax_id':1,'region':2,'cluster':3});w.apply_import(p['preview_id'],True)
    assert w.supplier(supplier['id'])['cluster']=='cluster_2'
    pr=project(service);lot=service.create_lot(LotCreate(**lot_payload(pr['id'])))
    if use=='campaign':service.create_campaign(lot['id'],CampaignCreate(supplier_ids=[supplier['id']]))
    elif use=='quote':service.add_quote(lot['id'],QuoteCreate(supplier_id=supplier['id'],currency='RUB',vat_included=True,
        items=[QuoteItemCreate(lot_item_id=lot['items'][0]['id'],unit_price=100)]))
    elif use=='price':
        with db.connection() as conn:conn.execute(
            "INSERT INTO price_history_entries(supplier_id,item_name,normalized_name,unit_price,created_at) VALUES (?,?,?,?,?)",
            (supplier['id'],'ТЕСТ','тест','100','2026-09-27'))
    else:service.register_source_document(filename='items.xlsx',content=(FIXTURES/'items.xlsx').read_bytes(),
        document_type='invoice',supplier_id=supplier['id'])
    before={t:db.all('SELECT * FROM '+t) for t in ('suppliers','launch_previews','audit_log')}
    with pytest.raises(ConflictError,match='уже используется'):w.rollback_import(p['preview_id'],True)
    assert all(db.all('SELECT * FROM '+t)==rows for t,rows in before.items())
    if use=='campaign':
        with db.connection() as conn:assert service._outbox_context(conn,service.list_outbox()[0]['id'])
    if use=='quote':assert service.comparison(lot['id'])['quotes']


@pytest.mark.parametrize('change',['edit','import'])
def test_used_supplier_cluster_cannot_be_changed(workflow,change):
    db,service,w=workflow
    supplier=service.create_supplier(SupplierCreate(name='ТЕСТ прежний',region='Москва',cluster='cluster_1',
        tax_id='7707083893',email='fixture@example.test'))
    pr=service.create_project(ProjectCreate(name='ТЕСТ Москва',region='Москва',cluster='cluster_1',delivery_address='ТЕСТ'))
    payload={**lot_payload(pr['id']),'region':'Москва','cluster':'cluster_1'}
    lot=service.create_lot(LotCreate(**payload))
    service.create_campaign(lot['id'],CampaignCreate(supplier_ids=[supplier['id']]))
    current=w.supplier(supplier['id'])
    if change=='import':
        table=read_table('Фирма;ИНН;Регион;Кластер\nТЕСТ обновлён;7707083893;Воронежская область;cluster_2\n'.encode(),'updated.csv')
        p=w.supplier_preview(table,{'name':0,'tax_id':1,'region':2,'cluster':3})
    before=db.all('SELECT * FROM suppliers');audits=db.all('SELECT * FROM audit_log')
    with pytest.raises(ConflictError,match='смена кластера'):
        if change=='import':w.apply_import(p['preview_id'],True)
        else:
            values={k:current[k] for k in SupplierCreate.model_fields}
            w.edit_supplier(supplier['id'],{**values,'region':'Воронежская область','cluster':'cluster_2'},current['revision'])
    assert db.all('SELECT * FROM suppliers')==before and db.all('SELECT * FROM audit_log')==audits
    with db.connection() as conn:assert service._outbox_context(conn,service.list_outbox()[0]['id'])


def test_sheet_review_corrections_source_attachments_campaign_snapshot(workflow):
    db,service,w=workflow
    pr=project(service); content=(FIXTURES/'items.xlsx').read_bytes()
    doc=service.register_source_document(filename='items.xlsx',content=content,document_type='project_section',project_id=pr['id'])
    p=w.sheet_preview(read_table(content,'items.xlsx'),source_document=doc)
    assert len(p['rows'])==3 and len(p['errors'])==1
    assert p['rows'][0]['quantity']=='10' and p['rows'][0]['delivery_date']=='2026-10-15'
    assert p['rows'][1]['quantity']=='2.5'
    data=lot_payload(pr['id']); data['items'][0]['specification']='Исправлено 0'
    lot=w.create_sheet_lot(p['preview_id'],data,True)
    assert lot['items'][0]['specification']=='Исправлено 0'
    assert lot['attachments'][0]['document_id']==doc['id']
    assert w.create_sheet_lot(p['preview_id'],data,True)['id']==lot['id']
    with pytest.raises(ConflictError):w.create_sheet_lot(p['preview_id'],{**data,'title':'Другая заявка'},True)
    s=service.create_supplier(SupplierCreate(name='ТЕСТ адресат',region='Воронеж',email='supplier@example.test'))
    campaign=service.create_campaign(lot['id'],CampaignCreate(supplier_ids=[s['id']]))
    m=campaign['messages'][0]
    assert len(m['attachments'])==1 and m['attachments'][0]['sha256']==hashlib.sha256(content).hexdigest()
    assert '2026-10-20' in m['body'] and '001230040500' in m['body'] and 'Исправлено 0' in m['body']
    with pytest.raises(ConflictError):w.attach_lot(lot['id'],[])
    assert w.document_file(doc).path.read_bytes()==content
    Path(doc['storage_path']).write_bytes(b'corrupt')
    with pytest.raises(ConflictError):w.document_file(doc)


def test_cross_project_attachment_is_rejected_before_lot_creation(workflow):
    db,service,w=workflow
    a=project(service);b=project(service)
    doc=service.register_source_document(filename='items.xlsx',content=(FIXTURES/'items.xlsx').read_bytes(),document_type='project_section',project_id=a['id'])
    with pytest.raises(ValueError,match='проекту'):service.create_lot(LotCreate(**lot_payload(b['id'],[doc['id']])))
    assert not db.all('SELECT * FROM lots')


@pytest.mark.parametrize('name',['../suppliers.xlsx','C:\\suppliers.xlsx','suppliers.exe.xlsx','suppliers.html.csv','.csv','a.xlsm','a.zip'])
def test_unsafe_upload_names(name):
    with pytest.raises(ValueError):safe_upload((FIXTURES/'suppliers.xlsx').read_bytes(),name,{'.xlsx','.csv'})


@pytest.mark.parametrize('values',[{'tax_id':'123'},{'tax_id':'7707083894'},{'phone':'12'},{'phone':'abc'},{'email':'bad'}, {'email':'a@b\nBcc: evil@example.test'}])
def test_invalid_contacts(values):
    with pytest.raises(ValueError):contacts(values)


def test_file_bounds_formula_and_header_errors():
    with pytest.raises(ValueError):safe_upload(b'x'*(MAX_FILE+1),'a.csv',{'.csv'})
    with pytest.raises(ValueError):read_table(b'a,b\n1,2','a.csv',header_row=100)
    with pytest.raises(ValueError):read_table(b'not zip','a.xlsx')
    with pytest.raises(ValueError):read_table(b'only header','a.csv')
    assert valid_inn('7707083893')=='7707083893'
    assert valid_inn('500100732259')=='500100732259'
