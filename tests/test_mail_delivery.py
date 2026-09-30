import hashlib
import json
import socket
import os
import smtplib
from datetime import datetime, UTC, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from test_launch_workflow import workflow, FIXTURES
from test_launch_mail import prepare, smtp_env
from smtp_capture import CaptureSMTP
from imap_capture import CaptureIMAP
from procurement.mail_delivery import copy_sent, journal, COPY_WARNING
from procurement.service import ConflictError
from procurement.mail_delivery import COPY_LEASE_SECONDS, QUEUE_LEASE_SECONDS, smtp_failure_receipt


def imap_env(monkeypatch, server, tmp_path):
    password = tmp_path / 'imap-password'
    password.write_text('TEST-secret-not-for-journals');password.chmod(0o600)
    for key, value in dict(HOST='127.0.0.1',PORT=str(server.server_address[1]),TLS='none',
                           USER='sender@example.test',PASSWORD_FILE=str(password)).items():
        monkeypatch.setenv('PROCUREMENT_IMAP_' + key,value)
    monkeypatch.delenv('PROCUREMENT_IMAP_SENT_FOLDER',raising=False)
    if os.name == 'nt':
        # NTFS does not implement Unix mode bits; the real permission gate is
        # exercised unchanged by Linux CI/canary.
        monkeypatch.setattr('procurement.sent_mail.secret',lambda prefix:password.read_text())


@pytest.mark.parametrize('reject_header_search',[False,True])
def test_recipient_sent_and_registry_match_message_id_and_exact_attachment(workflow,monkeypatch,tmp_path,reject_header_search):
    db,s,w = workflow;m,_ = prepare(workflow)
    with CaptureSMTP() as smtp, CaptureIMAP(reject_header_search=reject_header_search) as imap:
        smtp_env(monkeypatch,smtp);imap_env(monkeypatch,imap,tmp_path)
        r = w.send(m['id'],True)
        assert r['status']=='sent' and r['delivery']['sent_copy_status']=='saved' and r['warning'] is None
        assert len(smtp.messages)==len(imap.messages)==1
        copy = BytesParser(policy=policy.default).parsebytes(imap.messages[0])
        assert r['delivery']['rfc_message_id']==smtp.messages[0]['Message-ID']==copy['Message-ID']
        assert smtp.raw_messages[0]==imap.messages[0]
        assert hashlib.sha256(imap.messages[0]).hexdigest()==r['delivery']['spool_sha256']
        attachment = next(copy.iter_attachments()).get_payload(decode=True)
        assert hashlib.sha256(attachment).hexdigest()==hashlib.sha256((FIXTURES/'items.xlsx').read_bytes()).hexdigest()
        assert r['delivery']['accepted_recipients']==['recipient@example.test']
        assert r['delivery']['smtp_code']==250 and r['delivery']['smtp_reply']=='Accepted'
        assert w.send(m['id'],True)['duplicate']
        assert copy_sent(w,m['id'],True)['duplicate']
        assert len(smtp.messages)==len(imap.messages)==1 and imap.append_calls==1
    assert 'TEST-secret' not in json.dumps(s.list_outbox())+json.dumps(s.list_audit())
    assert 'spool_path' not in r['delivery']


def test_pending_copy_after_worker_exit_is_recoverable_without_resending(workflow,monkeypatch,tmp_path):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp, CaptureIMAP() as imap:
        smtp_env(monkeypatch,smtp);imap_env(monkeypatch,imap,tmp_path)
        monkeypatch.setattr('procurement.mail_delivery.copy_sent',lambda *args,**kwargs:None)
        w.send(m['id'],True)
        info=journal(db,m['id'])
        assert info['status']=='sent' and info['sent_copy_status']=='pending'
        assert info['copy_retry_allowed'] and not info['copy_reconcile_only']
        assert copy_sent(w,m['id'],True)['delivery']['sent_copy_status']=='saved'
        assert len(smtp.messages)==len(imap.messages)==1


def test_lost_smtp_ack_blocks_retry_without_false_sent(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP(drop_after_data=True) as smtp:
        smtp_env(monkeypatch,smtp)
        with pytest.raises(ConflictError,match='Повтор заблокирован'):w.send(m['id'],True)
        assert len(smtp.messages)==1
        assert journal(db,m['id'])['status']=='unknown'
        assert journal(db,m['id'])['retry_allowed'] is False
        with pytest.raises(ConflictError,match='повтор запрещён'):w.send(m['id'],True)
        assert len(smtp.messages)==1 and s.list_outbox()[0]['status']=='failed'


def test_unavailable_smtp_safe_retry_with_same_message_id(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp);monkeypatch.setenv('PROCUREMENT_SMTP_PORT',str(port))
        with pytest.raises(ConflictError,match='SMTP недоступен'):w.send(m['id'],True)
        first=journal(db,m['id'])
        assert first['status']=='failed' and first['retry_allowed'] and len(smtp.messages)==0
        monkeypatch.setenv('PROCUREMENT_SMTP_PORT',str(smtp.server_address[1]))
        r=w.send(m['id'],True)
        assert r['delivery']['rfc_message_id']==first['rfc_message_id']
        assert r['delivery']['spool_sha256']==first['spool_sha256']
        assert r['delivery']['attempt']==2 and len(smtp.messages)==1
        assert w.send(m['id'],True)['duplicate'] and len(smtp.messages)==1


def test_abandoned_pre_smtp_queue_recovers_once_without_new_message_id(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    import procurement.mail_delivery as delivery
    original=delivery.spool_message
    def crash_before_spool(*args):
        raise KeyboardInterrupt('worker exited before SMTP')
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp)
        monkeypatch.setattr(delivery,'spool_message',crash_before_spool)
        with pytest.raises(KeyboardInterrupt):w.send(m['id'],True)
        first=journal(db,m['id'])
        assert first['status']=='queued' and not first['retry_allowed'] and not smtp.messages
        with pytest.raises(ConflictError,match='повтор запрещён'):w.send(m['id'],True)
        started=(datetime.now(UTC)-timedelta(seconds=QUEUE_LEASE_SECONDS+1)).isoformat()
        with db.connection() as conn:
            conn.execute('UPDATE mail_receipts SET started_at=? WHERE message_id=?',(started,m['id']))
        assert journal(db,m['id'])['retry_allowed']
        monkeypatch.setattr(delivery,'spool_message',original)
        result=w.send(m['id'],True)
        assert result['status']=='sent' and result['delivery']['attempt']==2
        assert result['delivery']['rfc_message_id']==first['rfc_message_id']
        assert len(smtp.messages)==1 and w.send(m['id'],True)['duplicate']


def test_stale_queued_worker_is_fenced_before_smtp(workflow,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import procurement.mail_delivery as delivery
    db,s,w=workflow;m,_=prepare(workflow)
    original=delivery.spool_message
    entered,resume=Event(),Event()
    calls=[0]
    def delayed_spool(*args):
        calls[0]+=1
        if calls[0]==1:
            entered.set()
            assert resume.wait(10)
        return original(*args)
    with CaptureSMTP() as smtp, ThreadPoolExecutor(max_workers=2) as pool:
        smtp_env(monkeypatch,smtp)
        monkeypatch.setattr(delivery,'spool_message',delayed_spool)
        first=pool.submit(w.send,m['id'],True)
        assert entered.wait(10)
        with db.connection() as conn:
            conn.execute('UPDATE mail_receipts SET started_at=? WHERE message_id=?',
                         ((datetime.now(UTC)-timedelta(seconds=QUEUE_LEASE_SECONDS+1)).isoformat(),m['id']))
        second=pool.submit(w.send,m['id'],True)
        assert second.result(timeout=20)['status']=='sent'
        resume.set()
        with pytest.raises(ConflictError,match='другой работник'):first.result(timeout=20)
        assert len(smtp.messages)==1 and journal(db,m['id'])['attempt']==2


def test_stale_failure_cannot_overwrite_recovered_smtp_acceptance(workflow,monkeypatch):
    import procurement.mail_delivery as delivery
    db,s,w=workflow;m,_=prepare(workflow)
    original_spool=delivery.spool_message
    first=[True]

    def fail_first_spool(*args):
        if first[0]:
            first[0]=False
            raise OSError('first worker failed before SMTP')
        return original_spool(*args)

    def recover_between_attempt_check_and_failure_write(exc,user,credential):
        # Reproduce the exact interleaving: worker A already checked attempt 1,
        # then worker B takes its expired lease and accepts attempt 2.
        with db.connection() as conn:
            conn.execute('UPDATE mail_receipts SET started_at=? WHERE message_id=?',
                         ((datetime.now(UTC)-timedelta(seconds=QUEUE_LEASE_SECONDS+1)).isoformat(),m['id']))
        recovered=w.send(m['id'],True)
        assert recovered['status']=='sent' and recovered['delivery']['attempt']==2
        return None,None

    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp)
        monkeypatch.setattr(delivery,'spool_message',fail_first_spool)
        monkeypatch.setattr(delivery,'smtp_failure_receipt',recover_between_attempt_check_and_failure_write)
        with pytest.raises(ConflictError,match='другой работник'):
            w.send(m['id'],True)
        info=journal(db,m['id'])
        assert info['status']=='sent' and info['attempt']==2 and len(smtp.messages)==1
        assert db.one('SELECT status FROM mail_deliveries WHERE message_id=?',(m['id'],))['status']=='sent'
        assert db.one('SELECT status FROM outbox_messages WHERE id=?',(m['id'],))['status']=='sent'
        assert not any(row['event']=='mail_send_failed' for row in info['events'])
        assert w.send(m['id'],True)['duplicate'] and len(smtp.messages)==1


def test_abandoned_sending_is_never_retried_automatically(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    import procurement.mail_delivery as delivery
    original=delivery.submit_spool
    def crash_after_sending(*args):
        raise KeyboardInterrupt('SMTP outcome unknown')
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp)
        monkeypatch.setattr(delivery,'submit_spool',crash_after_sending)
        with pytest.raises(KeyboardInterrupt):w.send(m['id'],True)
        assert journal(db,m['id'])['status']=='sending'
        with db.connection() as conn:
            conn.execute('UPDATE mail_receipts SET started_at=? WHERE message_id=?',
                         ((datetime.now(UTC)-timedelta(days=1)).isoformat(),m['id']))
        assert not journal(db,m['id'])['retry_allowed']
        monkeypatch.setattr(delivery,'submit_spool',original)
        with pytest.raises(ConflictError,match='повтор запрещён'):w.send(m['id'],True)
        assert not smtp.messages


def test_imap_failure_warns_and_copy_retry_never_resends(workflow,monkeypatch,tmp_path):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp, CaptureIMAP(reject_append=True) as imap:
        smtp_env(monkeypatch,smtp);imap_env(monkeypatch,imap,tmp_path)
        r=w.send(m['id'],True)
        assert r['status']=='sent' and r['warning']==COPY_WARNING
        assert r['delivery']['sent_copy_status']=='failed'
        assert len(smtp.messages)==1 and len(imap.messages)==0
        imap.reject_append=False
        r=copy_sent(w,m['id'],True)
        assert r['delivery']['sent_copy_status']=='saved'
        assert len(smtp.messages)==len(imap.messages)==1


@pytest.mark.parametrize('reject_header_search',[False,True])
def test_imap_lost_ack_reconciles_without_duplicate_append(workflow,monkeypatch,tmp_path,reject_header_search):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp, CaptureIMAP(drop_append_reply=True,reject_header_search=reject_header_search) as imap:
        smtp_env(monkeypatch,smtp);imap_env(monkeypatch,imap,tmp_path)
        r=w.send(m['id'],True)
        assert r['delivery']['sent_copy_status']=='unknown'
        assert len(imap.messages)==1
        imap.drop_append_reply=False
        r=copy_sent(w,m['id'],True)
        assert r['delivery']['sent_copy_status']=='saved' and imap.append_calls==1
        assert len(smtp.messages)==len(imap.messages)==1


def test_quit_failure_does_not_erase_data_acceptance(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP(quit_failure=True) as smtp:
        smtp_env(monkeypatch,smtp)
        assert w.send(m['id'],True)['status']=='sent'
        assert w.send(m['id'],True)['duplicate'] and len(smtp.messages)==1


def test_recipient_rejected_means_no_submission(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP(reject_recipient=True) as smtp:
        smtp_env(monkeypatch,smtp)
        with pytest.raises(ConflictError,match='отклонил получателя'):w.send(m['id'],True)
        assert journal(db,m['id'])['status']=='failed' and not smtp.messages
        assert journal(db,m['id'])['smtp_code']==550
        assert journal(db,m['id'])['smtp_reply']=='recipient refused'


@pytest.mark.parametrize('server_already_saved',[False,True])
def test_abandoned_copy_lease_only_searches_never_appends(workflow,monkeypatch,tmp_path,server_already_saved):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp,CaptureIMAP(reject_append=not server_already_saved) as imap:
        smtp_env(monkeypatch,smtp);imap_env(monkeypatch,imap,tmp_path)
        w.send(m['id'],True)
        previous_appends=imap.append_calls
        started=(datetime.now(UTC)-timedelta(seconds=COPY_LEASE_SECONDS+1)).isoformat()
        with db.connection() as conn:
            conn.execute("UPDATE mail_receipts SET sent_copy_status='saving',sent_copy_started_at=?,sent_copy_lease='dead-worker'",(started,))
        assert journal(db,m['id'])['copy_retry_allowed']
        imap.reject_append=False
        value=copy_sent(w,m['id'],True)
        assert value['delivery']['sent_copy_status']==('saved' if server_already_saved else 'unknown')
        assert imap.append_calls==previous_appends and len(smtp.messages)==1
        assert 'sent_copy_lease' not in value['delivery']


def test_active_copy_lease_blocked_and_legacy_abandoned_copy_recoverable(workflow,monkeypatch,tmp_path):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp,CaptureIMAP() as imap:
        smtp_env(monkeypatch,smtp);imap_env(monkeypatch,imap,tmp_path);w.send(m['id'],True)
        with db.connection() as conn:
            conn.execute("UPDATE mail_receipts SET sent_copy_status='saving',sent_copy_started_at=?,sent_copy_lease='live-worker'",(datetime.now(UTC).isoformat(),))
        assert not journal(db,m['id'])['copy_retry_allowed']
        with pytest.raises(ConflictError,match='уже выполняется'):copy_sent(w,m['id'],True)
        with db.connection() as conn:conn.execute('UPDATE mail_receipts SET sent_copy_started_at=NULL')
        assert copy_sent(w,m['id'],True)['delivery']['sent_copy_status']=='saved'
        assert imap.append_calls==1 and len(smtp.messages)==1


def test_copy_after_supplier_soft_delete_does_not_relax_send(workflow,monkeypatch,tmp_path):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp,CaptureIMAP(reject_append=True) as imap:
        smtp_env(monkeypatch,smtp);imap_env(monkeypatch,imap,tmp_path)
        w.send(m['id'],True)
        current=w.supplier(m['supplier_id'])
        w.supplier_state(m['supplier_id'],False,current['revision'],True)
        imap.reject_append=False
        assert copy_sent(w,m['id'],True)['delivery']['sent_copy_status']=='saved'
        assert len(smtp.messages)==len(imap.messages)==1
        with pytest.raises(ValueError,match='inactive supplier'):w.send(m['id'],True)


def test_definite_data_failure_retains_bounded_reply_in_receipt_and_audit(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP(reject=True) as smtp:
        smtp.rejection_reply=b'451 temporary queue rejection '+b'x'*1500
        smtp_env(monkeypatch,smtp)
        with pytest.raises(ConflictError,match='451'):w.send(m['id'],True)
        info=journal(db,m['id'])
        assert info['smtp_code']==451 and info['smtp_reply'].startswith('temporary queue rejection')
        assert len(info['smtp_reply'])==1000 and info['retry_allowed']
        failure=next(e for e in info['events'] if e['event']=='mail_send_failed')
        assert failure['details']['smtp_reply']==info['smtp_reply']


def test_smtp_protocol_receipt_never_exposes_auth_reply_or_secret_echo():
    import base64
    password='secret-provider-test'
    assert smtp_failure_receipt(smtplib.SMTPAuthenticationError(535,password.encode()))==(535,'Ответ авторизации скрыт')
    encoded=base64.b64encode(password.encode()).decode()
    code,reply=smtp_failure_receipt(smtplib.SMTPDataError(451,f'rejected {password} {encoded}\x00'.encode()),'staff',password)
    assert code==451 and password not in reply and encoded not in reply and '\x00' not in reply


@pytest.mark.parametrize('trusted_certificate',[False,True])
def test_smtp_ssl_requires_verified_certificate_and_hostname(workflow,monkeypatch,trusted_certificate):
    import ssl
    db,s,w=workflow;m,_=prepare(workflow)
    contexts=[]
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp);monkeypatch.setenv('PROCUREMENT_SMTP_TLS','ssl')
        def verified_transport(host,port,*,timeout,context):
            contexts.append(context)
            assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
            if not trusted_certificate:
                raise ssl.SSLCertVerificationError('untrusted certificate')
            # Only the test fixture uses plaintext loopback, after validating
            # the context supplied to the production SMTP_SSL constructor.
            return smtplib.SMTP(host,port,timeout=timeout)
        monkeypatch.setattr(smtplib,'SMTP_SSL',verified_transport)
        if trusted_certificate:
            assert w.send(m['id'],True)['status']=='sent' and len(smtp.messages)==1
        else:
            with pytest.raises(ConflictError):w.send(m['id'],True)
            assert journal(db,m['id'])['status']=='failed' and not smtp.messages
        assert len(contexts)==1


def test_copy_lease_migration_preserves_existing_receipt(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp);w.send(m['id'],True)
        original=journal(db,m['id'])['rfc_message_id']
    with db.connection() as conn:
        conn.execute('ALTER TABLE mail_receipts DROP COLUMN sent_copy_started_at')
        conn.execute('ALTER TABLE mail_receipts DROP COLUMN sent_copy_lease')
    db.initialize()
    assert journal(db,m['id'])['rfc_message_id']==original
    assert db.one('PRAGMA quick_check')['quick_check']=='ok'


def test_legacy_receipt_is_honest_and_never_resent(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp)
        w.send(m['id'],True)
        with db.connection() as conn:conn.execute('DELETE FROM mail_receipts')
        r=w.send(m['id'],True)
        assert r['duplicate'] and 'не подтверждён журналом SMTP' in r['warning'] and r['delivery']['legacy']
        assert 'SMTP принял' not in r['warning'] and not r['accepted_by_smtp']
        assert 'rfc_message_id' not in r['delivery']
        with pytest.raises(ConflictError,match='Нет сохранённого оригинала'):copy_sent(w,m['id'],True)
        assert len(smtp.messages)==1


def test_copy_parallelism_and_immutable_spool(workflow,monkeypatch,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from procurement.identity import authenticated_actor
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp, CaptureIMAP(reject_append=True) as imap:
        smtp_env(monkeypatch,smtp);imap_env(monkeypatch,imap,tmp_path)
        w.send(m['id'],True);imap.reject_append=False
        original = db.one('SELECT spool_path,spool_sha256 FROM mail_receipts')
        def retry(_):
            token=authenticated_actor.set('staff-a')
            try:
                try:return copy_sent(w,m['id'],True)['delivery']['sent_copy_status']
                except ConflictError:return 'blocked'
            finally:authenticated_actor.reset(token)
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes=list(pool.map(retry,range(8)))
        assert 'saved' in outcomes and len(imap.messages)==len(smtp.messages)==1
        assert hashlib.sha256(Path(original['spool_path']).read_bytes()).hexdigest()==original['spool_sha256']


def test_http_failed_send_visible_safe_retry_and_copy_auth(http_boundary,monkeypatch,tmp_path):
    from test_sso_adapter import login, headers, BOB
    from test_procurement_redesign import fbs
    import procurement.app as application
    client,authority,settings,db=http_boundary
    user=login(client,authority);h=headers(user)
    lot,supplier=fbs(application.service)
    data={'supplier_ids':[supplier['id']],'item_ids':[r['id'] for r in lot['items']]}
    preview=client.post(f"/api/procurement/lots/{lot['id']}/preview",headers=h,json=data).json()
    campaign=client.post(f"/api/lots/{lot['id']}/campaigns",headers=h,json={**data,
        'snapshot_sha256':preview['snapshot_sha256'],'preview_sha256':preview['preview_sha256']}).json()
    mid=campaign['messages'][0]['id'];path=f'/api/procurement/outbox/{mid}/send'
    request={'lot_id':lot['id'],'snapshot_sha256':preview['snapshot_sha256'],'confirmed':True}
    with CaptureSMTP(reject=True) as smtp, CaptureIMAP() as imap:
        smtp_env(monkeypatch,smtp);imap_env(monkeypatch,imap,tmp_path)
        response=client.post(path,headers=h,json=request)
        assert response.status_code==409 and '451' in response.json()['detail']
        row=next(r for r in client.get('/api/outbox').json() if r['id']==mid)
        assert row['status']=='failed' and row['delivery']['retry_allowed'] and not smtp.messages
        smtp.reject=False
        response=client.post(path,headers=h,json=request)
        assert response.status_code==200,response.text
        assert response.json()['accepted_by_smtp'] and response.json()['delivery']['sent_copy_status']=='saved'
        assert client.post(path,headers=h,json=request).json()['duplicate']
        assert len(smtp.messages)==len(imap.messages)==1
        copy=f'/api/launch/outbox/{mid}/sent-copy'
        assert client.post(copy,json={'confirmed':True}).status_code==403
        bob=login(client,authority,BOB);authority.users[BOB]['read_only']=True
        assert client.post(copy,headers=headers(bob),json={'confirmed':True}).status_code==403
        client.cookies.clear()
        assert client.post(copy,headers={'X-OpenWebUI-User-Id':'admin'},json={'confirmed':True}).status_code==401


# Reuse the actual application HTTP/identity fixture, not a forged request header.
from test_launch_http import http_boundary
