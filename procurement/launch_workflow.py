"""Supplier lifecycle, reversible imports, reviewed sheets and RFQ attachments."""
import hashlib
import json
import mimetypes
import os
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from pathlib import Path

from .db import utcnow
from .identity import trusted_actor
from .imports import HEADER_ALIASES, supplier_dedup_key
from .models import SupplierCreate, LotCreate
from .region_routing import resolve_cluster
from .sandbox import payload_sha256, message_fingerprint
from .service import ConflictError, NotFoundError
from .table_ingest import contacts, mapped, suggested_mapping, quantity, delivery_date
from .upload_io import MAX_BATCH, FilePayload, payload_sha256 as file_sha256
from .stream_mail import send_streamed

SCHEMA = '''
CREATE TABLE IF NOT EXISTS launch_previews (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL, data_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'preview', result_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supplier_import_changes (
 preview_id TEXT NOT NULL REFERENCES launch_previews(id), supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
 before_json TEXT, after_json TEXT NOT NULL, PRIMARY KEY(preview_id,supplier_id));
CREATE TABLE IF NOT EXISTS lot_attachments (
 lot_id INTEGER NOT NULL REFERENCES lots(id), document_id INTEGER NOT NULL REFERENCES source_documents(id),
 PRIMARY KEY(lot_id,document_id));
CREATE TABLE IF NOT EXISTS outbox_attachments (
 message_id INTEGER NOT NULL REFERENCES outbox_messages(id), document_id INTEGER NOT NULL REFERENCES source_documents(id),
 filename TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 PRIMARY KEY(message_id,document_id));
CREATE TABLE IF NOT EXISTS mail_deliveries (
 message_id INTEGER PRIMARY KEY REFERENCES outbox_messages(id), payload_sha256 TEXT NOT NULL,
 status TEXT NOT NULL, message_key TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL);
'''
SUPPLIER_FIELDS = set(SupplierCreate.model_fields)
SUPPLIER_ALIASES = {k: v - {'бин', 'иин', 'огрн'} if k == 'tax_id' else v
                    for k, v in HEADER_ALIASES.items()}
ITEM_ALIASES = {
 'name': {'наименование','позиция','материал','товар','name'},
 'quantity': {'количество','кол-во','количество, шт','объем','объём','quantity'},
 'unit': {'ед. изм.','ед.изм.','единица','единица измерения','ед','unit'},
 'specification': {'характеристики','спецификация','описание','specification'},
 'delivery_date': {'срок','срок поставки','дата поставки','delivery_date'},
}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def supplier_values(data):
    values = data.model_dump(mode='json')
    values['cluster'] = resolve_cluster(data.region, data.cluster)
    values['categories_json'] = encode(values.pop('categories'))
    values['verified'] = int(values['verified'])
    return values


class LaunchWorkflow:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def supplier(self, supplier_id):
        row = self.service.get_supplier(supplier_id)
        raw = self.db.one('SELECT * FROM suppliers WHERE id=?', (supplier_id,))
        return {**row, 'revision': payload_sha256(raw)}

    def edit_supplier(self, supplier_id, data, revision):
        values = supplier_values(SupplierCreate(**contacts(data)))
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            before = conn.execute('SELECT * FROM suppliers WHERE id=?', (supplier_id,)).fetchone()
            if not before:
                raise NotFoundError('Поставщик не найден')
            if not before['active'] or payload_sha256(dict(before)) != revision:
                raise ConflictError('Карточка изменилась или удалена; обновите страницу')
            if before['cluster'] != values['cluster'] and self._supplier_used(conn,supplier_id):
                raise ConflictError('Поставщик уже используется; смена кластера запрещена')
            self._update(conn, supplier_id, values)
            after = dict(conn.execute('SELECT * FROM suppliers WHERE id=?', (supplier_id,)).fetchone())
            self.db.audit('supplier_edited', 'supplier', supplier_id,
                          details={'before': dict(before), 'after': after}, conn=conn)
        return self.supplier(supplier_id)

    @staticmethod
    def _update(conn, supplier_id, values):
        conn.execute('UPDATE suppliers SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                     (*values.values(), supplier_id))

    @staticmethod
    def _supplier_used(conn,supplier_id):
        return any(conn.execute('SELECT 1 FROM ' + table + ' WHERE supplier_id=? LIMIT 1',(supplier_id,)).fetchone()
                   for table in ('outbox_messages','quotes','purchase_history','price_history_entries','source_documents'))

    def supplier_state(self, supplier_id, active, revision, confirmed):
        if confirmed is not True:
            raise ValueError('Требуется явное подтверждение')
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM suppliers WHERE id=?', (supplier_id,)).fetchone()
            if not row:
                raise NotFoundError('Поставщик не найден')
            if payload_sha256(dict(row)) != revision:
                raise ConflictError('Карточка изменилась; обновите страницу')
            if bool(row['active']) == active:
                return self.supplier(supplier_id)
            conn.execute('UPDATE suppliers SET active=? WHERE id=?', (int(active), supplier_id))
            self.db.audit('supplier_restored' if active else 'supplier_soft_deleted', 'supplier', supplier_id,
                          details={'before': dict(row)}, conn=conn)
        return self.supplier(supplier_id)

    def save_preview(self, kind, data):
        pid = uuid.uuid4().hex
        with self.db.connection() as conn:
            conn.execute('INSERT INTO launch_previews(id,kind,actor,data_json,created_at) VALUES (?,?,?,?,?)',
                         (pid, kind, trusted_actor(), encode(data), utcnow()))
            self.db.audit('previewed', kind, pid, details={'rows': len(data.get('rows', []))}, conn=conn)
        return {'preview_id': pid, **data}

    def preview(self, conn, pid, kind):
        row = conn.execute('SELECT * FROM launch_previews WHERE id=? AND kind=? AND actor=?',
                           (pid, kind, trusted_actor())).fetchone()
        if not row:
            raise NotFoundError('Предпросмотр не найден или принадлежит другому пользователю')
        return row, json.loads(row['data_json'])

    def supplier_preview(self, table, mapping=None, region='Воронежская область'):
        mapping = suggested_mapping(table['headers'], SUPPLIER_ALIASES) if mapping is None else mapping
        if 'name' not in mapping:
            return {**table, 'rows': table['rows'][:20], 'mapping': mapping, 'needs_mapping': True}
        rows, seen = [], set()
        for source in table['rows']:
            try:
                values = mapped(source, mapping, table['headers'], SUPPLIER_FIELDS)
                values.setdefault('region', region)
                if 'categories' in values:
                    values['categories'] = [x.strip() for x in str(values['categories']).replace(';', ',').split(',') if x.strip()]
                if 'verified' in values:
                    values['verified'] = str(values['verified']).casefold() in {'да','true','1'}
                if 'rating' in values and not values['rating']:
                    values['rating'] = 3
                data = SupplierCreate(**contacts(values))
                prepared = supplier_values(data)
                key = supplier_dedup_key(data.tax_id, data.name, data.email, data.phone)
                matches = self.db.all('SELECT * FROM suppliers WHERE tax_id=?', (data.tax_id,)) if data.tax_id else [
                    r for r in self.db.all('SELECT * FROM suppliers')
                    if supplier_dedup_key(r['tax_id'], r['name'], r['email'], r['phone']) == key]
                if key in seen:
                    rows.append({'row': source['row'], 'action': 'skipped', 'reason': 'Дубль в файле', 'data': data.model_dump(mode='json')})
                    continue
                seen.add(key)
                if len(matches) > 1:
                    raise ValueError('Неоднозначный дубль: требуется ручная проверка')
                existing = matches[0] if matches else None
                if existing:
                    # Unmapped columns are not instructions to erase existing contact fields.
                    prior = {k: existing[k] for k in SUPPLIER_FIELDS - {'categories'}}
                    prior['categories'] = json.loads(existing['categories_json'])
                    supplied = {k:v for k,v in values.items() if k in mapping}
                    data = SupplierCreate(**contacts({**prior, **supplied}))
                    prepared = supplier_values(data)
                action = 'skipped' if existing and not existing['active'] else ('updated' if existing else 'added')
                if existing and all(existing[k] == v for k, v in prepared.items()):
                    action = 'skipped'
                rows.append({'row': source['row'], 'action': action, 'data': data.model_dump(mode='json'),
                             'before': existing, 'values': prepared,
                             'reason': 'Удалённая карточка не восстанавливается импортом' if existing and not existing['active'] else ''})
            except (ValueError, TypeError) as exc:
                rows.append({'row': source['row'], 'action': 'error', 'reason': str(exc)[:300]})
        return self.save_preview('supplier_import', {'headers': table['headers'], 'sheets': table['sheets'],
             'sheet': table['sheet'], 'mapping': mapping, 'region': region, 'rows': rows,
             'report': {k: sum(r['action'] == k for r in rows) for k in ('added','updated','skipped','error')}})

    def apply_import(self, pid, confirmed):
        if confirmed is not True:
            raise ValueError('Требуется подтверждение импорта')
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row, data = self.preview(conn, pid, 'supplier_import')
            if row['status'] == 'applied':
                return json.loads(row['result_json'])
            if row['status'] != 'preview':
                raise ConflictError('Импорт уже отменён')
            for entry in data['rows']:
                if entry['action'] not in {'added','updated'}:
                    continue
                before, values = entry['before'], entry['values']
                if before:
                    current = conn.execute('SELECT * FROM suppliers WHERE id=?', (before['id'],)).fetchone()
                    if not current or dict(current) != before:
                        raise ConflictError('Поставщик изменился после предпросмотра')
                    sid = before['id']
                    if before['cluster'] != values['cluster'] and self._supplier_used(conn,sid):
                        raise ConflictError('Поставщик уже используется; смена кластера импортом запрещена')
                    self._update(conn, sid, values)
                else:
                    # Recheck identity under the write lock; no race-created duplicates.
                    existing = conn.execute('SELECT * FROM suppliers').fetchall()
                    key = supplier_dedup_key(values['tax_id'], values['name'], values['email'], values['phone'])
                    if any(supplier_dedup_key(r['tax_id'],r['name'],r['email'],r['phone']) == key for r in existing):
                        raise ConflictError('Дубль появился после предпросмотра')
                    inserted = {**values, 'source': 'supplier_import:' + pid, 'created_at': utcnow()}
                    sid = conn.execute('INSERT INTO suppliers(' + ','.join(inserted) + ') VALUES (' + ','.join('?' for _ in inserted) + ')', tuple(inserted.values())).lastrowid
                after = dict(conn.execute('SELECT * FROM suppliers WHERE id=?', (sid,)).fetchone())
                conn.execute('INSERT INTO supplier_import_changes VALUES (?,?,?,?)',
                             (pid, sid, encode(before) if before else None, encode(after)))
            result = {'preview_id': pid, 'status': 'applied', **data['report']}
            conn.execute('UPDATE launch_previews SET status=?,result_json=? WHERE id=?', ('applied', encode(result), pid))
            self.db.audit('supplier_import_applied', 'supplier_import', pid, details=result, conn=conn)
        return result

    def rollback_import(self, pid, confirmed):
        if confirmed is not True:
            raise ValueError('Требуется подтверждение отката')
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row, _ = self.preview(conn, pid, 'supplier_import')
            if row['status'] == 'rolled_back':
                return {'status': 'rolled_back', 'changed': 0}
            if row['status'] != 'applied':
                raise ConflictError('Импорт не применён')
            changes = conn.execute('SELECT * FROM supplier_import_changes WHERE preview_id=?', (pid,)).fetchall()
            for change in changes:
                current = conn.execute('SELECT * FROM suppliers WHERE id=?', (change['supplier_id'],)).fetchone()
                if not current or encode(dict(current)) != change['after_json']:
                    raise ConflictError('После импорта карточка изменена; автоматический откат запрещён')
                if self._supplier_used(conn,change['supplier_id']):
                    raise ConflictError('Импортированный поставщик уже используется; откат запрещён')
                if change['before_json']:
                    before = json.loads(change['before_json'])
                    before.pop('id')
                    self._update(conn, change['supplier_id'], before)
                else:
                    conn.execute('UPDATE suppliers SET active=0 WHERE id=?', (change['supplier_id'],))
            conn.execute("UPDATE launch_previews SET status='rolled_back' WHERE id=?", (pid,))
            self.db.audit('supplier_import_rolled_back','supplier_import',pid,details={'changed':len(changes)},conn=conn)
        return {'status':'rolled_back','changed':len(changes)}

    def sheet_preview(self, table, mapping=None, source_document=None):
        mapping = suggested_mapping(table['headers'], ITEM_ALIASES) if mapping is None else mapping
        if not {'name','quantity','unit'} <= set(mapping):
            return {**table, 'rows': table['rows'][:20], 'mapping': mapping, 'needs_mapping': True}
        rows, errors = [], []
        for source in table['rows']:
            values = {}
            try:
                values = mapped(source, mapping, table['headers'], set(ITEM_ALIASES))
                values['quantity'] = quantity(values['quantity'])
                values['delivery_date'] = delivery_date(values.get('delivery_date', ''))
                rows.append({'row': source['row'], **values})
            except ValueError as exc:
                errors.append({'row':source['row'],'reason':str(exc)})
                rows.append({'row':source['row'],**values,'error':str(exc)})
        return self.save_preview('lot_sheet', {'headers':table['headers'],'sheets':table['sheets'],
              'sheet':table['sheet'],'mapping':mapping,'rows':rows,'errors':errors,
              'source_document_id':source_document['id'] if source_document else None,
              'project_id':source_document['project_id'] if source_document else None})

    def create_sheet_lot(self, pid, data, confirmed):
        if confirmed is not True:
            raise ValueError('Подтвердите исправленные позиции')
        lot = LotCreate(**data)
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            preview, preview_data = self.preview(conn, pid, 'lot_sheet')
            requested_hash = payload_sha256(lot.model_dump(mode='json'))
            if preview['status'] == 'applied':
                result = json.loads(preview['result_json'])
                if result['payload_sha256'] != requested_hash:
                    raise ConflictError('Этот предпросмотр уже использован с другими исправленными данными')
                return self.service.get_lot(result['lot_id'])
            source_id = preview_data.get('source_document_id')
            if source_id:
                if preview_data.get('project_id') != lot.project_id:
                    raise ValueError('Исходный лист принадлежит другому проекту')
                lot.attachment_document_ids = sorted(set(lot.attachment_document_ids + [source_id]))
            project = conn.execute('SELECT * FROM projects WHERE id=?',(lot.project_id,)).fetchone()
            if not project:
                raise NotFoundError('Проект не найден')
            cluster = resolve_cluster(lot.region,lot.cluster or project['cluster'])
            if lot.section_id and not conn.execute('SELECT 1 FROM project_sections WHERE id=? AND project_id=?',(lot.section_id,lot.project_id)).fetchone():
                raise ValueError('Раздел принадлежит другому проекту')
            self._documents(conn, lot.project_id, lot.attachment_document_ids)
            sid = conn.execute('''INSERT INTO lots(project_id,section_id,title,region,cluster,delivery_address,
               response_deadline,desired_delivery_date,currency,rfq_requirements_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
               (lot.project_id,lot.section_id,lot.title,lot.region,cluster,lot.delivery_address,str(lot.response_deadline),
                str(lot.desired_delivery_date) if lot.desired_delivery_date else None,lot.currency,
                encode(lot.rfq_requirements.model_dump(mode='json') if lot.rfq_requirements else {}),utcnow())).lastrowid
            for item in lot.items:
                if source_id and not item.source_document_id:
                    item.source_document_id = source_id
                    item.source_reference = 'Лист ' + preview_data['sheet']
                if item.source_document_id:
                    self._documents(conn,lot.project_id,[item.source_document_id])
                conn.execute('''INSERT INTO lot_items(lot_id,name,quantity,unit,specification,source_document_id,
                    source_page,source_reference,delivery_date) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (sid,item.name,str(item.quantity),item.unit,item.specification,item.source_document_id,
                     item.source_page,item.source_reference,str(item.delivery_date) if item.delivery_date else None))
            conn.executemany('INSERT INTO lot_attachments VALUES (?,?)',[(sid,d) for d in set(lot.attachment_document_ids)])
            conn.execute("UPDATE launch_previews SET status='applied',result_json=? WHERE id=?",(encode({'lot_id':sid,'payload_sha256':requested_hash}),pid))
            self.db.audit('lot_created_from_sheet','lot',sid,details={'preview_id':pid,'items':len(lot.items)},conn=conn)
        return self.service.get_lot(sid)

    def _documents(self, conn, project_id, ids):
        rows = []
        for did in sorted(set(ids)):
            doc = conn.execute('SELECT * FROM source_documents WHERE id=? AND project_id=?',(did,project_id)).fetchone()
            if not doc:
                raise ValueError('Вложение не принадлежит проекту заявки')
            self.document_file(dict(doc))
            rows.append(dict(doc))
        if sum(r['size_bytes'] for r in rows) > MAX_BATCH:
            raise ValueError('Общий размер вложений превышает 100 МБ')
        return rows

    def attach_lot(self, lot_id, ids):
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            lot = conn.execute('SELECT * FROM lots WHERE id=?',(lot_id,)).fetchone()
            if not lot:
                raise NotFoundError('Заявка не найдена')
            self._documents(conn,lot['project_id'],ids)
            if conn.execute('SELECT 1 FROM campaigns WHERE lot_id=?',(lot_id,)).fetchone():
                raise ConflictError('Вложения уже зафиксированы в КП; создайте новую заявку')
            conn.execute('DELETE FROM lot_attachments WHERE lot_id=?',(lot_id,))
            conn.executemany('INSERT INTO lot_attachments VALUES (?,?)',[(lot_id,d) for d in sorted(set(ids))])
            self.db.audit('lot_attachments_updated','lot',lot_id,details={'document_ids':ids},conn=conn)
        return self.service.get_lot(lot_id)

    def document_file(self, doc):
        root = Path(self.db.path).resolve().parent / 'uploads'
        path = Path(doc['storage_path']).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != doc['size_bytes']:
            raise ConflictError('Вложение недоступно или изменено')
        content = FilePayload(path)
        if file_sha256(content) != doc['sha256']:
            raise ConflictError('SHA вложения не совпадает')
        return content

    def send(self, message_id, confirmed):
        if confirmed is not True:
            raise ValueError('Подтвердите отправку адресату с указанными вложениями')
        host, sender = os.getenv('PROCUREMENT_SMTP_HOST',''), os.getenv('PROCUREMENT_SMTP_FROM','')
        if not host or not sender:
            raise ConflictError('Отправка не настроена: требуется SMTP-сервер и подтверждённый адрес отправителя')
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            message = self.service._outbox_context(conn,message_id)
            fingerprint = message_fingerprint(message)
            previous = conn.execute('SELECT * FROM mail_deliveries WHERE message_id=?',(message_id,)).fetchone()
            if previous:
                if previous['status'] == 'sent' and previous['payload_sha256'] == fingerprint:
                    return {'status':'sent','message_id':message_id,'duplicate':True}
                raise ConflictError('Отправка уже выполнялась; проверьте статус у почтового провайдера, повтор запрещён')
            approval = conn.execute('SELECT * FROM outbox_approvals WHERE message_id=?',(message_id,)).fetchone()
            if (message['channel'] != 'email' or message['status'] != 'approved' or not approval
                    or approval['payload_sha256'] != fingerprint
                    or approval['approved_by'] != message['approved_by']
                    or approval['approved_at'] != message['approved_at']):
                raise ConflictError('Необходим неизменённый черновик с подтверждением сотрудника')
            contacts({'email':message['recipient']})
            if any(c in sender + message['subject'] for c in '\r\n'):
                raise ValueError('Недопустимые заголовки письма')
            contacts({'email':sender})
            email = EmailMessage()
            email['From'],email['To'],email['Subject'] = sender,message['recipient'],message['subject']
            key = uuid.uuid4().hex
            email['Message-ID'] = '<' + key + '@' + sender.split('@')[1] + '>'
            email.set_content(message['body'])
            attachments=[]
            for attachment in message.get('attachments',[]):
                doc = conn.execute('SELECT * FROM source_documents WHERE id=?',(attachment['document_id'],)).fetchone()
                if not doc or doc['sha256'] != attachment['sha256']:
                    raise ConflictError('Вложение изменилось после согласования')
                content = self.document_file(dict(doc))
                attachments.append((attachment['filename'],content,attachment['sha256']))
            conn.execute('INSERT INTO mail_deliveries VALUES (?,?,?,?,?,?)',(message_id,fingerprint,'sending',key,trusted_actor(),utcnow()))
            self.db.audit('mail_send_started','outbox_message',message_id,details={'attachment_count':len(message.get('attachments',[]))},conn=conn)
        try:
            port = int(os.getenv('PROCUREMENT_SMTP_PORT','587'))
            mode = os.getenv('PROCUREMENT_SMTP_TLS','starttls')
            if mode == 'none' and host not in {'127.0.0.1','localhost','::1'}:
                raise ValueError('Незащищённый SMTP разрешён только на loopback')
            smtp_type = smtplib.SMTP_SSL if mode == 'ssl' else smtplib.SMTP
            with smtp_type(host,port,timeout=30) as smtp:
                if mode == 'starttls':
                    smtp.starttls(context=ssl.create_default_context())
                elif mode not in {'ssl','none'}:
                    raise ValueError('Недопустимый SMTP TLS режим')
                user = os.getenv('PROCUREMENT_SMTP_USER','')
                if user:
                    password_path = Path(os.environ['PROCUREMENT_SMTP_PASSWORD_FILE'])
                    if password_path.stat().st_mode & 0o077:
                        raise ValueError('SMTP secret должен иметь права 0400/0600')
                    smtp.login(user,password_path.read_text().strip())
                send_streamed(smtp,email,attachments)
        except Exception:
            with self.db.connection() as conn:
                conn.execute("UPDATE mail_deliveries SET status='unknown',updated_at=? WHERE message_id=?",(utcnow(),message_id))
                self.db.audit('mail_send_unconfirmed','outbox_message',message_id,details={'retry':'blocked'},conn=conn)
            raise ConflictError('Доставка не подтверждена. Автоматический повтор запрещён; проверьте почтовый сервер') from None
        with self.db.connection() as conn:
            conn.execute("UPDATE mail_deliveries SET status='sent',updated_at=? WHERE message_id=?",(utcnow(),message_id))
            conn.execute("UPDATE outbox_messages SET status='sent' WHERE id=?",(message_id,))
            self.db.audit('mail_sent','outbox_message',message_id,details={'attachment_count':len(message.get('attachments',[]))},conn=conn)
        return {'status':'sent','message_id':message_id,'attachment_count':len(message.get('attachments',[])),'accepted_by_smtp':True}
