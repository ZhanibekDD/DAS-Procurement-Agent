"""Supplier replies become quotes of the purchase only through an exact Message-ID and a person's check."""
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from email.message import EmailMessage

import pytest
from pydantic import ValidationError

from procurement.catalog import ALIASES, Catalog
from procurement.inbox_quotes import InboxQuote, quote_draft, save_quote
from procurement.incoming_mail import Inbox
from procurement.models import CampaignCreate, LotCreate, LotItemCreate, ProjectCreate, SupplierCreate
from procurement.service import ConflictError
from test_incoming_mail import admin, rows_of
from test_launch_http import http_boundary
from test_launch_workflow import workflow
from test_sso_adapter import ALICE, headers, login

RFQ_ID = '<rfq-0001@dnepr.test>'


def table(*rows):
    keys = list(ALIASES)
    base = dict(currency='RUB', vat='с НДС', supplier_name='ООО Поставщик', email='a@example.test', price_date='2026-10-01',
                valid_until='2099-01-01', region='ХМАО')
    lines = [';'.join(keys)] + [';'.join({**base, **row}.get(k, '') for k in keys) for row in rows]
    return ('\n'.join(lines) + '\n').encode()


OFFER = table(dict(item_name='ФБС 24.4.6', unit='шт', unit_price='5 120,50'),
              dict(item_name='Песок карьерный', unit='м3', unit_price='800'))


def reply(raw=OFFER, sender='Отдел продаж <A@Example.test>', in_reply_to=RFQ_ID, references=None, filename='kp.csv'):
    mail = EmailMessage()
    mail['From'], mail['To'], mail['Subject'] = sender, 'snab@dnepr.test', 'Re: Запрос цен'
    mail['Message-ID'] = f'<{uuid.uuid4().hex}@example.test>'
    if in_reply_to:
        mail['In-Reply-To'] = in_reply_to
    if references:
        mail['References'] = references
    mail.set_content('КП во вложении. Подтвердите заказ ответным письмом.')
    mail.add_attachment(raw, maintype='application', subtype='octet-stream', filename=filename)
    return mail.as_bytes()


def purchase(service, rfq_id=RFQ_ID, email='a@example.test', title='Фундаментные блоки', cluster='cluster_1'):
    project = service.create_project(ProjectCreate(name='ТЕСТ ЭХЗ ' + title, region='ХМАО', cluster=cluster,
                                                   delivery_address='Сургут, тестовая 1'))
    lot = service.create_lot(LotCreate(
        project_id=project['id'], title=title, region='ХМАО', cluster=cluster, delivery_address='Сургут, тестовая 1',
        response_deadline=date.today() + timedelta(days=7),
        items=[LotItemCreate(name='ФБС 24.4.6', quantity=Decimal('10'), unit='шт'),
               LotItemCreate(name='Кабель ВВГ 3х2,5', quantity=Decimal('100'), unit='м')]))
    supplier = service.create_supplier(SupplierCreate(name='Поставщик ' + title, region='ХМАО',
                                                      email=email, categories=['ФБС'], cluster=cluster, verified=True))
    message = service.create_campaign(lot['id'], CampaignCreate(supplier_ids=[supplier['id']]))['messages'][0]
    with service.db.connection() as conn:
        conn.execute('''INSERT INTO mail_receipts(message_id,rfc_message_id,sender,recipients_json,smtp_host,status,attempt,
            started_at,attachments_json) VALUES (?,?,?,?,?,'sent',1,?,'[]')''',
                     (message['id'], rfq_id, 'snab@dnepr.test', json.dumps([email]), 'smtp.test', '2026-10-10T00:00:00+00:00'))
        conn.execute("UPDATE outbox_messages SET status='sent' WHERE id=?", (message['id'],))
    return lot, supplier


def receive(workflow, tmp_path, raw, uid=1):
    db, service, launch = workflow
    inbox = Inbox(service, launch)
    path = tmp_path / f'{uid}.eml'
    path.write_bytes(raw)
    mid = inbox.ingest('account', 1, uid, path)
    message = next(m for m in inbox.listing()['messages'] if m['id'] == mid)
    return inbox, message


def form(lot, supplier, prices, **extra):
    values = dict(lot_id=lot['id'], supplier_id=supplier['id'], vat_included=True, confirmed_source=True,
                  items=[{'lot_item_id': lot['items'][n]['id'], 'unit_price': price} for n, price in prices.items()])
    return InboxQuote(**{**values, **extra})


def test_reply_to_request_becomes_quote_after_confirmation(workflow, tmp_path):
    db, service, _ = workflow
    lot, supplier = purchase(service)
    inbox, message = receive(workflow, tmp_path, reply())
    assert message['rfq'] == {'supplier_id': supplier['id'], 'lot_id': lot['id'], 'lot_title': 'Фундаментные блоки',
                              'archived': False, 'supplier_name': supplier['name'], 'sender_matches': True}
    aid = message['attachments'][0]['id']
    assert message['attachments'][0]['quote'] is None
    draft = quote_draft(service, inbox, aid, lot['id'])
    fbs, cable = draft['items']
    assert (fbs['suggested_price'], fbs['source_row'], fbs['candidates']) == ('5120.50', 'ФБС 24.4.6', 1)
    assert (cable['suggested_price'], cable['candidates']) == ('', 0)  # absent from the offer: nothing invented
    assert draft['currency'] == 'RUB' and draft['foreign_currencies'] == [] and draft['document_vat'] == ['с НДС']
    assert not db.all('SELECT * FROM quotes') and not db.all('SELECT * FROM source_documents')  # a draft saves nothing

    with pytest.raises(ValueError, match='Сверьте'):
        save_quote(service, inbox, aid, form(lot, supplier, {0: '5120.50'}, confirmed_source=False))
    with admin():
        saved = save_quote(service, inbox, aid, form(lot, supplier, {0: '5120.50'}, lead_days=5, payment_terms='50% аванс'))
    quote = db.one('SELECT * FROM quotes WHERE id=?', (saved['quote_id'],))
    assert (quote['lot_id'], quote['supplier_id'], quote['currency'], quote['vat_included'], quote['lead_days']) == \
           (lot['id'], supplier['id'], 'RUB', 1, 5)
    assert [row['unit_price'] for row in db.all('SELECT unit_price FROM quote_items WHERE quote_id=?', (quote['id'],))] == ['5120.50']
    document = db.one('SELECT * FROM source_documents WHERE id=?', (quote['source_document_id'],))
    assert (document['document_type'], document['project_id'], document['supplier_id']) == \
           ('commercial_offer', lot['project_id'], supplier['id'])
    assert document['sha256'] == db.one('SELECT sha256 FROM inbox_attachments WHERE id=?', (aid,))['sha256']
    assert service.get_lot(lot['id'])['status'] == 'quotes_received'
    listed = next(m for m in inbox.listing()['messages'] if m['id'] == message['id'])['attachments'][0]['quote']
    assert (listed['quote_id'], listed['lot_title']) == (quote['id'], 'Фундаментные блоки')
    assert db.one("SELECT details_json FROM audit_log WHERE action='inbox_quote_saved'")
    with pytest.raises(ConflictError, match='уже сохранено'):
        save_quote(service, inbox, aid, form(lot, supplier, {0: '1'}))
    assert len(db.all('SELECT * FROM quotes')) == 1
    assert db.one('SELECT status FROM outbox_messages')['status'] == 'sent'  # nothing was mailed back


def test_only_an_exact_message_id_links_the_reply(workflow, tmp_path):
    _, service, _ = workflow
    lot, supplier = purchase(service)
    other_lot, other_supplier = purchase(service, rfq_id='<rfq-0002@dnepr.test>', email='b@example.test', title='Песок')
    cases = [
        (dict(in_reply_to='<rfq-0001@other.test>'), None),  # similar, not equal
        (dict(in_reply_to=None), None),  # same subject and sender, no reply headers
        (dict(in_reply_to=None, references='<x@y.test> ' + RFQ_ID), (lot['id'], supplier['id'])),
        (dict(in_reply_to=RFQ_ID, references='<rfq-0002@dnepr.test>'), None),  # two purchases: ambiguous
    ]
    for uid, (headers_, expected) in enumerate(cases, 1):
        _, message = receive(workflow, tmp_path, reply(**headers_), uid=uid)
        got = message['rfq'] and (message['rfq']['lot_id'], message['rfq']['supplier_id'])
        assert got == expected, headers_
    _, message = receive(workflow, tmp_path, reply(sender='Другой <other@example.test>'), uid=10)
    assert message['rfq']['lot_id'] == lot['id'] and message['rfq']['sender_matches'] is False


def test_suggestions_are_conservative(workflow, tmp_path):
    _, service, _ = workflow
    lot, supplier = purchase(service)
    offer = table(dict(item_name='ПБ 24.4.6', unit='шт', unit_price='10'),  # another family, never ФБС
                  dict(item_name='Кабель ВВГ 3х2,5 ГОСТ', unit='км', unit_price='90000'),  # unit differs
                  dict(item_name='Кабель', unit='м', unit_price='1'))  # too generic for the item
    inbox, message = receive(workflow, tmp_path, reply(raw=offer))
    fbs, cable = quote_draft(service, inbox, message['attachments'][0]['id'], lot['id'])['items']
    assert (fbs['suggested_price'], fbs['candidates']) == ('', 0)
    assert cable['suggested_price'] == '' and cable['unit_differs'] is True and cable['source_unit'] == 'км'
    twice = table(dict(item_name='ФБС 24.4.6', unit='шт', unit_price='100'),
                  dict(item_name='Блок ФБС 24-4-6', unit='шт', unit_price='120'))
    inbox, message = receive(workflow, tmp_path, reply(raw=twice), uid=2)
    fbs = quote_draft(service, inbox, message['attachments'][0]['id'], lot['id'])['items'][0]
    assert (fbs['suggested_price'], fbs['candidates']) == ('', 2)  # two candidates: the person chooses


def test_guards_release_the_claim(workflow, tmp_path):
    db, service, _ = workflow
    lot, supplier = purchase(service)
    inbox, message = receive(workflow, tmp_path, reply())
    aid = message['attachments'][0]['id']
    with pytest.raises(ValueError, match='дважды'):
        save_quote(service, inbox, aid, InboxQuote(
            lot_id=lot['id'], supplier_id=supplier['id'], vat_included=False, confirmed_source=True,
            items=[{'lot_item_id': lot['items'][0]['id'], 'unit_price': '1'}] * 2))
    with pytest.raises(ValidationError):
        InboxQuote(lot_id=lot['id'], supplier_id=supplier['id'], vat_included='да', confirmed_source=True,
                   items=[{'lot_item_id': lot['items'][0]['id'], 'unit_price': '1'}])
    stranger = service.create_supplier(SupplierCreate(name='Чужой кластер', region='Воронеж', cluster='cluster_2'))
    with pytest.raises(ValueError, match='cluster'):
        save_quote(service, inbox, aid, form(lot, stranger, {0: '1'}))
    with pytest.raises(ValueError, match='другой закупки'):
        save_quote(service, inbox, aid, InboxQuote(
            lot_id=lot['id'], supplier_id=supplier['id'], vat_included=False, confirmed_source=True,
            items=[{'lot_item_id': 9999, 'unit_price': '1'}]))
    with pytest.raises(ValueError, match='раньше даты цены'):
        save_quote(service, inbox, aid, form(lot, supplier, {0: '1'}, price_date='2026-10-05', valid_until='2026-10-01'))
    assert not db.all('SELECT * FROM inbox_quotes') and not db.all('SELECT * FROM quotes')
    assert not db.all('SELECT * FROM source_documents')  # a refused quote leaves the file without an owner
    # The file already belongs to another purchase's supplier: no silent reassignment.
    other_lot, other_supplier = purchase(service, rfq_id='<rfq-0002@dnepr.test>', email='b@example.test', title='Песок')
    with admin():
        save_quote(service, inbox, aid, form(other_lot, other_supplier, {0: '7'}))
    inbox2, message2 = receive(workflow, tmp_path, reply(), uid=2)  # the same file in another letter
    with pytest.raises(ConflictError, match='другому объекту'):
        save_quote(service, inbox2, message2['attachments'][0]['id'], form(lot, supplier, {0: '1'}))
    assert len(db.all('SELECT * FROM inbox_quotes')) == 1 and len(db.all('SELECT * FROM quotes')) == 1
    # A claim left by a crashed request expires; a fresh one blocks a second save.
    aid2 = message2['attachments'][0]['id']
    with db.connection() as conn:
        conn.execute("INSERT INTO inbox_quotes VALUES (?,?,?,?,NULL,'x','2000-01-01T00:00:00+00:00')",
                     (aid2, message2['id'], lot['id'], supplier['id']))
    with admin():
        save_quote(service, inbox2, aid2, form(other_lot, other_supplier, {0: '7'}))  # same owner as the file
    inbox3, message3 = receive(workflow, tmp_path, reply(raw=table(dict(item_name='ФБС 24.4.6', unit='шт', unit_price='9'))), uid=3)
    aid3 = message3['attachments'][0]['id']
    with db.connection() as conn:
        conn.execute("INSERT INTO inbox_quotes VALUES (?,?,?,?,NULL,'x',?)", (aid3, message3['id'], lot['id'], supplier['id'],
                                                                            date.today().isoformat() + 'T23:59:59+00:00'))
    with pytest.raises(ConflictError, match='сохраняется'):
        save_quote(service, inbox3, aid3, form(lot, supplier, {0: '9'}))
    with db.connection() as conn:
        conn.execute('DELETE FROM inbox_quotes WHERE attachment_id=?', (aid3,))
    service.set_lot_archived(lot['id'], True)
    with pytest.raises(ConflictError, match='архиве'):
        save_quote(service, inbox3, aid3, form(lot, supplier, {0: '9'}))
    assert not db.one('SELECT 1 FROM inbox_quotes WHERE attachment_id=?', (aid3,))


def test_quote_committed_before_a_crash_is_recovered_not_duplicated(workflow, tmp_path):
    db, service, _ = workflow
    lot, supplier = purchase(service)
    inbox, message = receive(workflow, tmp_path, reply())
    aid = message['attachments'][0]['id']
    with admin():
        saved = save_quote(service, inbox, aid, form(lot, supplier, {0: '5120.50'}))
    # Simulate a stop between add_quote() and the claim update: the claim lost its quote id long ago.
    with db.connection() as conn:
        conn.execute("UPDATE inbox_quotes SET quote_id=NULL, created_at='2000-01-01T00:00:00+00:00' WHERE attachment_id=?", (aid,))
    with pytest.raises(ConflictError, match='уже сохранено'):
        save_quote(service, inbox, aid, form(lot, supplier, {0: '5120.50'}))
    assert len(db.all('SELECT * FROM quotes')) == 1
    assert db.one('SELECT quote_id FROM inbox_quotes WHERE attachment_id=?', (aid,))['quote_id'] == saved['quote_id']


def test_offer_in_another_currency_is_refused(workflow, tmp_path):
    db, service, _ = workflow
    lot, supplier = purchase(service)
    inbox, message = receive(workflow, tmp_path, reply(raw=table(dict(item_name='ФБС 24.4.6', unit='шт', unit_price='60', currency='USD'))))
    aid = message['attachments'][0]['id']
    assert quote_draft(service, inbox, aid, lot['id'])['foreign_currencies'] == ['USD']
    with pytest.raises(ValueError, match='Конвертации нет'):
        save_quote(service, inbox, aid, form(lot, supplier, {0: '60'}))
    assert not db.all('SELECT * FROM quotes')


def test_price_list_already_imported_is_reused_as_the_quote_source(workflow, tmp_path):
    db, service, launch = workflow
    lot, supplier = purchase(service)
    inbox, message = receive(workflow, tmp_path, reply())
    aid = message['attachments'][0]['id']
    with admin():
        preview = inbox.prepare(aid, rows_of(inbox, aid), True)
        Catalog(service, launch).apply_prices(preview['preview_id'], True)
        saved = save_quote(service, inbox, aid, form(lot, supplier, {0: '5120.50'}))
    document = db.one('SELECT d.* FROM quotes q JOIN source_documents d ON d.id=q.source_document_id WHERE q.id=?', (saved['quote_id'],))
    assert document['document_type'] == 'price_list' and len(db.all('SELECT * FROM source_documents')) == 1


def test_old_worker_tables_are_unchanged(workflow):
    db, _, _ = workflow
    with db.connection() as conn:
        assert len(conn.execute('PRAGMA table_info(inbox_messages)').fetchall()) == 13
        assert len(conn.execute('PRAGMA table_info(inbox_attachments)').fetchall()) == 8


def test_quote_endpoints_are_admin_only_with_csrf(http_boundary, tmp_path):
    client, authority, settings, db = http_boundary
    from procurement.launch_workflow import LaunchWorkflow
    from procurement.service import ProcurementService
    service = ProcurementService(db)
    lot, supplier = purchase(service)
    inbox = Inbox(service, LaunchWorkflow(service))
    path = tmp_path / 'in.eml'
    path.write_bytes(reply())
    inbox.ingest('account', 1, 1, path)
    aid = db.one('SELECT id FROM inbox_attachments')['id']
    base = f'/api/procurement/inbox/attachments/{aid}'
    payload = {'lot_id': lot['id'], 'supplier_id': supplier['id'], 'vat_included': True, 'confirmed_source': True,
               'items': [{'lot_item_id': lot['items'][0]['id'], 'unit_price': '5120.50'}]}
    staff = login(client, authority)
    assert client.get(f'{base}/quote-draft?lot_id={lot["id"]}').status_code == 403
    assert client.post(f'{base}/quote', json=payload, headers=headers(staff)).status_code == 403
    authority.users[ALICE]['access_admin'] = True
    alice = login(client, authority)
    listed = client.get('/api/procurement/inbox').json()['messages'][0]
    assert listed['rfq']['lot_id'] == lot['id']
    draft = client.get(f'{base}/quote-draft?lot_id={lot["id"]}')
    assert draft.status_code == 200 and draft.json()['items'][0]['suggested_price'] == '5120.50'
    assert client.post(f'{base}/quote', json=payload).status_code == 403  # no CSRF token
    assert client.post(f'{base}/quote', json={**payload, 'vat_included': None}, headers=headers(alice)).status_code == 422
    response = client.post(f'{base}/quote', json=payload, headers=headers(alice))
    assert response.status_code == 200, response.text
    assert client.post(f'{base}/quote', json=payload, headers=headers(alice)).status_code == 409
    assert db.one('SELECT actor FROM inbox_quotes')['actor'] == ALICE
