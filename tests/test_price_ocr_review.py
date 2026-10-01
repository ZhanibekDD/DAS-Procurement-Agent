"""The scan/typed PDF review paths share strict storage and ACL boundaries."""
from dataclasses import replace
from unittest.mock import patch
import json
import pytest
from procurement.price_ocr import scanned_items, document_date, seller_fields, extract_price_document
from procurement.imports import DocumentExtractResult
from procurement.catalog import Catalog, ALIASES
from procurement.incoming_mail import Inbox
from test_incoming_mail import ingest, rows_of, admin
from test_launch_workflow import workflow
from test_launch_http import http_boundary
from test_sso_adapter import login, headers, BOB


def cell(text,x,y,width=60,confidence=.98):
    return {'line':int(y+x),'text':text,'confidence':confidence,'bbox':{'left':x,'top':y,'width':width,'height':20}}


def table():
    return [cell('Товар (работы, услуги)',70,100,170),cell('Количество',280,100,80),
            cell('Цена 1 шт',380,95,80),cell('с НДС',400,118),
            cell('Цена',500,95),cell('1м3 с',500,118),cell('НДС',500,141),cell('Сумма с НДС',610,100,100),
            cell('ФБС 24.4.6',70,180,160),cell('218 шт',275,180),cell('118,374 м3',340,180,35),
            cell('1 000,00',395,180),cell('9 999,00',500,180),cell('218 000,00',610,180,100),
            cell('ФБС 12.4.6',70,215,160),cell('95 шт',275,215),cell('800,00',395,215),
            cell('Автоуслуги',70,250,160),cell('21 рейс',275,250),cell('500,00',395,250),
            cell('Итого:',370,290),cell('300000,00',610,290)]


def result(**kwargs):
    base=DocumentExtractResult('scan.pdf','0'*64,'unknown','','','','','','',None,None,'',False,scan_context=[''])
    return replace(base,**kwargs)


def test_columns_and_units_not_totals_or_volume_prices():
    rows=scanned_items(table(),1,'RUB',True)
    assert [r.item_name for r in rows]==['ФБС 24.4.6','ФБС 12.4.6','Автоуслуги']
    assert [r.unit_price for r in rows]==['1000.00','800.00','']
    assert [r.quantity for r in rows]==['218','95','21']
    assert [r.unit for r in rows]==['шт','шт','рейс']
    assert all(r.review_warning for r in rows)
    assert '9999' not in rows[0].unit_price and '218000' not in rows[0].unit_price


@pytest.mark.parametrize('confidence',[0,.5,.899])
def test_uncertain_price_retains_product_and_requires_correction(confidence):
    lines=table();next(c for c in lines if c['text']=='1 000,00')['confidence']=confidence
    rows=scanned_items(lines,1,'RUB',True)
    assert len(rows)==3 and rows[0].unit_price=='' and 'Цена не подтверждена' in rows[0].review_warning


@pytest.mark.parametrize('replacement',['Масса','Сумма','Цена доставки','Цена за 1000 шт'])
def test_no_price_from_unconfirmed_basis(replacement):
    lines=table();next(c for c in lines if c['text']=='Цена 1 шт')['text']=replacement
    rows=scanned_items(lines,1,'RUB',True)
    assert not any(r.unit_price for r in rows)


def test_missing_geometry_and_headers_never_invent_rows():
    assert not scanned_items([{'line':1,'text':'ФБС 24.4.6 218 1000','confidence':1}],1,'RUB',True)
    assert not scanned_items([c for c in table() if c['text']!='Товар (работы, услуги)'],1,'RUB',True)


@pytest.mark.parametrize('text',['218 шт','Цена 1 шт'])
def test_low_confidence_unit_or_heading_cannot_select_a_price(text):
    lines=table();next(c for c in lines if c['text']==text)['confidence']=.7
    rows=scanned_items(lines,1,'RUB',True)
    assert rows[0].unit_price==''


@pytest.mark.parametrize('text',['12 кг','9 м','5 шт'])
def test_numeric_dimension_outside_quantity_column_is_not_a_quantity(text):
    lines=table()+[cell(text,210,180,35)]
    row=scanned_items(lines,1,'RUB',True)[0]
    assert (row.quantity,row.unit,row.unit_price)==('218','шт','1000.00')


def test_missing_or_ambiguous_quantity_never_uses_neighboring_dimensions():
    lines=[c for c in table() if c['text'] not in ('218 шт','118,374 м3')]
    lines.append(cell('12 кг',210,180,35))
    row=scanned_items(lines,1,'RUB',True)[0]
    assert (row.quantity,row.unit,row.unit_price)==('','','')
    lines=table()+[cell('12 шт',340,180,35)]
    row=scanned_items(lines,1,'RUB',True)[0]
    assert (row.quantity,row.unit,row.unit_price)==('','','')


@pytest.mark.parametrize('article',['ABC-001','sku42','АБВ-218'])
def test_left_article_column_cannot_duplicate_product_rows(article):
    lines=table()+[cell('Артикул',0,100,40),cell(article,0,180,40)]
    rows=scanned_items(lines,1,'RUB',True)
    assert [r.item_name for r in rows]==['ФБС 24.4.6','ФБС 12.4.6','Автоуслуги']
    assert [r.unit_price for r in rows]==['1000.00','800.00','']


@pytest.mark.parametrize('left_header',['№','Артикул','Код товара'])
def test_centered_name_heading_does_not_hide_left_aligned_products(left_header):
    lines=table()+[cell(left_header,0,100,45),cell('SKU-01',0,180,45)]
    heading=next(c for c in lines if c['text']=='Товар (работы, услуги)')
    heading['bbox'].update(left=130,width=120)
    rows=scanned_items(lines,1,'RUB',True)
    assert [r.item_name for r in rows]==['ФБС 24.4.6','ФБС 12.4.6','Автоуслуги']
    assert [r.unit_price for r in rows]==['1000.00','800.00','']


def test_shipping_only_column_is_not_product_price():
    lines=[cell('Товар',70,100),cell('Количество',280,100),cell('Цена доставки',395,100),
           cell('ФБС 24.4.6',70,180),cell('218 шт',280,180),cell('1000',395,180)]
    assert not scanned_items(lines,1,'RUB',True)


def test_issuer_is_not_bank_buyer_or_sender_and_date_is_printed():
    text='ПАО «Банк» ИНН 7707083893\nПоставщик: ИНН 7707083893, АО "Пример" Воронежская обл\nПокупатель: ООО "Покупатель"'
    fields=seller_fields(text)
    assert fields['supplier_name']=='АО "Пример"' and fields['supplier_tax_id']=='7707083893'
    assert fields['supplier_region']=='Воронежская область'
    assert seller_fields('ПАО «Банк» ИНН 7707083893')=={}
    assert document_date('Счёт на оплату от 30 сентября 2026')=='2026-09-30'
    assert document_date('31 февраля 2026') is None
    assert document_date('Исх. №85 от «24» февраля 2026г.\nСрок действия: до 15.03.2026')=='2026-02-24'
    assert document_date('Срок действия: до 15.03.2026') is None
    assert document_date('Прайс-лист\nЦены указаны на 01.08.2026\nСрок действия не указан')=='2026-08-01'


def test_large_historical_pdf_review_keeps_unknown_region_and_never_marks_current(workflow):
    from procurement.procurement_routes import ReviewedPriceRows
    db,service,launch=workflow
    doc=service.register_source_document(filename='old-price.pdf',content=b'%PDF-test',document_type='price_list')
    rows=[]
    for n in range(501):
        row={key:'' for key in ALIASES}
        row.update(item_name=f'ФБС TEST-{n}',unit='шт',unit_price='100',currency='RUB',
                   vat='с НДС',price_date='2026-08-01',supplier_name='АО Испытательный завод',
                   email='prices@example.test')
        rows.append(row)
    rows[500]['unit_price']=''  # No implicit numeric value for an unpriced row.
    ReviewedPriceRows(rows=rows,confirmed_rub=True)
    preview=launch.save_preview('price_catalog_pdf',{'document_id':doc['id'],'rows':rows,
        'errors':[],'document_currencies':['RUB']})
    catalog=Catalog(service,launch)
    checked=catalog.review_pdf(preview['preview_id'],rows,True)
    assert len(checked['rows'])==500 and len(checked['errors'])==1
    assert all(not row['region'] for row in checked['rows'])
    report=catalog.apply_prices(checked['preview_id'],True)
    assert report['added']==500 and len(report['errors'])==1
    assert db.one('SELECT COUNT(*) AS n FROM supplier_catalog_prices')['n']==500
    assert db.one('SELECT region FROM suppliers WHERE email=?',('prices@example.test',))['region']=='Не указан'
    assert all(not row['current'] for row in catalog.prices('ФБС TEST-'))

    # Later independently reviewed regional evidence must make this exact
    # supplier routable without changing the earlier unknown-region prices.
    next_doc=service.register_source_document(filename='regional-price.pdf',content=b'%PDF-regional',document_type='price_list')
    regional=dict(rows[0],item_name='ФБС TEST-REGION',region='Воронежская область',source_row=1)
    next_preview=launch.save_preview('price_catalog_pdf',{'document_id':next_doc['id'],'rows':[regional],
        'errors':[],'document_currencies':['RUB']})
    checked=catalog.review_pdf(next_preview['preview_id'],[regional],True)
    assert catalog.apply_prices(checked['preview_id'],True)['added']==1
    supplier=db.one('SELECT region,cluster FROM suppliers WHERE email=?',('prices@example.test',))
    assert supplier['region']=='Воронежская область' and supplier['cluster']
    assert all(not row['current'] for row in catalog.prices('ФБС TEST-0'))
    assert any(row['current'] for row in catalog.prices('ФБС TEST-REGION'))


@pytest.mark.parametrize('label',['Дата окончания','Дата поставки','Дата действия','Дата окончания действия','Дата доставки','Дата отгрузки'])
def test_non_issue_dates_are_not_price_dates(label):
    assert document_date(label+': 15.03.2026') is None
    assert document_date(label+': 15 марта 2026') is None
    assert document_date(label+': 15.03.2026\nДата документа: 24.02.2026')=='2026-02-24'


@pytest.mark.parametrize('label',['Дата','Дата прайса','Дата документа'])
def test_issue_date_requires_immediate_date_after_exact_label(label):
    assert document_date(label+': 24.02.2026')=='2026-02-24'
    assert document_date(label+': не указана, действует до 15.03.2026') is None
    assert document_date(label+': 31 февраля 2026, уточнение 01.03.2026') is None


def test_ocr_only_in_review_path_and_all_lines_remain(monkeypatch,tmp_path):
    from procurement.upload_io import FilePayload
    path=tmp_path/'scan.pdf';path.write_bytes(b'%PDF-test')
    monkeypatch.setattr('procurement.imports.extract_document',lambda *args:result())
    lines=table()+[cell('Неразобранный текст 123',0,400),cell('руб.',0,430)]
    monkeypatch.setattr('procurement.document_analysis.extract_pdf_page_review',lambda *args:{'text':'\n'.join(c['text'] for c in lines),'lines':lines})
    r=extract_price_document(FilePayload(path),'scan.pdf')
    assert len(r.items)==3 and r.currency=='RUB'
    assert len(r.review_lines)==len(lines) and 'Неразобранный текст 123' in [c['text'] for c in r.review_lines]
    assert path.read_bytes()==b'%PDF-test'


def test_text_pdf_does_not_call_ocr(monkeypatch):
    expected=result(scan_context=[])
    monkeypatch.setattr('procurement.imports.extract_document',lambda *args:expected)
    with patch('procurement.document_analysis.extract_pdf_page_review',side_effect=AssertionError('unexpected OCR')):
        assert extract_price_document(b'pdf','text.pdf') is expected


def test_cached_unreviewed_mail_can_be_upgraded_once(workflow,tmp_path,monkeypatch):
    inbox,_=ingest(workflow,tmp_path);row=inbox.db.one('SELECT * FROM inbox_attachments');aid=row['id']
    draft=json.loads(row['draft_json']);draft.pop('recognition_version',None)
    with inbox.db.connection() as conn:conn.execute('UPDATE inbox_attachments SET draft_json=? WHERE id=?',(json.dumps(draft),aid))
    sha=inbox.attachment(aid)['sha256'];raw=inbox.path(row).read_bytes()
    original=__import__('procurement.incoming_mail',fromlist=['extract_draft']).extract_draft
    with patch('procurement.incoming_mail.extract_draft',wraps=original) as extraction:
        assert inbox.recognize(aid)['recognition_version']==2
        inbox.recognize(aid);assert extraction.call_count==1
    assert inbox.path(row).read_bytes()==raw and inbox.attachment(aid)['sha256']==sha
    assert not inbox.db.all('SELECT * FROM supplier_catalog_prices')


def test_reviewed_mail_never_replaced(workflow,tmp_path):
    inbox,_=ingest(workflow,tmp_path);row=inbox.db.one('SELECT * FROM inbox_attachments');aid=row['id']
    draft=json.loads(row['draft_json']);draft.pop('recognition_version',None)
    with inbox.db.connection() as conn:conn.execute('UPDATE inbox_attachments SET draft_json=? WHERE id=?',(json.dumps(draft),aid))
    with admin():
        rows=rows_of(inbox,aid);rows[0]['unit_price']='1777.12'
        checked=inbox.prepare(aid,rows,True)
        detail=inbox.detail(aid)
        assert detail['reviewed'] and not detail['applied']
        assert detail['rows'][0]['unit_price']=='1777.12'
        Catalog(workflow[1],workflow[2]).apply_prices(checked['preview_id'],True)
        assert inbox.detail(aid)['applied']
    with pytest.raises(Exception,match='уже проверялось'):inbox.recognize(aid)


def test_recognition_endpoint_requires_admin_and_csrf(http_boundary):
    client,authority,settings,db=http_boundary
    url='/api/procurement/inbox/attachments/'+'a'*32+'/recognize'
    assert client.post(url,json={}).status_code==401
    user=login(client,authority,BOB)
    assert client.post(url,headers=headers(user),json={}).status_code==403
    login(client,authority)
    assert client.post(url,json={}).status_code==403


@pytest.mark.parametrize('currency',['','USD','EUR'])
def test_pdf_currency_cannot_be_silently_relabelled_by_client(workflow,currency):
    from test_procurement_redesign import price_csv
    db,service,launch=workflow
    doc=service.register_source_document(filename='price.csv',content=price_csv(),document_type='price_list')
    catalog=Catalog(service,launch)
    row={k:price_csv().decode().strip().splitlines()[1].split(';')[n] for n,k in enumerate(ALIASES)}
    pid=launch.save_preview('price_catalog_pdf',{'document_id':doc['id'],'rows':[{**row,'currency':currency}]})['preview_id']
    assert row['currency']=='RUB'
    with pytest.raises(ValueError):catalog.review_pdf(pid,[row])
    if currency:
        with pytest.raises(ValueError):catalog.review_pdf(pid,[row],True)
    else:
        checked=catalog.review_pdf(pid,[{**row,'currency':''}],True)
        assert not checked['errors'] and checked['rows'][0]['currency']=='RUB'
    assert not db.all('SELECT * FROM supplier_catalog_prices')


def test_pdf_currency_confirmation_is_strict_boolean(http_boundary):
    client,authority,settings,db=http_boundary
    user=login(client,authority)
    for value in ('true',1,None):
        response=client.post('/api/procurement/catalog/unknown/review-pdf',headers=headers(user),
            json={'rows':[{'currency':'RUB'}],'confirmed_rub':value})
        assert response.status_code==422


@pytest.mark.parametrize('currency,pages',[('USD',[]),('EUR',[]),('', ['Цена RUB / USD']),('', ['Цена EUR'])])
def test_document_currency_evidence_survives_empty_tables(workflow,tmp_path,currency,pages):
    from test_procurement_redesign import price_csv
    db,service,launch=workflow
    doc=service.register_source_document(filename='price.csv',content=price_csv(),document_type='price_list')
    catalog=Catalog(service,launch)
    preview=catalog.extracted_price_preview(doc,result(items=[],currency=currency,page_texts=pages))
    assert set(preview['document_currencies'])-{'RUB'}
    row={k:price_csv().decode().strip().splitlines()[1].split(';')[n] for n,k in enumerate(ALIASES)}
    with pytest.raises(ValueError,match='другая валюта'):catalog.review_pdf(preview['preview_id'],[row],True)
    inbox,_=ingest(workflow,tmp_path);attachment=inbox.db.one('SELECT * FROM inbox_attachments')
    with db.connection() as conn:conn.execute('UPDATE inbox_attachments SET draft_json=? WHERE id=?',(json.dumps(preview),attachment['id']))
    with admin(),pytest.raises(ValueError,match='другая валюта'):inbox.prepare(attachment['id'],[row],True)
    assert not db.all('SELECT * FROM supplier_catalog_prices')


@pytest.mark.parametrize('action',['prepare','apply'])
@pytest.mark.parametrize('currency',['USD','EUR'])
def test_legacy_pending_pdf_rechecks_evidence_without_changing_reviewed_rows(workflow,tmp_path,monkeypatch,action,currency):
    inbox,_=ingest(workflow,tmp_path);attachment=inbox.db.one('SELECT * FROM inbox_attachments');aid=attachment['id']
    with admin():checked=inbox.prepare(aid,rows_of(inbox,aid),True)
    draft=json.loads(attachment['draft_json']);draft.pop('document_currencies',None);draft.pop('recognition_version',None);draft['rows']=[]
    with inbox.db.connection() as conn:conn.execute('UPDATE inbox_attachments SET filename=?,draft_json=? WHERE id=?',('legacy.pdf',json.dumps(draft),aid))
    monkeypatch.setattr(inbox.__class__,'path',lambda self,a:tmp_path/'unused.pdf')
    monkeypatch.setattr('procurement.incoming_mail.extract_draft',lambda *args:{'rows':[],'document_currencies':[currency]})
    before=inbox.db.one('SELECT data_json FROM launch_previews WHERE id=?',(checked['preview_id'],))['data_json']
    with admin(),pytest.raises(ValueError,match='другая валюта'):
        if action=='prepare':inbox.prepare(aid,[{k:r[k] for k in ALIASES} for r in checked['rows']],True)
        else:Catalog(workflow[1],workflow[2]).apply_prices(checked['preview_id'],True)
    assert inbox.db.one('SELECT data_json FROM launch_previews WHERE id=?',(checked['preview_id'],))['data_json']==before
    assert json.loads(inbox.attachment(aid)['draft_json'])['rows']==[]
    assert not inbox.db.all('SELECT * FROM supplier_catalog_prices')


@pytest.mark.parametrize('currency',['USD','EUR'])
@pytest.mark.parametrize('phase',['review','apply'])
@pytest.mark.parametrize('original_name',['legacy.pdf','older.csv'])
def test_legacy_standalone_pdf_rechecks_original_not_empty_preview(workflow,monkeypatch,currency,phase,original_name):
    from test_procurement_redesign import price_csv
    from pathlib import Path
    from procurement.table_ingest import read_table
    db,service,launch=workflow
    raw=(Path(__file__).parent/'fixtures/russian_scan.pdf').read_bytes()
    original=service.register_source_document(filename=original_name,content=raw,document_type='price_list')
    doc=service.register_source_document(filename='legacy.pdf',content=raw,document_type='price_list',_price_import=True)
    assert doc['id']==original['id'] and doc['filename']==original_name
    catalog=Catalog(service,launch)
    row={k:price_csv().decode().strip().splitlines()[1].split(';')[n] for n,k in enumerate(ALIASES)}
    if phase=='review':p=launch.save_preview('price_catalog_pdf',{'document_id':doc['id'],'rows':[]})
    else:p=catalog.price_preview(doc,read_table(price_csv(),'prices.csv'))
    before=db.one('SELECT data_json FROM launch_previews WHERE id=?',(p['preview_id'],))['data_json']
    def extract_saved_source(content,filename):
        from procurement.upload_io import open_payload
        with open_payload(content) as stream:assert stream.read()==raw
        assert filename=='source.pdf'
        return result(items=[],currency=currency)
    monkeypatch.setattr('procurement.price_ocr.extract_price_document',extract_saved_source)
    with pytest.raises(ValueError,match='другая валюта'):
        if phase=='review':catalog.review_pdf(p['preview_id'],[row],True)
        else:catalog.apply_prices(p['preview_id'],True)
    assert db.one('SELECT data_json FROM launch_previews WHERE id=?',(p['preview_id'],))['data_json']==before
    assert not db.all('SELECT * FROM supplier_catalog_prices')


def test_native_scan_preserves_transcript_without_mining_quantities_as_prices():
    import shutil
    from pathlib import Path
    from procurement.upload_io import FilePayload
    if not shutil.which('tesseract') or not shutil.which('pdftoppm'):
        pytest.skip('Native Russian OCR is required; mandatory in Linux CI')
    path=Path(__file__).parent/'fixtures/russian_scan.pdf'
    import hashlib
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    r=extract_price_document(FilePayload(path),'scan.pdf')
    # OCR may render Cyrillic brand letters with visually identical Latin
    # glyphs. Require the complete source facts, not one typography choice.
    text='\n'.join(line['text'] for line in r.review_lines)
    assert 'Спецификация' in text and '24.4.6' in text and '179' in text
    assert '001230040500' in text and 'количество уточнить' in text
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    assert not r.items  # a specification with quantities is not a price list
    assert any('не распознана' in e for e in r.errors)


def test_ocr_deadline_and_page_limit_fail_closed(monkeypatch,tmp_path):
    from procurement.upload_io import FilePayload
    partial=scanned_items(table(),1,'RUB',True)
    monkeypatch.setattr('procurement.imports.extract_document',lambda *args:result(scan_context=['typed']+['']*12,items=partial))
    with patch('procurement.document_analysis.extract_pdf_page_review',side_effect=AssertionError('OCR beyond page limit')):
        with pytest.raises(ValueError,match='12 страниц'):extract_price_document(b'pdf','scan.pdf')
    monkeypatch.setattr('procurement.imports.extract_document',lambda *args:result(scan_context=['','']))
    path=tmp_path/'scan.pdf';path.write_bytes(b'%PDF-test')
    monkeypatch.setattr('procurement.price_ocr.time.monotonic',iter([0,0,151]).__next__)
    with patch('procurement.document_analysis.extract_pdf_page_review',return_value={'text':'x','lines':[]}) as ocr:
        with pytest.raises(ValueError,match='слишком долго'):extract_price_document(FilePayload(path),'scan.pdf')
        assert ocr.call_count==1
