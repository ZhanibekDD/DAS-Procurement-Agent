"""Private, read-only mailbox intake. Mail content never grants supplier trust.

Run in a separate, resource-limited worker: python -m procurement.incoming_mail.
No SMTP, STORE, APPEND, EXPUNGE or remote URLs are used. Attachments stay outside
the shared document registry until an authenticated administrator reviews them.
"""
from __future__ import annotations

import hashlib
import errno
import imaplib
import json
import os
import re
import signal
import shutil
import socket
import ssl
import threading
import tempfile
import time
import uuid
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .catalog import ALIASES, Catalog
from .db import utcnow
from .identity import authenticated_actor, trusted_actor
from .inbox_quotes import listing_extras, store_refs
from .sent_mail import PreAuthTransportError, connect_imap
from .service import ConflictError, NotFoundError
from .table_ingest import loose_mapping, mapped, read_docx_table, read_table, safe_upload, suggested_mapping
from .upload_io import FilePayload, payload_sha256

MAX_MAIL = 20 * 1024 * 1024
MAX_PARTS = 20
MAX_ROWS = 2000
MAX_CELL = 8000
MAX_DRAFT_BYTES = 2 * 1024 * 1024
MAX_PRIVATE_BYTES = 1024 * 1024 * 1024
UID_BATCH = 25
NETWORK_SECONDS = 120
CHUNK = 64 * 1024
RECOGNITION_ERROR = 'Не удалось полностью распознать вложение. Скачайте оригинал и проверьте данные вручную.'
SUPPORTED = {'.pdf', '.xlsx', '.csv', '.docx'}
UNSUPPORTED = 'Неподдерживаемое вложение пропущено; доступны PDF, XLSX, DOCX и CSV.'
EXCERPT = 1500
# Messages received before a reader improvement are re-read once from the mailbox (read-only).
UPGRADE_VERSION = 1
UPGRADE_DAYS = 45
UPGRADE_BATCH = 5

SCHEMA = '''
CREATE TABLE IF NOT EXISTS inbox_state (
 account TEXT PRIMARY KEY, validity INTEGER NOT NULL, last_uid INTEGER NOT NULL,
 next_uid INTEGER NOT NULL, checked_at TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS inbox_messages (
 id TEXT PRIMARY KEY, account TEXT NOT NULL, validity INTEGER NOT NULL, uid INTEGER NOT NULL,
 sha256 TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL DEFAULT '',
 message_id TEXT NOT NULL DEFAULT '', mail_date TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,
 status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', duplicate_of TEXT,
 UNIQUE(account,validity,uid));
CREATE TABLE IF NOT EXISTS inbox_texts (
 message_id TEXT PRIMARY KEY REFERENCES inbox_messages(id), excerpt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inbox_upgrades (
 message_id TEXT NOT NULL REFERENCES inbox_messages(id), version INTEGER NOT NULL, result TEXT NOT NULL,
 at TEXT NOT NULL, PRIMARY KEY(message_id, version));
CREATE INDEX IF NOT EXISTS inbox_digest ON inbox_messages(account,sha256);
CREATE TABLE IF NOT EXISTS inbox_attachments (
 id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES inbox_messages(id),
 part INTEGER NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 draft_json TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', UNIQUE(message_id,part));
CREATE TABLE IF NOT EXISTS inbox_reviews (
 attachment_id TEXT NOT NULL REFERENCES inbox_attachments(id), actor TEXT NOT NULL,
 preview_id TEXT NOT NULL REFERENCES launch_previews(id), PRIMARY KEY(attachment_id,actor));
'''


def account_key():
    identity = [os.getenv('PROCUREMENT_IMAP_HOST', '').lower(), os.getenv('PROCUREMENT_IMAP_PORT', '993'),
                (os.getenv('PROCUREMENT_IMAP_USER') or os.getenv('PROCUREMENT_SMTP_USER', '')).lower(), 'INBOX']
    if not identity[0] or not identity[2]:
        raise ValueError('Почтовый ящик для входящих КП не настроен')
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def clean_header(value, limit=500):
    return ' '.join(str(value or '').replace('\x00', '').split())[:limit]


class DraftOnly:
    def save_preview(self, kind, data):
        return {**data, 'kind': kind}


def extract_draft(path, filename):
    """Preserve unknown/invalid rows for human correction, never silently drop."""
    payload = FilePayload(path)
    safe_upload(payload, filename, SUPPORTED)
    if Path(filename).suffix.lower() == '.pdf':
        from .price_ocr import extract_price_document as extract_document
        result = extract_document(payload, filename)
        # Reuse the same OCR/price interpretation without writing shared previews.
        catalog = object.__new__(Catalog)
        catalog.launch = DraftOnly()
        draft = catalog.extracted_price_preview({'id': 0, 'filename': filename}, result)
    else:
        table = read_docx_table(payload, filename) if Path(filename).suffix.lower() == '.docx' else read_table(payload, filename)
        # CSV readers can return long strings even when row counts are small.
        # Validate all columns, including unmapped ones, before building a draft.
        for value in [*table['headers'], *(cell for row in table['rows'] for cell in row['cells'])]:
            if isinstance(value, str) and len(value) > MAX_CELL:
                raise ValueError('Ячейка прайса превышает 8000 символов')
        if len(table['sheets']) != 1:
            return {'rows': [], 'errors': ['В книге несколько листов. Загрузите оригинал через импорт прайса и выберите нужный лист.']}
        mapping = loose_mapping(table['headers'], suggested_mapping(table['headers'], ALIASES))
        # «Цена, руб. с НДС» states VAT for every row of the column; the reviewer still confirms it.
        price_header = str(table['headers'][mapping['unit_price']]).casefold() if 'unit_price' in mapping else ''
        column_vat = 'без НДС' if 'без ндс' in price_header else 'с НДС' if 'ндс' in price_header else ''
        rows = []
        for row in table['rows']:
            warning = ''
            try:
                values = mapped(row, mapping, table['headers'], set(ALIASES))
            except ValueError:
                values = {}
                warning = 'В строке формула или неоднозначные колонки; заполните по оригиналу.'
            if column_vat and not values.get('vat'):
                values['vat'] = column_vat
            rows.append({**{key: str(values.get(key, '')) for key in ALIASES},
                         'source_row': row['row'], 'review_warning': warning})
        draft = {'rows': rows, 'errors': [], 'source_filename': filename}
        if not {'item_name', 'unit_price'} <= set(mapping):
            draft['errors'].append('Не определены колонки товара или цены. Заполните строки по оригиналу.')
    if len(draft['rows']) > MAX_ROWS:
        return {'rows': [], 'errors': ['Более 2000 строк. Используйте ручной импорт оригинального прайса.']}
    # The product is rouble-only. Never silently convert a declared foreign price.
    for row in draft['rows']:
        if not row.get('currency'):
            row['currency'] = 'RUB'
            row['review_warning'] = (row.get('review_warning', '') + ' Валюта не указана: подтвердите, что цена в рублях.').strip()
    draft['requires_review'] = True
    from .price_ocr import RECOGNITION_VERSION
    draft['recognition_version'] = RECOGNITION_VERSION
    bounded_draft(draft)
    return draft


def bounded_draft(draft):
    def check(value):
        if isinstance(value, str) and len(value) > MAX_CELL:
            raise ValueError('Значение прайса превышает 8000 символов')
        if isinstance(value, dict):
            for child in value.values():check(child)
        elif isinstance(value, list):
            for child in value:check(child)
    check(draft)
    encoded = json.dumps(draft, ensure_ascii=False)
    if len(encoded.encode('utf-8')) > MAX_DRAFT_BYTES:
        raise ValueError('Предпросмотр прайса превышает 2 МБ')
    return encoded


_QUOTE_LINE = re.compile(
    r'^\s*(?:-{2,}\s*(?:original message|исходное сообщение|пересылаемое сообщение|forwarded message)'
    r'|(?:from|от|sent|отправлено)\s*:.{0,200}$|.{0,120}(?:wrote|пишет|написал\(а\)|написала|написал)\s*:\s*$)',
    re.IGNORECASE)
_QUOTE_MARK = '\x00quote\x00'


class _HtmlText(HTMLParser):
    """Visible text of an HTML mail; quoted history (blockquote, *quote* blocks) becomes a cut mark."""
    BREAKS = {'br', 'p', 'div', 'tr', 'li', 'table', 'h1', 'h2', 'h3', 'h4'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        attributes = {k: (v or '').casefold() for k, v in attrs}
        if tag in ('style', 'script', 'head', 'title'):
            self.hidden += 1
        elif tag == 'blockquote' or 'quote' in attributes.get('class', '') or attributes.get('id', '') in (
                'divrplyfwdmsg', 'appendonsend'):
            self.out.append('\n' + _QUOTE_MARK + '\n')
        elif tag in self.BREAKS:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in ('style', 'script', 'head', 'title'):
            self.hidden = max(0, self.hidden - 1)
        elif tag in self.BREAKS:
            self.out.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.out.append(data)


def reply_excerpt(mail, limit=EXCERPT) -> str:
    """The supplier's own words: the text before the quoted history, bounded. Never raises."""
    try:
        part = mail.get_body(preferencelist=('plain', 'html'))
        if part is None:
            return ''
        text = part.get_content()
        if part.get_content_type() == 'text/html':
            parser = _HtmlText()
            parser.feed(text[:400000])
            text = ''.join(parser.out)
    except Exception:
        return ''
    lines, own = [], True
    for line in str(text).replace('\r', '').replace('\xa0', ' ').split('\n'):
        if _QUOTE_MARK in line or line.lstrip().startswith('>') or _QUOTE_LINE.match(line):
            own = False
        if own:
            lines.append(line.rstrip())
    excerpt = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
    if not excerpt:  # a forward without a comment: show the start of the forwarded text instead
        excerpt = re.sub(r'\n{3,}', '\n\n', str(text).replace(_QUOTE_MARK, '')).strip()
    return excerpt[:limit]


def store_excerpt(conn, mid: str, mail) -> None:
    conn.execute('INSERT OR REPLACE INTO inbox_texts VALUES (?,?)', (mid, reply_excerpt(mail)))


class Inbox:
    def __init__(self, service, launch):
        self.service, self.launch, self.db = service, launch, service.db
        self.root = Path(self.db.path).resolve().parent / 'incoming-private'

    def storage_budget(self, additional=0):
        used = self.db.one('SELECT COALESCE(SUM(length(CAST(draft_json AS BLOB))),0) AS size FROM inbox_attachments')['size']
        for n, path in enumerate(self.root.rglob('*')):
            if n >= 10000 or path.is_symlink():
                raise RuntimeError('inbox_storage_limit')
            if path.is_file():
                used += path.stat().st_size
        if used + additional > MAX_PRIVATE_BYTES or shutil.disk_usage(self.root).free < 1024**3:
            raise RuntimeError('inbox_storage_limit')

    def path(self, attachment):
        aid, suffix = attachment['id'], Path(attachment['filename']).suffix.lower()
        if not re.fullmatch('[a-f0-9]{32}', aid) or suffix not in SUPPORTED:
            raise ConflictError('Недопустимый исходник')
        path = self.root / (aid + suffix)
        if path.is_symlink() or not path.is_file() or payload_sha256(FilePayload(path)) != attachment['sha256']:
            raise ConflictError('Исходное вложение недоступно или изменилось')
        return path

    def attachment(self, aid):
        row = self.db.one('SELECT * FROM inbox_attachments WHERE id=?', (aid,))
        if not row:
            raise NotFoundError('Вложение не найдено')
        return row

    def ingest(self, account, validity, uid, path):
        """Crash-safe identity and immutable source; completed UID is never repeated."""
        existing = self.db.one('SELECT id FROM inbox_messages WHERE account=? AND validity=? AND uid=?', (account, validity, uid))
        if existing:
            return existing['id']
        digest = payload_sha256(FilePayload(path))
        duplicate = self.db.one("SELECT id FROM inbox_messages WHERE account=? AND sha256=? AND status!='error' LIMIT 1", (account, digest))
        mid = uuid.uuid4().hex
        if duplicate:
            self._store_message(mid, account, validity, uid, digest, 'duplicate', duplicate_of=duplicate['id'])
            return mid
        if path.stat().st_size > MAX_MAIL:
            raise ValueError('Письмо превышает 20 МБ')
        with path.open('rb') as stream:
            mail = BytesParser(policy=policy.default).parse(stream)
        parts = list(mail.walk())
        if len(parts) > 100 or any(part.defects for part in parts):
            self._store_message(mid, account, validity, uid, digest, 'error', error='Повреждённая или слишком сложная структура письма')
            return mid
        attachments = [p for p in parts if not p.is_multipart() and p.get_filename()]
        if len(attachments) > MAX_PARTS:
            self._store_message(mid, account, validity, uid, digest, 'error', error='В письме больше 20 вложений; требуется ручной импорт')
            return mid
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.attachments-', dir=self.root) as stage:
            return self._save_attachments(mid, account, validity, uid, digest, mail, attachments, Path(stage))

    def _save_attachments(self, mid, account, validity, uid, digest, mail, attachments, stage, known_parts=None):
        """known_parts is set when an already stored message is re-read: those parts stay as they are."""
        saved, warnings, published = [], [], []
        draft_bytes = 0
        for number, part in enumerate(attachments, 1):
            filename = part.get_filename()
            if known_parts is not None and number in known_parts:
                continue
            if part.get_content_maintype() == 'image' and Path(filename).suffix.lower() not in SUPPORTED:
                continue  # logos and signatures; a price photo is sent as PDF or entered by hand
            if Path(filename).suffix.lower() not in SUPPORTED:
                warnings.append(UNSUPPORTED)
                continue
            # Stable destinations recover a crash between file publication and
            # DB commit without writing new orphan files on each retry.
            aid = hashlib.sha256(json.dumps([account, validity, uid, number]).encode()).hexdigest()[:32]
            payload = part.get_payload(decode=True)
            try:
                safe_upload(payload or b'', filename, SUPPORTED)
            except (ValueError, TypeError):
                warnings.append('Опасное имя или повреждённое вложение заблокировано.')
                continue
            name = aid + Path(filename).suffix.lower()
            final = self.root / name
            sha = hashlib.sha256(payload).hexdigest()
            recovered = final.exists() or final.is_symlink()
            if recovered:
                # A crash may have published this immutable file before commit.
                # It is already charged by storage_budget; reuse it in place,
                # reserving only the new draft rather than another payload.
                if final.is_symlink() or not final.is_file() or payload_sha256(FilePayload(final)) != sha:
                    raise ConflictError('Конфликт неизменяемого исходника')
                attachment_path = final
                self.storage_budget(draft_bytes)
            else:
                attachment_path = stage / name
                self.storage_budget(len(payload) + draft_bytes)
                with attachment_path.open('xb') as output:
                    os.chmod(attachment_path, 0o600)
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
            try:
                draft = extract_draft(attachment_path, filename)
                encoded = bounded_draft(draft)
                error = ''
            except Exception:
                # Parser exceptions can contain arbitrary private mail text.
                encoded, error = bounded_draft({'rows': [], 'errors': [RECOGNITION_ERROR]}), RECOGNITION_ERROR
            draft_bytes += len(encoded.encode('utf-8'))
            self.storage_budget(draft_bytes)
            saved.append((aid, mid, number, filename, sha, len(payload), encoded, error))
        try:
            with self.db.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if known_parts is None:
                    self._store_message(mid, account, validity, uid, digest, 'review' if saved else 'no_prices',
                        error=' '.join(sorted(set(warnings))), mail=mail, conn=conn)
                else:
                    has_parts = bool(saved) or bool(known_parts)
                    conn.execute("UPDATE inbox_messages SET status=CASE WHEN ? THEN 'review' ELSE status END, error=? WHERE id=?",
                                 (has_parts, ' '.join(sorted(set(warnings))), mid))
                    store_excerpt(conn, mid, mail)
                    conn.execute('INSERT OR REPLACE INTO inbox_upgrades VALUES (?,?,?,?)',
                                 (mid, UPGRADE_VERSION, 'attachments:%d' % len(saved), utcnow()))
                conn.executemany('INSERT INTO inbox_attachments VALUES (?,?,?,?,?,?,?,?)', saved)
                for aid, _, _, filename, sha, *_ in saved:
                    name = aid + Path(filename).suffix.lower()
                    final = self.root / name
                    if final.exists() or final.is_symlink():
                        if final.is_symlink() or not final.is_file() or payload_sha256(FilePayload(final)) != sha:
                            raise ConflictError('Конфликт неизменяемого исходника')
                        continue
                    try:
                        os.link(stage / name, final)  # exclusive, no overwritten originals
                        published.append((aid, final))
                    except FileExistsError:
                        if final.is_symlink() or payload_sha256(FilePayload(final)) != sha:
                            raise ConflictError('Конфликт неизменяемого исходника')
                if hasattr(os, 'O_DIRECTORY'):
                    fd = os.open(self.root, os.O_DIRECTORY)
                    try:os.fsync(fd)
                    finally:os.close(fd)
                self.db.audit('incoming_mail_received' if known_parts is None else 'incoming_mail_reread', 'inbox_message', mid,
                              details={'attachments': len(saved), 'requires_review': True}, conn=conn)
        except BaseException:
            for aid, final in published:
                # If commit outcome is uncertain / DB unavailable, keep bytes;
                # stable paths allow later reconciliation without duplication.
                try:
                    if not self.db.one('SELECT 1 FROM inbox_attachments WHERE id=?', (aid,)):
                        final.unlink()
                except Exception:
                    pass
            raise
        return mid

    def pending_rereads(self, account, validity, limit=UPGRADE_BATCH):
        """Recent stored messages not yet re-read by the current reader version."""
        since = (datetime.now(timezone.utc) - timedelta(days=UPGRADE_DAYS)).isoformat()
        return self.db.all('''SELECT m.id, m.uid, m.sha256 FROM inbox_messages m
            WHERE m.account=? AND m.validity=? AND m.received_at>=? AND m.status IN ('no_prices','review')
            AND NOT EXISTS(SELECT 1 FROM inbox_upgrades u WHERE u.message_id=m.id AND u.version=?)
            ORDER BY m.rowid LIMIT ?''', (account, validity, since, UPGRADE_VERSION, limit))

    def reread(self, row, account, validity, path):
        """Add what the current reader understands (DOCX tables, the reply text) to an already stored message.
        Stored attachments, drafts, reviews and quotes are never touched; the mailbox copy must be identical."""
        def done(result):
            with self.db.connection() as conn:
                conn.execute('INSERT OR REPLACE INTO inbox_upgrades VALUES (?,?,?,?)', (row['id'], UPGRADE_VERSION, result, utcnow()))
        if payload_sha256(FilePayload(path)) != row['sha256']:
            return done('source_changed')
        with path.open('rb') as stream:
            mail = BytesParser(policy=policy.default).parse(stream)
        parts = list(mail.walk())
        if len(parts) > 100 or any(part.defects for part in parts):
            return done('unparsed')
        attachments = [p for p in parts if not p.is_multipart() and p.get_filename()]
        known = {r['part'] for r in self.db.all('SELECT part FROM inbox_attachments WHERE message_id=?', (row['id'],))}
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.attachments-', dir=self.root) as stage:
            self._save_attachments(row['id'], account, validity, row['uid'], row['sha256'], mail,
                                   attachments[:MAX_PARTS], Path(stage), known_parts=known)

    def _store_message(self, mid, account, validity, uid, digest, status, error='', duplicate_of=None, mail=None, conn=None):
        if conn is None:
            with self.db.connection() as connection:
                return self._store_message(mid, account, validity, uid, digest, status, error, duplicate_of, mail, connection)
        headers = [clean_header(mail.get(key)) if mail is not None else '' for key in ('From', 'Subject', 'Message-ID', 'Date')]
        conn.execute('INSERT INTO inbox_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (mid, account, validity, uid, digest, *headers, utcnow(), status, error, duplicate_of))
        if mail is not None:
            store_refs(conn, mid, mail)
            store_excerpt(conn, mid, mail)

    def listing(self, before=None):
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError('Некорректная страница')
        rows = self.db.all('''SELECT m.rowid AS sequence,m.id,m.sender,m.subject,m.mail_date,m.received_at,m.status,m.error,
            COALESCE(t.excerpt,'') AS excerpt FROM inbox_messages m LEFT JOIN inbox_texts t ON t.message_id=m.id
            WHERE (? IS NULL OR m.rowid<?) ORDER BY m.rowid DESC LIMIT 51''', (before, before))
        for row in rows[:50]:
            row['attachments'] = self.db.all('''SELECT a.id,a.filename,a.size_bytes,a.error,
                EXISTS(SELECT 1 FROM inbox_reviews r JOIN launch_previews p ON p.id=r.preview_id
                       WHERE r.attachment_id=a.id AND p.status='applied') AS applied
                FROM inbox_attachments a WHERE a.message_id=? ORDER BY a.part''', (row['id'],))
        listing_extras(self.db, rows[:50])
        state = self.db.all('SELECT checked_at,error,next_uid,last_uid FROM inbox_state')
        return {'messages': rows[:50], 'next_before': rows[49]['sequence'] if len(rows) > 50 else None, 'connections': state}

    def detail(self, aid):
        row = self.attachment(aid)
        self.path(row)
        reviews=self.db.all('''SELECT r.actor,p.status,p.data_json FROM inbox_reviews r
            JOIN launch_previews p ON p.id=r.preview_id WHERE r.attachment_id=?''',(aid,))
        draft=json.loads(row['draft_json'])
        own=next((r for r in reviews if r['actor']==trusted_actor() and r['status']=='preview'),None)
        if own:
            draft['rows']=json.loads(own['data_json'])['rows']
        return {**draft,'id': aid, 'filename': row['filename'], 'sha256': row['sha256'],
                'reviewed':bool(reviews),'applied':any(r['status']=='applied' for r in reviews)}

    def recognize(self, aid):
        """Upgrade an unreviewed draft, not the immutable source or saved prices."""
        from .price_ocr import RECOGNITION_VERSION
        row=self.attachment(aid)
        draft=json.loads(row['draft_json'])
        path=self.path(row)
        if draft.get('recognition_version',0)>=RECOGNITION_VERSION:return self.detail(aid)
        if self.db.one('SELECT 1 FROM inbox_reviews WHERE attachment_id=?',(aid,)):
            raise ConflictError('Это КП уже проверялось. Сохранённые строки не заменены распознаванием.')
        draft=extract_draft(path,row['filename'])
        encoded=bounded_draft(draft)
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM inbox_reviews WHERE attachment_id=?',(aid,)).fetchone():
                raise ConflictError('КП уже проверено другим сотрудником; распознавание не заменило проверенные строки')
            # Concurrent requests may have completed the same bounded OCR.
            current=conn.execute('SELECT draft_json FROM inbox_attachments WHERE id=?',(aid,)).fetchone()
            if json.loads(current['draft_json']).get('recognition_version',0)<RECOGNITION_VERSION:
                conn.execute('UPDATE inbox_attachments SET draft_json=? WHERE id=?',(encoded,aid))
                self.db.audit('incoming_attachment_recognized','inbox_attachment',aid,
                    details={'version':RECOGNITION_VERSION,'rows':len(draft['rows'])},conn=conn)
        return self.detail(aid)

    def validate_currency(self,aid):
        """Legacy reviewed rows stay immutable; refresh only source evidence."""
        attachment=self.attachment(aid)
        source=json.loads(attachment['draft_json'])
        if Path(attachment['filename']).suffix.lower()=='.pdf' and 'document_currencies' not in source:
            fresh=extract_draft(self.path(attachment),attachment['filename'])
            if 'document_currencies' not in fresh:
                raise ValueError('Не удалось проверить валюту исходного КП. Исходник доступен; цены не сохранены')
            with self.db.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current=conn.execute('SELECT draft_json FROM inbox_attachments WHERE id=?',(aid,)).fetchone()
                source=json.loads(current['draft_json'])
                source['document_currencies']=fresh['document_currencies']
                conn.execute('UPDATE inbox_attachments SET draft_json=? WHERE id=?',(bounded_draft(source),aid))
                self.db.audit('incoming_currency_evidence_checked','inbox_attachment',aid,
                    details={'source_sha256':attachment['sha256'],'reviewed_rows_changed':False},conn=conn)
        if (any(c!='RUB' for c in source.get('document_currencies',[]))
                or any(r.get('currency') not in ('','RUB') for r in source['rows'])):
            raise ValueError('В исходнике указана другая валюта. Автоматической конвертации в рубли нет')

    def prepare(self, aid, rows, confirmed_source):
        if confirmed_source is not True:
            raise ValueError('Подтвердите реквизиты поставщика по оригиналу; отправитель письма не является подтверждением')
        if not 1 <= len(rows) <= MAX_ROWS or any(set(row) - set(ALIASES) for row in rows):
            raise ValueError('Некорректные строки прайса')
        if any(not isinstance(value, str) or len(value) > 8000 for row in rows for value in row.values()):
            raise ValueError('Некорректные значения прайса')
        if any(row.get('currency') != 'RUB' for row in rows):
            raise ValueError('Поддерживаются цены в рублях; автоматической конвертации валют нет')
        attachment = self.attachment(aid)
        self.validate_currency(aid)
        applied = self.db.one('''SELECT 1 FROM inbox_reviews r JOIN launch_previews p ON p.id=r.preview_id
            WHERE r.attachment_id=? AND p.status='applied' ''', (aid,))
        if applied:
            raise ConflictError('Это вложение уже импортировано; история цен не перезаписывается')
        existing = self.db.one('''SELECT 1 FROM source_documents d JOIN supplier_catalog_prices p ON p.source_document_id=d.id
            WHERE d.sha256=? LIMIT 1''', (attachment['sha256'],))
        if existing:
            raise ConflictError('Этот исходник уже имеет сохранённые цены; повторный импорт не выполняется')
        path = self.path(attachment)
        table = {'headers': list(ALIASES), 'sheet': 'Проверено из письма',
                 'rows': [{'row': n, 'cells': [row.get(k, '') for k in ALIASES]} for n, row in enumerate(rows, 1)]}
        catalog = Catalog(self.service, DraftOnly())
        checked = catalog.price_preview({'id': 0}, table, {key: n for n, key in enumerate(ALIASES)})
        if checked['errors']:
            return {'errors': checked['errors'], 'rows': [], 'requires_correction': True}
        # Human verification explicitly publishes ONLY this supported attachment.
        doc = self.service.register_source_document(filename=attachment['filename'], content=FilePayload(path), document_type='price_list', _price_import=True)
        checked.update(document_id=doc['id'], inbox_attachment_id=aid)
        checked.pop('kind', None)
        preview = self.launch.save_preview('price_catalog', checked)
        with self.db.connection() as conn:
            previous = conn.execute('SELECT preview_id FROM inbox_reviews WHERE attachment_id=? AND actor=?', (aid, trusted_actor())).fetchone()
            if previous:
                old = conn.execute('SELECT status FROM launch_previews WHERE id=?', (previous['preview_id'],)).fetchone()
                if old['status'] == 'applied':
                    raise ConflictError('Это вложение уже импортировано; история цен не перезаписывается')
            conn.execute('INSERT OR REPLACE INTO inbox_reviews VALUES (?,?,?)', (aid, trusted_actor(), preview['preview_id']))
            self.db.audit('incoming_supplier_verified', 'inbox_attachment', aid, details={'preview_id': preview['preview_id'], 'rows': len(rows)}, conn=conn)
        return preview

    def clean_abandoned_staging(self):
        """Called only under the worker flock. Never targets committed UUID files."""
        if not self.root.exists():return
        for path in self.root.iterdir():
            if path.name.startswith(('.intake-', '.attachments-')) and path.is_dir() and not path.is_symlink():
                if path.resolve().parent != self.root.resolve():raise RuntimeError('Unsafe staging path')
                shutil.rmtree(path)


@contextmanager
def network_deadline(client):
    expired = threading.Event()
    def expire():
        expired.set()
        try:
            client.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer = threading.Timer(NETWORK_SECONDS, expire)
    timer.daemon = True
    timer.start()
    try:
        yield
        if expired.is_set():
            raise TimeoutError('Проверка почты превысила время ожидания')
    finally:
        timer.cancel()
        timer.join()


def numeric_response(client, name):
    _, values = client.response(name)
    if not values or len(values) != 1 or not re.fullmatch(rb'[1-9][0-9]{0,9}', values[0] or b''):
        raise ValueError('Почтовый сервер не подтвердил состояние папки')
    value = int(values[0])
    if value > 2**32 - 1:
        raise ValueError('Некорректный UID почты')
    return value


def message_sizes(client, low, high):
    # Discover the expected set independently. An OK FETCH is not proof that
    # every existing UID in the numeric window was returned (expunges/races or
    # truncated responses). A mismatch retries the same cursor next minute.
    typ, values = client.uid('SEARCH', None, f'UID {low}:{high}')
    if (typ != 'OK' or not isinstance(values, list) or len(values) != 1
            or not isinstance(values[0], bytes) or len(values[0]) > UID_BATCH * 11):
        raise OSError('Не удалось подтвердить список писем')
    ids = values[0].split()
    if (len(ids) > UID_BATCH or any(not re.fullmatch(rb'[1-9][0-9]{0,9}', uid) for uid in ids)):
        raise ValueError('Некорректный список UID')
    expected = {int(uid) for uid in ids}
    if len(expected) != len(ids) or any(not low <= uid <= high for uid in expected):
        raise ValueError('Несовпадение диапазона UID')
    if not expected:
        return []
    typ, values = client.uid('FETCH', ','.join(map(str, sorted(expected))), '(UID RFC822.SIZE)')
    if typ != 'OK' or not isinstance(values, list) or len(values) != len(expected):
        raise OSError('Не удалось получить список писем')
    result = {}
    for value in values:
        if not isinstance(value, bytes) or len(value) > 200:
            raise ValueError('Некорректный ответ почты')
        uid = re.search(rb'\bUID ([0-9]+)\b', value)
        size = re.search(rb'\bRFC822.SIZE ([0-9]+)\b', value)
        if not uid or not size or int(uid[1]) in result or int(uid[1]) not in expected:
            raise ValueError('Некорректный список писем')
        result[int(uid[1])] = int(size[1])
    if set(result) != expected:
        raise OSError('Получены не все письма; проверка будет повторена')
    return sorted(result.items())


def download(client, uid, size, path):
    if not 0 < size <= MAX_MAIL:
        raise ValueError('Письмо пусто или больше 20 МБ; требуется ручной импорт')
    with path.open('xb') as output:
        os.chmod(path, 0o600)
        for offset in range(0, size, CHUNK):
            count = min(CHUNK, size - offset)
            # imaplib normally allocates the server-declared literal before its
            # caller sees it. Enforce the requested chunk bound before allocation.
            original_read = getattr(client, 'read', None)
            if original_read is not None:
                def bounded_read(length):
                    if length > count:
                        raise ValueError('Почтовый сервер превысил размер блока')
                    return original_read(length)
                client.read = bounded_read
            try:
                typ, values = client.uid('FETCH', str(uid), f'(UID BODY.PEEK[]<{offset}.{count}>)')
            finally:
                if original_read is not None:
                    client.read = original_read
            literals = [v for v in values if isinstance(v, tuple)]
            if typ != 'OK' or len(literals) != 1:
                raise OSError('Письмо не получено целиком')
            header, block = literals[0]
            match = re.search(rb'\bUID ([0-9]+)\b', header)
            origin = re.search(rb'BODY\[\]<([0-9]+)>', header)
            if not match or int(match[1]) != uid or not origin or int(origin[1]) != offset or len(block) != count:
                raise ValueError('Несовпадение исходного письма')
            output.write(block)
        output.flush()
        os.fsync(output.fileno())


def poll(inbox, connect=connect_imap):
    account = account_key()
    client = None
    staging = None
    try:
        client = connect()
        inbox.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        inbox.storage_budget()
        staging = tempfile.TemporaryDirectory(prefix='.intake-', dir=inbox.root)
        fetched = []
        with network_deadline(client):
            if client.select('INBOX', readonly=True)[0] != 'OK':
                raise OSError('Входящие недоступны')
            validity, next_uid = numeric_response(client, 'UIDVALIDITY'), numeric_response(client, 'UIDNEXT')
            state = inbox.db.one('SELECT * FROM inbox_state WHERE account=?', (account,))
            last = state['last_uid'] if state and state['validity'] == validity else 0
            high = min(last + UID_BATCH, next_uid - 1)
            if high > last:
                for uid, size in message_sizes(client, last + 1, high):
                    if inbox.db.one('SELECT 1 FROM inbox_messages WHERE account=? AND validity=? AND uid=?', (account, validity, uid)):
                        continue
                    if not 0 < size <= MAX_MAIL:
                        inbox._store_message(uuid.uuid4().hex, account, validity, uid, '', 'error', error='Письмо пусто или больше 20 МБ; требуется ручной импорт')
                        continue
                    path = Path(staging.name) / (uuid.uuid4().hex + '.eml')
                    download(client, uid, size, path)
                    fetched.append((uid, path))
        client.logout()
        client = None
        token = authenticated_actor.set('system:incoming-mail')
        try:
            for uid, path in fetched:
                try:
                    inbox.ingest(account, validity, uid, path)
                except (ValueError, RecursionError, UnicodeError):
                    inbox._store_message(uuid.uuid4().hex, account, validity, uid, payload_sha256(FilePayload(path)),
                        'error', error='Структура письма не распознана; требуется ручная проверка оригинала в почте')
        finally:
            authenticated_actor.reset(token)
        with inbox.db.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO inbox_state VALUES (?,?,?,?,?,?)',
                         (account, validity, max(last, high), next_uid, utcnow(), ''))
        return {'status': 'ok', 'received': len(fetched), 'caught_up': high >= next_uid - 1}
    except Exception as exc:
        # No exception strings, mailbox headers, authentication or response dumps.
        with inbox.db.connection() as conn:
            conn.execute("INSERT INTO inbox_state VALUES (?,0,0,0,?,?) ON CONFLICT(account) DO UPDATE SET checked_at=excluded.checked_at,error=excluded.error",
                (account, utcnow(), 'Не удалось проверить почту. Полученные письма сохранены; следующая попытка через минуту.'))
        transport = (isinstance(exc, PreAuthTransportError) or client is not None and (
                     isinstance(exc, (TimeoutError, ConnectionError, imaplib.IMAP4.abort, ssl.SSLEOFError))
                     or isinstance(exc, OSError) and exc.errno in (
                         errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED,
                         errno.EPIPE, errno.ENETUNREACH, errno.EHOSTUNREACH)))
        return {'status': 'error', 'received': 0, 'retryable': transport,
                'error_type': type(exc).__name__}
    finally:
        if staging is not None:
            staging.cleanup()
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                pass


def reread_pending(inbox, connect=connect_imap):
    """Re-read up to UPGRADE_BATCH stored messages from the mailbox (read-only, same message bytes only)."""
    account = account_key()
    client = staging = None
    try:
        state = inbox.db.one('SELECT validity FROM inbox_state WHERE account=?', (account,))
        rows = inbox.pending_rereads(account, state['validity']) if state else []
        if not rows:
            return {'status': 'ok', 'reread': 0}
        client = connect()
        inbox.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        inbox.storage_budget()
        staging = tempfile.TemporaryDirectory(prefix='.intake-', dir=inbox.root)
        fetched = []
        with network_deadline(client):
            if client.select('INBOX', readonly=True)[0] != 'OK':
                raise OSError('Входящие недоступны')
            if numeric_response(client, 'UIDVALIDITY') != state['validity']:
                return {'status': 'ok', 'reread': 0}
            for row in rows:
                sizes = dict(message_sizes(client, row['uid'], row['uid']))
                if row['uid'] not in sizes or not 0 < sizes[row['uid']] <= MAX_MAIL:
                    fetched.append((row, None))
                    continue
                path = Path(staging.name) / (uuid.uuid4().hex + '.eml')
                download(client, row['uid'], sizes[row['uid']], path)
                fetched.append((row, path))
        client.logout()
        client = None
        token = authenticated_actor.set('system:incoming-mail')
        try:
            for row, path in fetched:
                if path is None:
                    with inbox.db.connection() as conn:
                        conn.execute('INSERT OR REPLACE INTO inbox_upgrades VALUES (?,?,?,?)',
                                     (row['id'], UPGRADE_VERSION, 'gone', utcnow()))
                    continue
                try:
                    inbox.reread(row, account, state['validity'], path)
                except (ValueError, RecursionError, UnicodeError):
                    with inbox.db.connection() as conn:
                        conn.execute('INSERT OR REPLACE INTO inbox_upgrades VALUES (?,?,?,?)',
                                     (row['id'], UPGRADE_VERSION, 'unparsed', utcnow()))
        finally:
            authenticated_actor.reset(token)
        return {'status': 'ok', 'reread': len(fetched)}
    except Exception as exc:
        # A failed re-read changes nothing and is retried with the next poll.
        return {'status': 'error', 'reread': 0, 'error_type': type(exc).__name__}
    finally:
        if staging is not None:
            staging.cleanup()
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                pass


def poll_bounded(inbox, connect=connect_imap, pause=time.sleep):
    """Retry one transport failure only; never retry auth, parsing or quota errors."""
    result=poll(inbox,connect)
    if result.get('retryable') and not pause(3):
        result=poll(inbox,connect)
    return result


def main():
    import argparse
    import fcntl
    from .db import Database
    from .service import ProcurementService
    from .launch_workflow import LaunchWorkflow
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    db = Database(os.environ['PROCUREMENT_DB_PATH'])
    db.initialize()
    service = ProcurementService(db)
    inbox = Inbox(service, LaunchWorkflow(service))
    # One reader across processes/containers sharing this data volume. Lock is
    # kernel-owned, released on crash; no stale lease or credentials in the lock.
    with (Path(db.path).parent / 'incoming.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('{"status":"already_running"}', flush=True)
            return
        inbox.clean_abandoned_staging()
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.is_set():
            result = poll_bounded(inbox,pause=stop.wait)
            print(json.dumps(result), flush=True)
            if result.get('status') == 'ok' and result.get('caught_up'):
                reread = reread_pending(inbox)
                if reread.get('reread') or reread.get('status') != 'ok':
                    print(json.dumps(reread), flush=True)
            if args.once:
                return
            stop.wait(60)


if __name__ == '__main__':
    main()
