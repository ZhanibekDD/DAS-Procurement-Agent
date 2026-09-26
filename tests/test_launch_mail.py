import concurrent.futures
from pathlib import Path

import pytest

from procurement.identity import authenticated_actor
from procurement.models import SupplierCreate, LotCreate, CampaignCreate
from procurement.service import ConflictError
from test_launch_workflow import workflow, project, lot_payload, FIXTURES
from smtp_capture import CaptureSMTP


def prepare(workflow):
    db,service,w=workflow
    pr=project(service)
    doc=service.register_source_document(filename='items.xlsx',content=(FIXTURES/'items.xlsx').read_bytes(),document_type='project_section',project_id=pr['id'])
    lot=service.create_lot(LotCreate(**lot_payload(pr['id'],[doc['id']])))
    s=service.create_supplier(SupplierCreate(name='ТЕСТ адресат',region='Воронеж',email='recipient@example.test'))
    m=service.create_campaign(lot['id'],CampaignCreate(supplier_ids=[s['id']]))['messages'][0]
    service.approve_message(m['id'],'ignored-client-identity')
    return m,doc


def smtp_env(monkeypatch,server):
    for key,value in dict(HOST='127.0.0.1',PORT=str(server.server_address[1]),TLS='none',FROM='sender@example.test').items():
        monkeypatch.setenv('PROCUREMENT_SMTP_'+key,value)
    monkeypatch.delenv('PROCUREMENT_SMTP_USER',raising=False)


def test_real_smtp_rfq_contains_exact_attachment_and_no_duplicate(workflow,monkeypatch):
    db,service,w=workflow;m,doc=prepare(workflow)
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp)
        assert w.send(m['id'],True)['accepted_by_smtp']
        assert w.send(m['id'],True)['duplicate']
        assert len(smtp.messages)==1
        email=smtp.messages[0]
        assert email['To']=='recipient@example.test'
        files=list(email.iter_attachments())
        assert len(files)==1 and files[0].get_filename()=='items.xlsx'
        assert files[0].get_payload(decode=True)==(FIXTURES/'items.xlsx').read_bytes()
        body=email.get_body(preferencelist=('plain',)).get_content()
        assert '001230040500' in body and 'Исполнение 0' in body and '10' in body
    assert service.list_outbox()[0]['status']=='sent'
    assert db.one('SELECT actor FROM mail_deliveries')['actor']=='staff-a'


def test_parallel_send_has_at_most_one_smtp_submission(workflow,monkeypatch):
    db,service,w=workflow;m,_=prepare(workflow)
    def send(_):
        token=authenticated_actor.set('staff-a')
        try:
            try:return w.send(m['id'],True)['status']
            except ConflictError:return 'blocked'
        finally:authenticated_actor.reset(token)
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(send,range(8)))
        assert len(smtp.messages)==1 and 'sent' in results
    assert db.one('SELECT count(*) n FROM mail_deliveries')['n']==1


def test_smtp_failure_no_false_ready_no_retry(workflow,monkeypatch):
    db,service,w=workflow;m,_=prepare(workflow)
    with CaptureSMTP(reject=True) as smtp:
        smtp_env(monkeypatch,smtp)
        with pytest.raises(ConflictError,match='не подтверждена'):w.send(m['id'],True)
        assert db.one('SELECT status FROM mail_deliveries')['status']=='unknown'
        assert service.list_outbox()[0]['status']=='approved'
        smtp.reject=False
        with pytest.raises(ConflictError,match='повтор запрещён'):w.send(m['id'],True)
        assert not smtp.messages


def test_missing_smtp_corrupt_file_and_unapproved_message_fail_closed(workflow,monkeypatch):
    db,service,w=workflow;m,doc=prepare(workflow)
    monkeypatch.delenv('PROCUREMENT_SMTP_HOST',raising=False)
    with pytest.raises(ConflictError,match='не настроена'):w.send(m['id'],True)
    assert not db.all('SELECT * FROM mail_deliveries')
    with CaptureSMTP() as smtp:
        smtp_env(monkeypatch,smtp)
        with pytest.raises(ValueError):w.send(m['id'],False)
        Path(doc['storage_path']).write_bytes(b'corrupt')
        with pytest.raises(ConflictError,match='Вложение'):w.send(m['id'],True)
        assert not smtp.messages and not db.all('SELECT * FROM mail_deliveries')
