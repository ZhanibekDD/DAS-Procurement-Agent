"""Audited SMTP acceptance, immutable MIME and independent Sent-copy ledger."""
import json
import os
import smtplib
import ssl
import uuid
import base64
import re
from datetime import datetime, UTC, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from .db import utcnow
from .identity import trusted_actor
from .sandbox import message_fingerprint
from .service import ConflictError
from .table_ingest import contacts
from .stream_mail import spool_message, submit_spool, SubmissionUncertain
from .sent_mail import archive_sent, file_digest, secret, ArchiveUncertain

SCHEMA = '''
CREATE TABLE IF NOT EXISTS mail_receipts (
 message_id INTEGER PRIMARY KEY REFERENCES outbox_messages(id),
 rfc_message_id TEXT NOT NULL, sender TEXT NOT NULL, recipients_json TEXT NOT NULL,
 smtp_host TEXT NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL,
 started_at TEXT NOT NULL, accepted_at TEXT, smtp_code INTEGER, smtp_reply TEXT,
 accepted_recipients_json TEXT NOT NULL DEFAULT '[]', attachments_json TEXT NOT NULL,
 spool_path TEXT, spool_sha256 TEXT, error TEXT,
 sent_copy_status TEXT NOT NULL DEFAULT 'pending', sent_copy_uid TEXT,
 sent_copy_started_at TEXT, sent_copy_lease TEXT,
 delivery_status TEXT NOT NULL DEFAULT 'unconfirmed');
CREATE TABLE IF NOT EXISTS mail_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL REFERENCES outbox_messages(id),
 created_at TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, details_json TEXT NOT NULL);
'''

COPY_WARNING = 'SMTP принял письмо, но копия в “Отправленных” не сохранена'
COPY_LEASE_SECONDS = 600
QUEUE_LEASE_SECONDS = 600


class SupersededQueue(Exception):
    """Another worker owns this pre-SMTP attempt; never change its state."""


def queued_lease_expired(row):
    if not row or row['status'] != 'queued':
        return False
    try:
        started = datetime.fromisoformat(row['started_at'])
        return started.tzinfo is not None and datetime.now(UTC) - started >= timedelta(seconds=QUEUE_LEASE_SECONDS)
    except (TypeError, ValueError):
        return False


def copy_lease_expired(row):
    if row['sent_copy_status'] != 'saving':
        return False
    try:
        started = datetime.fromisoformat(row['sent_copy_started_at'])
        if started.tzinfo is None:
            return True
        return datetime.now(UTC) - started >= timedelta(seconds=COPY_LEASE_SECONDS)
    except (TypeError, ValueError):
        # Old interrupted records have no timestamp. Reconcile, never APPEND.
        return True


def smtp_failure_receipt(exc, user='', password=None):
    """Keep bounded protocol evidence, never AUTH replies/credential echoes."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return exc.smtp_code, 'Ответ авторизации скрыт'
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        if len(exc.recipients) != 1:
            return None, None
        code, reply = next(iter(exc.recipients.values()))
    elif isinstance(exc, smtplib.SMTPResponseException):
        code, reply = exc.smtp_code, exc.smtp_error
    else:
        return None, None
    reply = reply[:4096].decode('utf-8', 'replace') if isinstance(reply, bytes) else str(reply)[:4096]
    if password:
        sensitive = [password, base64.b64encode(password.encode()).decode(),
                     base64.b64encode(('\x00' + user + '\x00' + password).encode()).decode()]
        for value in sorted(sensitive, key=len, reverse=True):
            reply = reply.replace(value, '[скрыто]')
    reply = re.sub(r'[\x00-\x1f\x7f]', ' ', reply)
    return code, reply[:1000]


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def event(db, conn, mid, name, **details):
    conn.execute('INSERT INTO mail_events(message_id,created_at,actor,event,details_json) VALUES (?,?,?,?,?)',
                 (mid, utcnow(), trusted_actor(), name, encode(details)))
    db.audit(name, 'outbox_message', mid, details=details, conn=conn)


def journal(db, mid):
    delivery = db.one('SELECT status,updated_at FROM mail_deliveries WHERE message_id=?', (mid,))
    if not delivery:
        return None
    row = db.one('SELECT * FROM mail_receipts WHERE message_id=?', (mid,))
    if not row:
        # Legacy SMTP acceptance is known, but exact reply/MIME was not retained.
        return {**delivery, 'legacy': True, 'sent_copy_status': 'unavailable',
                'delivery_status': 'unconfirmed', 'retry_allowed': False,
                'warning': COPY_WARNING if delivery['status'] == 'sent' else 'Исход старой отправки требует проверки',
                'events': []}
    row.pop('spool_path')  # private disk paths never exposed to the browser
    row.pop('message_id')
    expired = copy_lease_expired(row)
    row.pop('sent_copy_lease')
    for key in ('recipients', 'accepted_recipients', 'attachments'):
        row[key] = json.loads(row.pop(key + '_json'))
    row['retry_allowed'] = row['status'] == 'failed' or queued_lease_expired(row)
    row['copy_retry_allowed'] = row['status'] == 'sent' and (row['sent_copy_status'] in {'pending','failed','unknown'} or expired)
    row['copy_reconcile_only'] = row['sent_copy_status'] == 'unknown' or expired
    row['warning'] = COPY_WARNING if row['status'] == 'sent' and row['sent_copy_status'] != 'saved' else None
    row['events'] = []
    for item in db.all('SELECT created_at,actor,event,details_json FROM mail_events WHERE message_id=? ORDER BY id', (mid,)):
        details = json.loads(item.pop('details_json'))
        row['events'].append({**item, 'details': details})
    return row


def result(db, mid, duplicate=False):
    info = journal(db, mid)
    return {'status': info['status'], 'message_id': mid, 'duplicate': duplicate,
            'accepted_by_smtp': info['status'] == 'sent', 'delivery': info,
            'warning': info.get('warning')}


def send(workflow, mid, confirmed):
    if confirmed is not True:
        raise ValueError('Подтвердите отправку адресату с указанными вложениями')
    host, sender = os.getenv('PROCUREMENT_SMTP_HOST',''), os.getenv('PROCUREMENT_SMTP_FROM','')
    if not host or not sender:
        raise ConflictError('Отправка не настроена: требуется SMTP-сервер и подтверждённый адрес отправителя')
    service, db = workflow.service, workflow.db
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        message = service._outbox_context(conn, mid)
        from .procurement_flow import validate_message, ProcurementFlow
        validate_message(conn, message)
        fingerprint = message_fingerprint(message)
        previous = conn.execute('SELECT * FROM mail_deliveries WHERE message_id=?', (mid,)).fetchone()
        if previous:
            if previous['status'] == 'sent' and previous['payload_sha256'] == fingerprint:
                return result(db, mid, duplicate=True)
            recover_queued = previous['status'] == 'queued' and queued_lease_expired(
                conn.execute('SELECT status,started_at FROM mail_receipts WHERE message_id=?', (mid,)).fetchone())
            if (previous['status'] != 'failed' and not recover_queued) or previous['payload_sha256'] != fingerprint:
                raise ConflictError('Исход отправки не подтверждён либо отправка выполняется; повтор запрещён до проверки сервера')
        flow=ProcurementFlow(service)
        from .identity import trusted_role
        if flow.approval_required(conn,message['lot_id'],trusted_role()) and not flow.admin_approval_valid(conn,message):
            raise ConflictError('Требуется согласование текущего правила закупки администратором')
        approval = conn.execute('SELECT * FROM outbox_approvals WHERE message_id=?', (mid,)).fetchone()
        if (message['channel'] != 'email' or message['status'] not in {'approved','failed','queued'} or not approval
                or approval['payload_sha256'] != fingerprint or approval['approved_by'] != message['approved_by']
                or approval['approved_at'] != message['approved_at']):
            raise ConflictError('Необходим неизменённый черновик с подтверждением сотрудника')
        contacts({'email': message['recipient']})
        contacts({'email': sender})
        if any(c in sender + message['subject'] for c in '\r\n'):
            raise ValueError('Недопустимые заголовки письма')
        attachments = []
        for attachment in message.get('attachments', []):
            doc = conn.execute('SELECT * FROM source_documents WHERE id=?', (attachment['document_id'],)).fetchone()
            if not doc or doc['sha256'] != attachment['sha256']:
                raise ConflictError('Вложение изменилось после согласования')
            attachments.append((attachment['filename'], workflow.document_file(dict(doc)), attachment['sha256']))
        key = previous['message_key'] if previous else uuid.uuid4().hex
        rfc_id = '<' + key + '@' + sender.split('@')[1] + '>'
        old = conn.execute('SELECT * FROM mail_receipts WHERE message_id=?', (mid,)).fetchone()
        if old and (old['sender'] != sender or old['smtp_host'] != host):
            raise ConflictError('Почтовая конфигурация изменилась; повтор требует проверки')
        now = utcnow()
        if previous:
            conn.execute("UPDATE mail_deliveries SET status='queued',updated_at=? WHERE message_id=?", (now,mid))
            conn.execute("UPDATE mail_receipts SET status='queued',attempt=attempt+1,error=NULL,started_at=? WHERE message_id=?", (now,mid))
        else:
            conn.execute('INSERT INTO mail_deliveries VALUES (?,?,?,?,?,?)', (mid,fingerprint,'queued',key,trusted_actor(),now))
            conn.execute('''INSERT INTO mail_receipts(message_id,rfc_message_id,sender,recipients_json,smtp_host,status,attempt,
                started_at,attachments_json) VALUES (?,?,?,?,?,'queued',1,?,?)''',
                (mid,rfc_id,sender,encode([message['recipient']]),host,now,encode(message.get('attachments',[]))))
        conn.execute("UPDATE outbox_messages SET status='queued' WHERE id=?", (mid,))
        attempt = conn.execute('SELECT attempt FROM mail_receipts WHERE message_id=?', (mid,)).fetchone()['attempt']
        event(db,conn,mid,'mail_queued',message_id=rfc_id,recipients=[message['recipient']],attachment_count=len(attachments))
    smtp = None
    user, credential = '', None
    try:
        current = db.one('SELECT * FROM mail_receipts WHERE message_id=?', (mid,))
        if current['status'] != 'queued' or current['attempt'] != attempt:
            raise SupersededQueue()
        path = Path(current['spool_path']) if current['spool_path'] else Path(db.path).resolve().parent / 'mail-spool' / (key + '-' + str(attempt) + '.eml')
        if current['spool_path']:
            if file_digest(path) != current['spool_sha256']:
                raise ValueError('SHA письма изменился; отправка заблокирована')
        else:
            path.parent.mkdir(mode=0o700, exist_ok=True)
            email = EmailMessage()
            email['From'],email['To'],email['Subject'] = sender,message['recipient'],message['subject']
            email['Message-ID'],email['Date'] = rfc_id,formatdate(localtime=False, usegmt=True)
            email.set_content(message['body'])
            spool_message(path,email,attachments)
            with db.connection() as conn:
                updated = conn.execute("UPDATE mail_receipts SET spool_path=?,spool_sha256=? WHERE message_id=? AND status='queued' AND attempt=? AND spool_path IS NULL",
                                       (str(path),file_digest(path),mid,attempt)).rowcount
                if not updated:
                    raise SupersededQueue()
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            updated = conn.execute("UPDATE mail_receipts SET status='sending' WHERE message_id=? AND status='queued' AND attempt=?",(mid,attempt)).rowcount
            if not updated:
                raise SupersededQueue()
            conn.execute("UPDATE mail_deliveries SET status='sending',updated_at=? WHERE message_id=? AND status='queued'",(utcnow(),mid))
            conn.execute("UPDATE outbox_messages SET status='sending' WHERE id=?",(mid,))
            event(db,conn,mid,'mail_send_started',message_id=rfc_id)
        port = int(os.getenv('PROCUREMENT_SMTP_PORT','587'))
        mode = os.getenv('PROCUREMENT_SMTP_TLS','starttls')
        if mode == 'none' and host not in {'127.0.0.1','localhost','::1'}:
            raise ValueError('Незащищённый SMTP разрешён только на loopback')
        if mode not in {'ssl','starttls','none'}:
            raise ValueError('Недопустимый SMTP TLS режим')
        smtp_type = smtplib.SMTP_SSL if mode == 'ssl' else smtplib.SMTP
        smtp = smtp_type(host,port,timeout=30)
        if mode == 'starttls':
            smtp.starttls(context=ssl.create_default_context())
        user = os.getenv('PROCUREMENT_SMTP_USER','')
        if user:
            credential = secret('PROCUREMENT_SMTP')
            smtp.login(user,credential)
        receipt = submit_spool(smtp,path,sender,message['recipient'])
    except Exception as exc:
        if isinstance(exc, SupersededQueue) or db.one('SELECT attempt FROM mail_receipts WHERE message_id=?', (mid,))['attempt'] != attempt:
            raise ConflictError('Эту очередь уже обрабатывает другой работник; повтор SMTP не выполнялся') from None
        uncertain = isinstance(exc, SubmissionUncertain)
        status = 'unknown' if uncertain else 'failed'
        reason = 'Письмо не принято почтовым сервером; доступен безопасный повтор.'
        if uncertain:
            reason = 'Почтовый сервер не подтвердил приём после передачи письма. Повтор заблокирован: возможен дубликат.'
        elif isinstance(exc, smtplib.SMTPAuthenticationError):
            reason = f'SMTP отклонил авторизацию (код {exc.smtp_code}). Проверьте почтовые настройки; письмо не отправлено.'
        elif isinstance(exc, smtplib.SMTPRecipientsRefused):
            reason = 'SMTP отклонил получателя; письмо не отправлено. Проверьте адрес и повторите.'
        elif isinstance(exc, smtplib.SMTPResponseException):
            reason = f'SMTP отклонил письмо (код {exc.smtp_code}); письмо не отправлено. Доступен безопасный повтор.'
        elif isinstance(exc, (OSError, smtplib.SMTPServerDisconnected)):
            reason = 'SMTP недоступен или время соединения истекло; письмо не отправлено. Доступен безопасный повтор.'
        code, reply = smtp_failure_receipt(exc,user,credential)
        with db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # The earlier attempt read is only a fast check. A recovering worker
            # may take over before this transaction starts, so fence every state
            # change on the receipt's current attempt and pre-SMTP/sending state.
            updated = conn.execute('''UPDATE mail_receipts SET status=?,error=?,smtp_code=?,smtp_reply=?
                WHERE message_id=? AND attempt=? AND status IN ('queued','sending')''',
                (status,reason,code,reply,mid,attempt)).rowcount
            if not updated:
                raise ConflictError('Эту очередь уже обрабатывает другой работник; повтор SMTP не выполнялся') from None
            if conn.execute("UPDATE mail_deliveries SET status=?,updated_at=? WHERE message_id=? AND status IN ('queued','sending')",
                            (status,utcnow(),mid)).rowcount != 1:
                raise ConflictError('Состояние отправки изменилось; повтор SMTP не выполнялся')
            if conn.execute("UPDATE outbox_messages SET status='failed' WHERE id=? AND status IN ('queued','sending')",
                            (mid,)).rowcount != 1:
                raise ConflictError('Состояние отправки изменилось; повтор SMTP не выполнялся')
            event(db,conn,mid,'mail_send_unconfirmed' if uncertain else 'mail_send_failed',
                  error=reason,error_type=type(exc).__name__,smtp_code=code,smtp_reply=reply,retry=not uncertain)
        raise ConflictError(reason) from None
    finally:
        if smtp:
            # A QUIT failure after DATA 250 must not discard acceptance or enable retry.
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass
    with db.connection() as conn:
        now = utcnow()
        conn.execute("UPDATE mail_deliveries SET status='sent',updated_at=? WHERE message_id=?",(now,mid))
        conn.execute('''UPDATE mail_receipts SET status='sent',accepted_at=?,smtp_code=?,smtp_reply=?,
            accepted_recipients_json=? WHERE message_id=?''',
            (now,receipt['smtp_code'],receipt['smtp_reply'],encode(receipt['accepted_recipients']),mid))
        conn.execute("UPDATE outbox_messages SET status='sent' WHERE id=?",(mid,))
        service._set_lot_progress(conn,message['lot_id'],'rfq_sent')
        event(db,conn,mid,'mail_sent',message_id=rfc_id,sender=sender,**receipt,attachment_count=len(attachments))
    copy_sent(workflow,mid,confirmed=True)
    return result(db,mid)


def copy_sent(workflow, mid, confirmed):
    if confirmed is not True:
        raise ValueError('Подтвердите сохранение копии')
    db = workflow.db
    with db.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        # Route auth/CSRF remains mandatory. Historical archiving is not a new
        # supplier contact: current outbound eligibility must not block it.
        workflow.service._outbox_record(conn, mid)
        row = conn.execute('SELECT * FROM mail_receipts WHERE message_id=?', (mid,)).fetchone()
        if not row or row['status'] != 'sent':
            raise ConflictError('Нет сохранённого оригинала принятого SMTP письма; повторная отправка запрещена')
        if row['sent_copy_status'] == 'saved':
            return result(db,mid,duplicate=True)
        expired = copy_lease_expired(row)
        if row['sent_copy_status'] == 'saving' and not expired:
            raise ConflictError('Сохранение копии уже выполняется; SMTP-повтор запрещён')
        reconcile = row['sent_copy_status'] == 'unknown' or expired
        lease = uuid.uuid4().hex
        conn.execute("UPDATE mail_receipts SET sent_copy_status='saving',sent_copy_started_at=?,sent_copy_lease=? WHERE message_id=?",
                     (utcnow(),lease,mid))
        if expired:
            event(db,conn,mid,'mail_sent_copy_reconcile',append_allowed=False,smtp_resend=False)
    try:
        outcome = archive_sent(row['spool_path'],row['spool_sha256'],row['rfc_message_id'],reconcile_only=reconcile)
    except Exception as exc:
        outcome = {'status': 'unknown' if isinstance(exc, ArchiveUncertain) or reconcile else 'failed','uid':None}
    with db.connection() as conn:
        changed = conn.execute('UPDATE mail_receipts SET sent_copy_status=?,sent_copy_uid=?,sent_copy_lease=NULL WHERE message_id=? AND sent_copy_lease=?',
                     (outcome['status'],outcome['uid'],mid,lease)).rowcount
        if changed:
            event(db,conn,mid,'mail_sent_copy',status=outcome['status'],smtp_resend=False)
    return result(db,mid)
