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
    with admin():inbox.prepare(aid,rows_of(inbox,aid),True)
    with pytest.raises(Exception,match='уже проверялось'):inbox.recognize(aid)


def test_recognition_endpoint_requires_admin_and_csrf(http_boundary):
    client,authority,settings,db=http_boundary
    url='/api/procurement/inbox/attachments/'+'a'*32+'/recognize'
    assert client.post(url,json={}).status_code==401
    user=login(client,authority,BOB)
    assert client.post(url,headers=headers(user),json={}).status_code==403
    login(client,authority)
    assert client.post(url,json={}).status_code==403


def test_native_scan_preserves_transcript_without_mining_quantities_as_prices():
    import shutil
    from pathlib import Path
    from procurement.upload_io import FilePayload
    if not shutil.which('tesseract') or not shutil.which('pdftoppm'):
        pytest.skip('Native Russian OCR is required; mandatory in Linux CI')
    path=Path(__file__).parent/'fixtures/russian_scan.pdf'
    r=extract_price_document(FilePayload(path),'scan.pdf')
    assert r.review_lines and any('ФБС' in line['text'] for line in r.review_lines)
    assert not r.items  # a specification with quantities is not a price list
    assert any('не распознана' in e for e in r.errors)


def test_ocr_deadline_and_page_limit_fail_closed(monkeypatch,tmp_path):
    from procurement.upload_io import FilePayload
    monkeypatch.setattr('procurement.imports.extract_document',lambda *args:result(scan_context=['']*13))
    with patch('procurement.document_analysis.extract_pdf_page_review',side_effect=AssertionError('OCR beyond page limit')):
        r=extract_price_document(b'pdf','scan.pdf')
        assert not r.items and '12 страниц' in r.errors[0]
    monkeypatch.setattr('procurement.imports.extract_document',lambda *args:result(scan_context=['','']))
    path=tmp_path/'scan.pdf';path.write_bytes(b'%PDF-test')
    monkeypatch.setattr('procurement.price_ocr.time.monotonic',iter([0,0,151]).__next__)
    with patch('procurement.document_analysis.extract_pdf_page_review',return_value={'text':'x','lines':[]}) as ocr:
        with pytest.raises(ValueError,match='слишком долго'):extract_price_document(FilePayload(path),'scan.pdf')
        assert ocr.call_count==1
