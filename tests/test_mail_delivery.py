import hashlib
import json
import socket
import os
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


def test_recipient_sent_and_registry_match_message_id_and_exact_attachment(workflow,monkeypatch,tmp_path):
    db,s,w = workflow;m,_ = prepare(workflow)
    with CaptureSMTP() as smtp, CaptureIMAP() as imap:
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


def test_imap_lost_ack_reconciles_without_duplicate_append(workflow,monkeypatch,tmp_path):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp, CaptureIMAP(drop_append_reply=True) as imap:
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


def test_legacy_receipt_is_honest_and_never_resent(workflow,monkeypatch):
    db,s,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp)
        w.send(m['id'],True)
        with db.connection() as conn:conn.execute('DELETE FROM mail_receipts')
        r=w.send(m['id'],True)
        assert r['duplicate'] and r['warning']==COPY_WARNING and r['delivery']['legacy']
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
