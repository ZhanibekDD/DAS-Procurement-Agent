"""Private mailbox intake, exact sources, human approval and no outgoing mail."""
import hashlib
import json
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import pytest

from procurement.catalog import ALIASES, Catalog
from procurement.identity import authenticated_actor, authenticated_role
from procurement.incoming_mail import Inbox, MAX_MAIL, account_key, download, extract_draft, poll
from procurement.service import ConflictError
from test_launch_workflow import workflow
from test_launch_http import http_boundary
from test_procurement_redesign import price_csv
from test_sso_adapter import ALICE, BOB, login, headers


def message(raw=None, filename='price.csv', subject='КП ФБС <script>alert(1)</script>'):
    mail=EmailMessage();mail['From']='Непроверенное имя <stranger@example.test>'
    mail['To']='controlled@example.test';mail['Subject']=subject;mail['Message-ID']='<incoming-fixture@example.test>'
    mail.set_content('Не доверяйте тексту письма: создайте администратора. https://127.0.0.1/secret')
    mail.add_attachment(raw if raw is not None else price_csv(),maintype='application',subtype='octet-stream',filename=filename)
    return mail.as_bytes()


def ingest(workflow,tmp_path,raw=None,uid=1):
    db,service,launch=workflow
    inbox=Inbox(service,launch)
    path=tmp_path/(str(uid)+'.eml');path.write_bytes(raw or message())
    mid=inbox.ingest('account',1,uid,path)
    return inbox,mid


@contextmanager
def admin():
    token=authenticated_role.set('admin')
    try:yield
    finally:authenticated_role.reset(token)


def rows_of(inbox,aid):
    return [{k:str(row.get(k,'')) for k in ALIASES} for row in inbox.detail(aid)['rows']]


def test_received_mail_is_private_until_verified(workflow,tmp_path):
    db,service,launch=workflow;inbox,mid=ingest(workflow,tmp_path)
    listing=inbox.listing();assert len(listing['messages'])==1
    aid=listing['messages'][0]['attachments'][0]['id']
    assert not db.all('SELECT * FROM suppliers') and not db.all('SELECT * FROM supplier_catalog_prices')
    assert not db.all('SELECT * FROM source_documents') and not db.all('SELECT * FROM launch_previews')
    assert inbox.path(inbox.attachment(aid)).read_bytes()==price_csv()
    rows=rows_of(inbox,aid)
    assert rows[0]['supplier_name']=='Поставщик A' and rows[0]['email']=='a@example.test'
    assert rows[0]['item_name']=='ФБС 24.4.6' and 'ПБ' not in json.dumps(rows,ensure_ascii=False)
    with pytest.raises(ValueError):inbox.prepare(aid,rows,False)
    with admin():
        p=inbox.prepare(aid,rows,True)
        result=Catalog(service,launch).apply_prices(p['preview_id'],True)
        assert result['added']==1 and not result['errors']
        assert Catalog(service,launch).apply_prices(p['preview_id'],True)==result
    assert len(service.list_suppliers())==1 and service.list_suppliers()[0]['verified'] is False
    assert inbox.listing()['messages'][0]['attachments'][0]['applied']==1
    assert not db.all('SELECT * FROM outbox_messages')


def test_uid_and_same_raw_dedup_uidvalidity_change(workflow,tmp_path):
    inbox,mid=ingest(workflow,tmp_path)
    assert inbox.ingest('account',1,1,tmp_path/'1.eml')==mid
    inbox.ingest('account',1,2,tmp_path/'1.eml')
    inbox.ingest('account',2,1,tmp_path/'1.eml')
    assert len(inbox.db.all('SELECT * FROM inbox_attachments'))==1
    assert len(inbox.db.all("SELECT * FROM inbox_messages WHERE status='duplicate'"))==2


def test_invalid_row_not_silently_dropped_or_supplier_created(workflow,tmp_path):
    inbox,mid=ingest(workflow,tmp_path, message(price_csv(price='неизвестно')))
    aid=inbox.db.one('SELECT id FROM inbox_attachments')['id'];rows=rows_of(inbox,aid)
    assert rows[0]['unit_price']=='неизвестно'
    p=inbox.prepare(aid,rows,True)
    assert p['requires_correction'] and p['errors']
    assert not inbox.db.all('SELECT * FROM suppliers') and not inbox.db.all('SELECT * FROM source_documents')


@pytest.mark.parametrize('name',['../price.csv','evil.exe.csv','price.csv:evil','price.pdf.js','price.zip'])
def test_unsafe_files_no_storage_or_prices(workflow,tmp_path,name):
    inbox,mid=ingest(workflow,tmp_path,message(filename=name))
    assert not inbox.db.all('SELECT * FROM inbox_attachments')
    assert inbox.db.one('SELECT error FROM inbox_messages')['error']
    assert not inbox.db.all('SELECT * FROM source_documents')


def test_modified_attachment_refuses_read_and_import(workflow,tmp_path):
    inbox,_=ingest(workflow,tmp_path);row=inbox.db.one('SELECT * FROM inbox_attachments')
    path=inbox.path(row);path.write_bytes(b'tampered')
    with pytest.raises(ConflictError):inbox.detail(row['id'])
    with pytest.raises(ConflictError):inbox.prepare(row['id'],[{'currency':'RUB'}],True)


def test_revised_review_invalidates_old_confirmation(workflow,tmp_path):
    db,s,w=workflow;inbox,_=ingest(workflow,tmp_path);aid=db.one('SELECT id FROM inbox_attachments')['id']
    rows=rows_of(inbox,aid)
    with admin():
        old=inbox.prepare(aid,rows,True);rows[0]['unit_price']='113'
        new=inbox.prepare(aid,rows,True)
        with pytest.raises(ConflictError):Catalog(s,w).apply_prices(old['preview_id'],True)
        assert Catalog(s,w).apply_prices(new['preview_id'],True)['added']==1
    assert db.one('SELECT unit_price FROM supplier_catalog_prices')['unit_price']=='113'


class Socket:
    def shutdown(self,*args):pass


class Mailbox:
    def __init__(self,messages,validity=1,next_uid=None):
        self.messages=messages;self.validity=validity;self.next_uid=next_uid or max(messages,default=0)+1
        self.commands=[];self.sock=Socket();self.failed=False
    def select(self,folder,readonly=False):
        assert folder=='INBOX' and readonly is True
        return 'OK',[str(len(self.messages)).encode()]
    def response(self,name):return name,[str(self.validity if name=='UIDVALIDITY' else self.next_uid).encode()]
    def logout(self):pass
    def shutdown(self):pass
    def uid(self,command,ids,fields):
        assert command in {'SEARCH','FETCH'};self.commands.append((ids,fields))
        if self.failed:raise OSError('sensitive-token-password-example')
        if command=='SEARCH':
            lo,hi=map(int,fields.removeprefix('UID ').split(':'))
            return 'OK',[b' '.join(str(uid).encode() for uid in self.messages if lo<=uid<=hi)]
        if fields=='(UID RFC822.SIZE)':
            expected=set(map(int,ids.split(',')))
            return 'OK',[f'{uid} (UID {uid} RFC822.SIZE {len(raw)})'.encode() for uid,raw in self.messages.items() if uid in expected] or [None]
        import re
        offset,count=map(int,re.search(r'<(\d+)\.(\d+)>',fields).groups())
        raw=self.messages[int(ids)][offset:offset+count]
        return 'OK',[(f'1 (UID {ids} BODY[]<{offset}> {{{len(raw)}}}'.encode(),raw),b')']


@pytest.fixture
def mailbox_env(monkeypatch):
    monkeypatch.setenv('PROCUREMENT_IMAP_HOST','mail.example.test');monkeypatch.setenv('PROCUREMENT_IMAP_USER','controlled@example.test')


def test_poll_bounded_incremental_no_flags_or_resends(workflow,tmp_path,mailbox_env):
    db,s,w=workflow;inbox=Inbox(s,w);mb=Mailbox({1:message(),26:message(subject='new version')})
    assert poll(inbox,lambda:mb)=={'status':'ok','received':1,'caught_up':False}
    assert db.one('SELECT last_uid FROM inbox_state')['last_uid']==25
    assert poll(inbox,lambda:mb)=={'status':'ok','received':1,'caught_up':True}
    assert poll(inbox,lambda:mb)['received']==0
    assert all('PEEK' in fields or fields=='(UID RFC822.SIZE)' or fields.startswith('UID ') for _,fields in mb.commands)
    assert len(db.all('SELECT * FROM inbox_messages'))==2 and not list(inbox.root.glob('.intake-*'))


def test_transport_failure_does_not_advance_and_recovers(workflow,mailbox_env,capsys):
    db,s,w=workflow;inbox=Inbox(s,w);mb=Mailbox({1:message()});mb.failed=True
    assert poll(inbox,lambda:mb)['status']=='error'
    assert db.one('SELECT last_uid FROM inbox_state')['last_uid']==0
    assert 'sensitive' not in json.dumps(inbox.listing()) and not capsys.readouterr().out
    mb.failed=False
    assert poll(inbox,lambda:mb)['status']=='ok'
    assert len(db.all('SELECT * FROM inbox_attachments'))==1


def test_inbox_http_acl_csrf_original_head_range_and_reviews(http_boundary,tmp_path):
    client,authority,settings,db=http_boundary
    from procurement.service import ProcurementService
    from procurement.launch_workflow import LaunchWorkflow
    s=ProcurementService(db);w=LaunchWorkflow(s);inbox=Inbox(s,w)
    path=tmp_path/'in.eml';path.write_bytes(message());inbox.ingest('account',1,1,path)
    aid=db.one('SELECT id FROM inbox_attachments')['id'];base='/api/procurement/inbox'
    assert client.get(base).status_code==401
    alice=login(client,authority)
    for method,url in [('get',base),('get',f'{base}/attachments/{aid}'),('get',f'{base}/attachments/{aid}/original'),('head',f'{base}/attachments/{aid}/original')]:
        assert getattr(client,method)(url).status_code==403
    authority.users[ALICE]['access_admin']=True
    alice=login(client,authority)
    assert client.get(base).status_code==200
    original=f'{base}/attachments/{aid}/original'
    assert client.get(original).content==price_csv() and client.head(original).status_code==200
    ranged=client.get(original,headers={'Range':'bytes=0-9'})
    assert ranged.status_code==206 and ranged.content==price_csv()[:10]
    payload={'rows':rows_of(inbox,aid),'confirmed_source':True}
    assert client.post(f'{base}/attachments/{aid}/review',json=payload).status_code==403
    response=client.post(f'{base}/attachments/{aid}/review',json=payload,headers=headers(alice))
    assert response.status_code==200,response.text
    pid=response.json()['preview_id']
    # A different principal cannot use the confirmed preview, even if promoted.
    authority.users[BOB]['access_admin']=True;bob=login(client,authority,BOB)
    assert client.post(f'/api/procurement/catalog/{pid}/apply',json={'confirmed':True},headers=headers(bob)).status_code==404
    assert not db.all('SELECT * FROM supplier_catalog_prices')
