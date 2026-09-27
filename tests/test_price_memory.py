import csv
import io
from pathlib import Path
from datetime import date
from decimal import Decimal
import pytest

from procurement.catalog import ALIASES, Catalog
from procurement.models import QuoteCreate
from procurement.price_memory import PriceMemory, material_key, unit_key
from procurement.table_ingest import read_table
from test_launch_workflow import workflow
from test_procurement_redesign import fbs
from test_launch_http import http_boundary
from test_sso_adapter import login, headers, BOB

TODAY=date(2026,9,27)


def price(workflow, name='ФБС 24.4.6', amount='100', supplier='A', pdate='2026-09-01', **extra):
    db,s,w=workflow
    row=dict(item_name=name,specification='ГОСТ 13579-2018',unit='шт.',unit_price=amount,currency='RUB',
        vat='с НДС',delivery='доставка включена',region='Воронежская область',minimum_batch='10',
        price_date=pdate,valid_until='2050-01-01',supplier_name='ТЕСТ '+supplier,email=supplier.lower()+'@example.test')
    row.update(extra)
    out=io.StringIO();writer=csv.DictWriter(out,fieldnames=list(ALIASES),delimiter=';');writer.writeheader();writer.writerow(row)
    raw=out.getvalue().encode();doc=s.register_source_document(filename='price.csv',content=raw,document_type='price_list')
    cat=Catalog(s,w);p=cat.price_preview(doc,read_table(raw,'price.csv'));assert not p['errors'],p
    cat.apply_prices(p['preview_id'],True)
    return doc


@pytest.mark.parametrize('mark',['24.4.6','12.4.6','9.4.6'])
def test_fbs_all_sources_history_no_pb_and_correct_dynamics(workflow,mark):
    db,s,w=workflow;name='ФБС '+mark
    old=price(workflow,name,'90','A');price(workflow,name,'110','B')
    price(workflow,name,'112','A','2026-09-20');price(workflow,name,'128','B','2026-09-20')
    price(workflow,'ПБ '+mark,'1','C')
    memory=PriceMemory(s).search(name,today=TODAY)
    assert memory['total']==4 and len(memory['groups'])==1
    assert all(r['item_name']==name and r['source_url'] for r in memory['records'])
    group=memory['groups'][0]
    assert group['current_stats']==dict(count=2,min='112',median='120',max='128')
    assert group['period_stats']['count']==4 and group['change_pct']=='20.00'
    assert {r['price_index_pct'] for r in memory['records'] if r['current']}=={'-6.67','6.67'}
    assert 'price_rise' in {a['kind'] for a in memory['alerts']}
    assert all(r['rating']==3 for r in s.list_suppliers())
    assert w.document_file(db.one('SELECT * FROM source_documents WHERE id=?',(old['id'],)))


@pytest.mark.parametrize('field,value',[('specification','B15'),('unit','м³'),('currency','USD'),
    ('vat','без НДС'),('region','Липецкая область'),('delivery','самовывоз'),('minimum_batch','20')])
def test_different_basis_never_averaged(workflow,field,value):
    _,s,_=workflow;price(workflow);price(workflow,amount='1000',supplier='B',**{field:value})
    result=PriceMemory(s).search('ФБС 24.4.6',today=TODAY)
    assert len(result['groups'])==2
    assert all(g['current_stats']['count']==1 for g in result['groups'])
    assert all(r['price_index_pct'] is None for r in result['records'])


@pytest.mark.parametrize('field',['vat','delivery','minimum_batch','valid_until'])
def test_unknown_conditions_visible_but_not_comparable(workflow,field):
    _,s,_=workflow;price(workflow,**{field:''});result=PriceMemory(s).search('ФБС',today=TODAY)
    assert result['total']==1 and not result['groups'] and not result['records'][0]['comparable']


@pytest.mark.parametrize('days',[30,90,365])
def test_period_and_validity_not_fake_zero_or_forward_fill(workflow,days):
    _,s,_=workflow;price(workflow,pdate='2025-01-01',valid_until='2026-09-15')
    result=PriceMemory(s).search('ФБС',days=days,today=TODAY)
    group=result['groups'][0]
    assert group['timeline'][-1]['date']=='2026-09-27' and group['timeline'][-1]['median'] is None
    assert group['current_stats']['count']==0
    assert result['records'][0]['freshness']=='expired'
    assert (TODAY-date.fromisoformat(result['from'])).days==days-1
    assert not result['alerts']


def test_future_and_same_day_numeric_sequence(workflow):
    db,s,w=workflow
    for n in range(1,12):price(workflow,amount=str(100+n),pdate='2026-09-01')
    price(workflow,amount='999',pdate='2027-01-01')
    result=PriceMemory(s).search('ФБС',today=TODAY)
    assert result['groups'][0]['current_stats']['median']=='111'
    assert sum(r['current'] for r in result['records'])==1
    assert result['total']==12


@pytest.mark.parametrize('name,expected',[('Блок ФБС 24-4-6','фбс 24.4.6'),('фбс 24 × 4 × 6','фбс 24.4.6'),
    ('ФБС 9.4.6-Т','фбс 9.4.6 -т'),('ПБ 24.4.6','пб 24.4.6')])
def test_material_normalization(name,expected):
    assert material_key(name)==expected


def test_unit_aliases_no_false_dimension_conversion():
    assert unit_key('штук')==unit_key('шт.')=='шт'
    assert unit_key('м²')==unit_key('м2')=='м²'
    assert unit_key('т')!=unit_key('кг')


def test_pb_with_fbs_in_description_never_in_fbs_search(workflow):
    _,s,_=workflow;price(workflow,'Плита ПБ 63.12 для ФБС 24.4.6')
    assert PriceMemory(s).search('ФБС 24.4.6',today=TODAY)['total']==0


@pytest.mark.parametrize('mark',['24.4.6','12.4.6','9.4.6'])
def test_quote_plus_catalog_source_history_and_project_filter(workflow,mark):
    db,s,w=workflow;lot,supplier=fbs(s)
    item=next(i for i in lot['items'] if i['name']=='ФБС '+mark)
    price(workflow,'ФБС '+mark,amount='100',supplier='A',minimum_batch='10.0')
    price(workflow,'Чужой кабель','100','A')
    raw=b'original quote source';doc=s.register_source_document(filename='quote.csv',content=raw,document_type='commercial_offer',project_id=lot['project_id'])
    payload=dict(supplier_id=supplier['id'],currency='RUB',vat_included=True,delivery_basis='included',
        price_date=TODAY,valid_until=date(2050,1,1),source_document_id=doc['id'],
        items=[dict(lot_item_id=item['id'],unit_price='112',minimum_batch='10')])
    # Matching characteristics are explicit; never inherited from an unrelated price.
    with db.connection() as conn:conn.execute("UPDATE lot_items SET specification='ГОСТ 13579-2018' WHERE id=?",(item['id'],))
    q=s.add_quote(lot['id'],QuoteCreate(**payload))
    memory=PriceMemory(s)
    result=memory.search('ФБС '+mark,today=TODAY)
    assert result['total']==2 and len(result['groups'])==1
    assert result['groups'][0]['current_stats']['median']=='106'
    qr=next(r for r in result['records'] if r['source_kind']=='quote')
    assert qr['source_document_id']==doc['id'] and qr['quote_id']==q['id']
    assert s.list_quotes(lot['id'])[0]['source_document_id']==doc['id']
    assert not any('кабель' in r['item_name'].lower() for r in memory.search(project_id=lot['project_id'],today=TODAY)['records'])
    payload['items'][0]['unit_price']='120';s.add_quote(lot['id'],QuoteCreate(**payload))
    assert memory.search('ФБС',today=TODAY)['total']==3
    w.document_file(db.one('SELECT * FROM source_documents WHERE id=?',(doc['id'],)))
    assert Path(doc['storage_path']).read_bytes()==raw


def test_preview_and_rejected_import_are_not_prices(workflow):
    db,s,w=workflow;price(workflow)
    with db.connection() as conn:
        conn.execute("INSERT INTO price_history_entries(item_name,normalized_name,unit_price,currency,created_at,status) VALUES ('ФБС 24.4.6','фбс','1','RUB','2026-09-01','rejected')")
    assert PriceMemory(s).search('ФБС',today=TODAY)['total']==1


def test_pagination_does_not_truncate_statistics(workflow):
    _,s,_=workflow
    for n in range(12):price(workflow,amount=str(100+n),supplier='S'+str(n))
    result=PriceMemory(s).search('ФБС',limit=2,offset=10,today=TODAY)
    assert len(result['records'])==2 and result['total']==12
    assert result['groups'][0]['current_stats']['count']==12
    assert result['groups'][0]['current_stats']['median']=='105.5'


def test_same_day_cheaper_offer_notification_and_read_actor(workflow):
    db,s,w=workflow;price(workflow,amount='100',pdate='2026-09-27');price(workflow,amount='88',supplier='B',pdate='2026-09-27')
    result=PriceMemory(s).search('ФБС',today=TODAY)
    alert=next(a for a in result['alerts'] if a['kind']=='better_offer')
    assert alert['change_pct']=='-12.00'
    with db.connection() as conn:conn.execute('INSERT INTO price_memory_alert_reads VALUES (?,?,?)',('staff-a',alert['event_id'],'2026-09-27'))
    assert next(a for a in PriceMemory(s).search('ФБС',today=TODAY)['alerts'] if a['event_id']==alert['event_id'])['read']
    from procurement.identity import authenticated_actor
    token=authenticated_actor.set('other-user')
    try:assert not next(a for a in PriceMemory(s).search('ФБС',today=TODAY)['alerts'] if a['event_id']==alert['event_id'])['read']
    finally:authenticated_actor.reset(token)


def test_zero_and_decimal_math(workflow):
    _,s,_=workflow;price(workflow,amount='0');price(workflow,amount='0',supplier='B')
    result=PriceMemory(s).search('ФБС',today=TODAY)
    assert result['groups'][0]['current_stats']['median']=='0'
    assert all(r['price_index_pct'] is None for r in result['records'])


def test_migration_idempotent_and_old_quote_fields_unchanged(workflow):
    db,s,w=workflow;lot,supplier=fbs(s)
    s.add_quote(lot['id'],QuoteCreate(supplier_id=supplier['id'],currency='RUB',vat_included=True,
        items=[dict(lot_item_id=lot['items'][0]['id'],unit_price='10')]))
    before=db.all('SELECT * FROM quotes');db.initialize();db.initialize()
    assert before==db.all('SELECT * FROM quotes')
    result=PriceMemory(s).search('ФБС',today=date.today())
    assert result['total']==1 and not result['groups']


def test_brand_in_specification_is_searchable_without_mixing_pb(workflow):
    _,s,_=workflow
    price(workflow,name='Блок фундаментный',specification='ФБС 24.4.6')
    price(workflow,name='ПБ 24.4.6',specification='для ФБС 24.4.6',supplier='B')
    rows=PriceMemory(s).search('ФБС 24.4.6',today=TODAY)['records']
    assert len(rows)==1 and rows[0]['item_name']=='Блок фундаментный'


def test_concurrent_cross_source_order_ambiguous_not_guessed(workflow):
    db,s,w=workflow;lot,supplier=fbs(s)
    price(workflow,supplier='A',pdate='2026-09-27')
    sid=next(r['id'] for r in s.list_suppliers() if r['email']=='a@example.test')
    s.add_quote(lot['id'],QuoteCreate(supplier_id=sid,currency='RUB',vat_included=True,
        price_date=TODAY,valid_until=date(2050,1,1),delivery_basis='included',
        items=[dict(lot_item_id=lot['items'][0]['id'],unit_price='112',minimum_batch='10')]))
    with db.connection() as conn:
        conn.execute("UPDATE lot_items SET specification='ГОСТ 13579-2018' WHERE id=?",(lot['items'][0]['id'],))
        conn.execute("UPDATE quotes SET created_at='2026-09-27T10:00:00+00:00'")
        conn.execute("UPDATE supplier_catalog_prices SET created_at='2026-09-27T10:00:00+00:00'")
    result=PriceMemory(s).search('ФБС',today=TODAY)
    assert result['groups'][0]['current_stats']['count']==0
    assert all(r['ambiguous_order'] and not r['current'] for r in result['records'])


def test_memory_auth_csrf_readonly_and_signed_source_acl(http_boundary):
    client,authority,settings,db=http_boundary
    assert client.get('/api/procurement/price-memory').status_code==401
    alice=login(client,authority)
    assert client.get('/api/procurement/price-memory?days=30').status_code==200
    assert client.get('/api/procurement/price-memory?days=12').status_code==422
    eid='a'*64;url='/api/procurement/price-memory/alerts/read'
    assert client.post(url,json={'event_ids':[eid]}).status_code==403
    assert client.post(url,headers=headers(alice),json={'event_ids':[eid]}).status_code==200
    authority.users[BOB]['read_only']=True;bob=login(client,authority,BOB)
    assert client.get('/api/procurement/price-memory').status_code==200
    assert client.post(url,headers=headers(bob),json={'event_ids':[eid]}).status_code==403
    authority.users[BOB]['modules']=[]
    assert client.get('/api/procurement/price-memory').status_code==403
    client.cookies.clear()
    assert client.get('/api/procurement/price-memory',headers={'X-OpenWebUI-User-Id':'admin'}).status_code==401


def test_invalid_quote_source_and_dates_leave_no_partial_quote(workflow):
    db,s,w=workflow;lot,supplier=fbs(s)
    base=dict(supplier_id=supplier['id'],currency='RUB',vat_included=True,items=[dict(lot_item_id=lot['items'][0]['id'],unit_price='10')])
    for extra in [dict(source_document_id=999),dict(price_date=TODAY,valid_until=date(2020,1,1))]:
        with pytest.raises(ValueError):s.add_quote(lot['id'],QuoteCreate(**base,**extra))
    assert not db.all('SELECT * FROM quotes')
