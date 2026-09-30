import hashlib
import json
from email.message import EmailMessage
import pytest
from procurement.incoming_mail import Inbox, MAX_DRAFT_BYTES
from test_launch_workflow import workflow
from test_incoming_mail import ingest, message
from test_procurement_redesign import price_csv


@pytest.mark.parametrize('column',['item_name','unmapped'])
def test_large_csv_cell_rejected_before_draft_storage(workflow,tmp_path,column):
    raw=(column+';unit_price\n'+'Я'*8001+';100\n').encode()
    inbox,_=ingest(workflow,tmp_path,message(raw));row=inbox.db.one('SELECT * FROM inbox_attachments')
    assert row['error'] and len(row['draft_json'].encode())<1000
    assert not inbox.detail(row['id'])['rows'] and inbox.path(row).read_bytes()==raw


def test_total_draft_bound_even_with_individually_small_cells(workflow,tmp_path):
    header,row=price_csv().decode().splitlines()
    raw=(header+'\n'+(row+'\n')*100+(row.replace('ФБС 24.4.6','Ф'*7900)+'\n')*200).encode()
    assert len(raw)>MAX_DRAFT_BYTES
    inbox,_=ingest(workflow,tmp_path,message(raw));row=inbox.db.one('SELECT * FROM inbox_attachments')
    assert row['error'] and len(row['draft_json'])<1000


def test_pdf_extractor_cannot_bypass_persistence_bounds(workflow,tmp_path,monkeypatch):
    monkeypatch.setattr('procurement.incoming_mail.extract_draft',lambda *a:{'rows':[{'item_name':'X'*8001}],'errors':[]})
    inbox,_=ingest(workflow,tmp_path);row=inbox.db.one('SELECT * FROM inbox_attachments')
    assert row['error'] and len(row['draft_json'])<1000


def two_attachments(path):
    mail=EmailMessage();mail.set_content('controlled test')
    for name in ('first.csv','second.csv'):mail.add_attachment(price_csv(),maintype='text',subtype='csv',filename=name)
    path.write_bytes(mail.as_bytes())


def test_repeated_second_attachment_quota_failure_leaves_no_files(workflow,tmp_path,monkeypatch):
    db,s,w=workflow;inbox=Inbox(s,w);path=tmp_path/'two.eml';two_attachments(path);budget=inbox.storage_budget
    for _ in range(5):
        calls=[]
        def limited(extra=0):
            calls.append(1)
            if len(calls)==3:raise RuntimeError('inbox_storage_limit')
            budget(extra)
        monkeypatch.setattr(inbox,'storage_budget',limited)
        with pytest.raises(RuntimeError,match='inbox_storage_limit'):inbox.ingest('account',1,1,path)
        assert not list(inbox.root.iterdir())
        assert not db.all('SELECT * FROM inbox_messages') and not db.all('SELECT * FROM inbox_attachments')
    monkeypatch.setattr(inbox,'storage_budget',budget);inbox.ingest('account',1,1,path)
    assert len(list(inbox.root.iterdir()))==2 and len(db.all('SELECT * FROM inbox_attachments'))==2


def test_transaction_abort_removes_only_its_uncommitted_files(workflow,tmp_path,monkeypatch):
    db,s,w=workflow;inbox=Inbox(s,w);path=tmp_path/'one.eml';path.write_bytes(message());original_audit=db.audit
    def fail(*args,**kwargs):raise RuntimeError('test_transaction_abort')
    monkeypatch.setattr(db,'audit',fail)
    with pytest.raises(RuntimeError,match='test_transaction_abort'):inbox.ingest('account',1,1,path)
    assert not list(inbox.root.iterdir()) and not db.all('SELECT * FROM inbox_messages')
    monkeypatch.setattr(db,'audit',original_audit);inbox.ingest('account',1,1,path)
    assert len(db.all('SELECT * FROM inbox_attachments'))==1


def test_after_crash_stable_file_is_reused_and_stale_staging_cleaned(workflow,tmp_path):
    db,s,w=workflow;inbox=Inbox(s,w);inbox.root.mkdir()
    aid=hashlib.sha256(json.dumps(['account',1,1,1]).encode()).hexdigest()[:32]
    original=inbox.root/(aid+'.csv');original.write_bytes(price_csv());old=original.stat().st_mtime_ns
    abandoned=inbox.root/'.attachments-abandoned';abandoned.mkdir();(abandoned/'partial').write_bytes(b'partial')
    inbox.clean_abandoned_staging();assert original.exists() and not abandoned.exists()
    path=tmp_path/'one.eml';path.write_bytes(message());inbox.ingest('account',1,1,path)
    assert original.stat().st_mtime_ns==old and len(list(inbox.root.iterdir()))==1
    assert inbox.db.one('SELECT id FROM inbox_attachments')['id']==aid
