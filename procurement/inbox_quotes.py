"""Supplier replies to a request for quotation become quotes of that purchase, with a human in the loop.

The worker keeps the reply's In-Reply-To/References. A reply is linked to the purchase only by an
exact match with the Message-ID of our sent request (mail_receipts.rfc_message_id); the subject and
the sender never establish the link. A person opens the attachment, enters the prices for the lot
items (suggested only where exactly one row of the attachment matches) and saves the quote; the
attachment becomes the quote's source document. Nothing is saved without that confirmation.

Separate tables keep the previous worker image working on the same database.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr
from typing import Literal

from pydantic import Field

from .db import utcnow
from .identity import trusted_actor
from .models import QuoteCreate, QuoteItemCreate, StrictModel
from .price_memory import INVALID_DESIGNATION, designation_key, material_key, normalized, unit_key
from .service import ConflictError
from .upload_io import FilePayload

SCHEMA = '''
CREATE TABLE IF NOT EXISTS inbox_message_refs (
 message_id TEXT PRIMARY KEY REFERENCES inbox_messages(id),
 in_reply_to TEXT NOT NULL DEFAULT '', references_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS inbox_quotes (
 attachment_id TEXT PRIMARY KEY REFERENCES inbox_attachments(id), message_id TEXT NOT NULL,
 lot_id INTEGER NOT NULL, supplier_id INTEGER NOT NULL, quote_id INTEGER REFERENCES quotes(id),
 actor TEXT NOT NULL, created_at TEXT NOT NULL);
'''

_MESSAGE_ID = re.compile(r'<[^<>\s]{3,250}>')
MAX_REFERENCES = 20
STALE_CLAIM = timedelta(minutes=15)
ARCHIVED = 'Закупка в архиве. Верните её из архива, чтобы добавить КП.'


class InboxQuoteItem(StrictModel):
    lot_item_id: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    note: str = Field(default='', max_length=2000)


class InboxQuote(StrictModel):
    lot_id: int = Field(gt=0)
    supplier_id: int = Field(gt=0)
    # The quote must state VAT; the currency is the purchase's own, never converted.
    vat_included: bool = Field(strict=True)
    delivery_cost: Decimal = Field(default=Decimal('0'), ge=0)
    lead_days: int = Field(default=0, ge=0, le=3650)
    payment_terms: str = Field(default='', max_length=1000)
    valid_until: date | None = None
    price_date: date | None = None
    delivery_basis: Literal['included', 'pickup', 'extra', ''] = ''
    items: list[InboxQuoteItem] = Field(min_length=1, max_length=500)
    confirmed_source: bool = Field(strict=True)


def message_ids(value) -> list[str]:
    return _MESSAGE_ID.findall(str(value or ''))[:MAX_REFERENCES]


def store_refs(conn, mid: str, mail) -> None:
    """Called by the worker inside the transaction that stores the message."""
    in_reply_to = (message_ids(mail.get('In-Reply-To')) or [''])[0]
    references = message_ids(mail.get('References'))
    if in_reply_to or references:
        conn.execute('INSERT OR REPLACE INTO inbox_message_refs VALUES (?,?,?)',
                     (mid, in_reply_to, json.dumps(references)))


def rfq_link(conn, mid: str) -> dict | None:
    """The request this message answers, by exact Message-ID only; None when absent or ambiguous."""
    ref = conn.execute('SELECT * FROM inbox_message_refs WHERE message_id=?', (mid,)).fetchone()
    if not ref:
        return None
    tokens = sorted({t for t in [ref['in_reply_to'], *json.loads(ref['references_json'])] if t})
    if not tokens:
        return None
    rows = conn.execute(f'''SELECT m.supplier_id, m.recipient, c.lot_id, l.title AS lot_title,
            l.archived_at != '' AS archived, s.name AS supplier_name
        FROM mail_receipts r JOIN outbox_messages m ON m.id=r.message_id
        JOIN campaigns c ON c.id=m.campaign_id JOIN lots l ON l.id=c.lot_id
        JOIN suppliers s ON s.id=m.supplier_id
        WHERE r.rfc_message_id IN ({','.join('?' * len(tokens))})''', tokens).fetchall()
    if len({(r['lot_id'], r['supplier_id']) for r in rows}) != 1:
        return None
    link = dict(rows[0])
    message = conn.execute('SELECT sender FROM inbox_messages WHERE id=?', (mid,)).fetchone()
    sender = parseaddr(message['sender'] if message else '')[1].casefold()
    link['sender_matches'] = bool(sender) and sender == parseaddr(link.pop('recipient') or '')[1].casefold()
    link['archived'] = bool(link['archived'])
    return link


def listing_extras(db, messages: list[dict]) -> None:
    with db.connection() as conn:
        for message in messages:
            message['rfq'] = rfq_link(conn, message['id'])
            saved = {row['attachment_id']: dict(row) for row in conn.execute(
                '''SELECT q.attachment_id, q.quote_id, q.lot_id, l.title AS lot_title
                   FROM inbox_quotes q JOIN lots l ON l.id=q.lot_id
                   WHERE q.message_id=? AND q.quote_id IS NOT NULL''', (message['id'],))}
            for attachment in message.get('attachments', []):
                attachment['quote'] = saved.get(attachment['id'])


def _price(value) -> str:
    try:
        number = Decimal(str(value).replace(' ', '').replace(' ', '').replace(',', '.'))
    except InvalidOperation:
        return ''
    return str(number) if number.is_finite() and number >= 0 else ''


def _matches(item: dict, row: dict) -> bool:
    """Conservative: same complete mark, same material key, or the row contains the whole item name."""
    name, spec = row.get('item_name', ''), row.get('specification', '')
    mark = designation_key(item['name'], item.get('specification', ''))
    if mark and mark != INVALID_DESIGNATION:
        return mark == designation_key(name, spec)
    if material_key(item['name']) and material_key(item['name']) == material_key(name):
        return True
    wanted = normalized(item['name'])
    return len(wanted) >= 4 and wanted in normalized(name)


def _foreign_currency(draft: dict, currency: str) -> list[str]:
    declared = set(draft.get('document_currencies') or []) | {r.get('currency') for r in draft.get('rows', [])}
    return sorted(c for c in declared if c and c != currency)


def quote_draft(service, inbox, aid: str, lot_id: int) -> dict:
    """Lot items with a suggested price where exactly one attachment row matches; nothing is saved."""
    attachment = inbox.attachment(aid)
    inbox.path(attachment)  # the immutable original must still be intact
    lot = service.get_lot(lot_id)
    draft = json.loads(attachment['draft_json'])
    rows = draft.get('rows', [])
    items = []
    for item in lot['items']:
        found = [row for row in rows if _matches(item, row)]
        row = found[0] if len(found) == 1 else None
        unit_differs = bool(row and row.get('unit') and unit_key(row['unit']) != unit_key(item['unit']))
        items.append({'lot_item_id': item['id'], 'name': item['name'], 'specification': item.get('specification', ''),
                      'quantity': item['quantity'], 'unit': item['unit'],
                      'suggested_price': _price(row.get('unit_price', '')) if row and not unit_differs else '',
                      'source_row': row.get('item_name', '')[:200] if row else '',
                      'source_unit': row.get('unit', '')[:30] if row else '',
                      'unit_differs': unit_differs, 'candidates': len(found)})
    saved = service.db.one('''SELECT q.quote_id, l.title AS lot_title FROM inbox_quotes q JOIN lots l ON l.id=q.lot_id
        WHERE q.attachment_id=? AND q.quote_id IS NOT NULL''', (aid,))
    return {'attachment_id': aid, 'filename': attachment['filename'], 'lot_id': lot['id'], 'lot_title': lot['title'],
            'archived': bool(lot.get('archived_at')), 'currency': lot['currency'],
            'foreign_currencies': _foreign_currency(draft, lot['currency']),
            'document_vat': sorted({r.get('vat', '')[:40] for r in rows if r.get('vat')}),
            'items': items, 'rows': len(rows), 'errors': draft.get('errors', []), 'saved': saved}


def save_quote(service, inbox, aid: str, data: InboxQuote) -> dict:
    if data.confirmed_source is not True:
        raise ValueError('Сверьте поставщика, цены и НДС с оригиналом КП и отметьте это')
    attachment = inbox.attachment(aid)
    path = inbox.path(attachment)
    lot = service.get_lot(data.lot_id)
    if lot.get('archived_at'):
        raise ConflictError(ARCHIVED)
    foreign = _foreign_currency(json.loads(attachment['draft_json']), lot['currency'])
    if foreign:
        raise ValueError(f'В КП указана валюта {", ".join(foreign)}, а закупка в {lot["currency"]}. Конвертации нет')
    # The checks of add_quote, before the file gets an owner: a refused quote must not register the source.
    service._supplier_cluster(service.get_supplier(data.supplier_id), service._lot_cluster(lot))
    submitted = [item.lot_item_id for item in data.items]
    if len(set(submitted)) != len(submitted):
        raise ValueError('Позиция закупки указана дважды')
    if not set(submitted) <= {int(item['id']) for item in lot['items']}:
        raise ValueError('Позиция из другой закупки')
    if data.price_date and data.valid_until and data.valid_until < data.price_date:
        raise ValueError('Срок действия КП раньше даты цены')
    # Claim the attachment first: a double click or a second administrator cannot create two quotes.
    now = utcnow()
    stale = (datetime.now(UTC).replace(microsecond=0) - STALE_CLAIM).isoformat()
    with service.db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM inbox_quotes WHERE attachment_id=? AND quote_id IS NULL AND created_at<?', (aid, stale))
        if conn.execute('SELECT 1 FROM inbox_quotes WHERE attachment_id=?', (aid,)).fetchone():
            raise ConflictError('КП из этого вложения уже сохранено или сохраняется')
        conn.execute('INSERT INTO inbox_quotes VALUES (?,?,?,?,NULL,?,?)',
                     (aid, attachment['message_id'], lot['id'], data.supplier_id, trusted_actor(), now))
    try:
        existing = service.db.one('SELECT * FROM source_documents WHERE sha256=?', (attachment['sha256'],))
        if existing:
            if existing['project_id'] not in (None, lot['project_id']) or existing['supplier_id'] not in (None, data.supplier_id):
                raise ConflictError('Этот файл уже относится к другому объекту или поставщику')
            document = existing
        else:
            document = service.register_source_document(filename=attachment['filename'], content=FilePayload(path),
                                                        document_type='commercial_offer', project_id=lot['project_id'],
                                                        supplier_id=data.supplier_id)
        quote = service.add_quote(lot['id'], QuoteCreate(
            supplier_id=data.supplier_id, currency=lot['currency'], vat_included=data.vat_included,
            delivery_cost=data.delivery_cost, lead_days=data.lead_days, payment_terms=data.payment_terms,
            valid_until=data.valid_until, price_date=data.price_date, delivery_basis=data.delivery_basis,
            source_filename=attachment['filename'][:255], source_document_id=document['id'],
            items=[QuoteItemCreate(lot_item_id=i.lot_item_id, unit_price=i.unit_price, note=i.note) for i in data.items]))
    except BaseException:
        with service.db.connection() as conn:
            conn.execute('DELETE FROM inbox_quotes WHERE attachment_id=? AND quote_id IS NULL', (aid,))
        raise
    with service.db.connection() as conn:
        conn.execute('UPDATE inbox_quotes SET quote_id=? WHERE attachment_id=?', (quote['id'], aid))
        service.db.audit('inbox_quote_saved', 'inbox_attachment', aid,
                         details={'quote_id': quote['id'], 'lot_id': lot['id'], 'supplier_id': data.supplier_id,
                                  'source_document_id': document['id'], 'items': len(data.items)}, conn=conn)
    return {'quote_id': quote['id'], 'lot_id': lot['id'], 'lot_title': lot['title'], 'items': len(data.items)}
