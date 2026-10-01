"""100 MiB boundary, bounded reads, auth, parser and SMTP regressions."""
import asyncio
import hashlib
import io
import tracemalloc
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path

import pytest

from procurement.upload_io import (FilePayload,MAX_FILE,MAX_BODY,MAX_PRICE_REVIEW_BODY,CHUNK,TOO_LARGE,
    UploadTooLarge,staged_upload,payload_sha256,UploadBodyLimit,PACK_TOO_LARGE,upload_request,price_review_request)
from procurement.table_ingest import read_table
from procurement.imports import parse_supplier_table
from procurement.stream_mail import send_streamed
from test_launch_workflow import workflow,project,FIXTURES
from test_launch_http import http_boundary
from test_sso_adapter import login,headers,BOB
from smtp_capture import CaptureSMTP


class ChunkUpload:
    def __init__(self,size):self.remaining=size;self.read_sizes=[]
    async def read(self,size):
        assert 0<size<=CHUNK
        self.read_sizes.append(size)
        n=min(size,self.remaining);self.remaining-=n
        return b'x'*n


@pytest.mark.parametrize('size',[3*1024*1024,MAX_FILE])
def test_stage_disk_bounded_read_and_cleanup(size):
    async def run():
        file=ChunkUpload(size)
        async with staged_upload(file) as payload:
            path=payload.path
            assert len(payload)==size and isinstance(payload,FilePayload)
            assert max(file.read_sizes)==CHUNK
        assert not path.exists()
    asyncio.run(run())


def test_over_limit_friendly_and_no_payload():
    async def run():
        with pytest.raises(UploadTooLarge,match=TOO_LARGE):
            async with staged_upload(ChunkUpload(MAX_FILE+1)):
                pytest.fail('Oversized payload accepted')
    asyncio.run(run())


def test_exact_100mb_storage_hash_memory_and_over_limit(workflow,tmp_path,monkeypatch):
    db,service,w=workflow;pr=project(service)
    path=tmp_path/'source.pdf'
    with path.open('wb') as f:f.write(b'%PDF-1.4\n');f.truncate(MAX_FILE)
    payload=FilePayload(path)
    def deny(*args,**kwargs):raise AssertionError('Whole-file read prohibited')
    monkeypatch.setattr(Path,'read_bytes',deny)
    tracemalloc.start()
    try:
        doc=service.register_source_document(filename='source.pdf',content=payload,document_type='project_section',project_id=pr['id'])
        assert doc['size_bytes']==MAX_FILE
        assert payload_sha256(w.document_file(doc))==payload_sha256(payload)
        assert tracemalloc.get_traced_memory()[1]<8*1024*1024
    finally:tracemalloc.stop()
    with path.open('ab') as f:f.write(b'x')
    with pytest.raises(UploadTooLarge,match=TOO_LARGE):
        service.register_source_document(filename='over.pdf',content=payload,document_type='project_section',project_id=pr['id'])
    assert db.one('SELECT count(*) n FROM source_documents')['n']==1


@pytest.mark.parametrize('name',['suppliers.csv','suppliers.xlsx','items.xlsx'])
def test_spreadsheet_disk_payload_same_result(name,tmp_path,monkeypatch):
    data=(FIXTURES/name).read_bytes();path=tmp_path/name;path.write_bytes(data)
    expected=read_table(data,name)
    monkeypatch.setattr(Path,'read_bytes',lambda *_:pytest.fail('whole file read'))
    assert read_table(FilePayload(path),name)==expected
    if name.startswith('suppliers'):
        # These launch fixtures have no required legacy region column.
        with pytest.raises(ValueError,match='region/city'):
            parse_supplier_table(FilePayload(path),name)


def test_legacy_supplier_csv_stream(tmp_path):
    path=tmp_path/'legacy.csv'
    path.write_bytes('Поставщик;Регион;Почта\nTest;Воронеж;t@example.test\n'.encode())
    assert parse_supplier_table(FilePayload(path),path.name).rows[0].email=='t@example.test'


@pytest.mark.parametrize('path',['/api/documents','/api/procurement/quick-intake'])
@pytest.mark.parametrize('chunked',[False,True])
def test_request_envelope_101mb_and_chunked_limit(chunked,path):
    async def run():
        scope={'type':'http','method':'POST','path':path,'headers':[]}
        if not chunked:scope['headers']=[(b'content-length',str(MAX_BODY+1).encode())]
        emitted=[]
        async def receive():return {'type':'http.request','body':b'x'*CHUNK,'more_body':True}
        async def send(m):emitted.append(m)
        async def app(scope,receive,send):
            from starlette.formparsers import MultiPartException
            from starlette.responses import JSONResponse
            try:
                for _ in range(MAX_BODY//CHUNK+1):await receive()
            except MultiPartException as e:
                await JSONResponse({'detail':e.message},400)(scope,receive,send)
        await UploadBodyLimit(app)(scope,receive,send)
        assert emitted[0]['status']==413
        assert TOO_LARGE.encode() in emitted[1]['body']
    asyncio.run(run())


def test_quick_intake_prebody_identity_and_two_upload_slots(http_boundary):
    from starlette.requests import Request
    import procurement.app as application

    assert upload_request({'type':'http','method':'POST','path':'/api/procurement/quick-intake'})
    assert not upload_request({'type':'http','method':'GET','path':'/api/procurement/quick-intake'})
    assert price_review_request({'type':'http','method':'POST','path':'/api/procurement/catalog/preview-id/review-pdf'})

    async def run():
        # The identity boundary must reject an anonymous production upload before
        # FastAPI/Starlette consumes the multipart body.
        settings=application.settings
        application.settings=replace(settings,sso_enabled=False,environment='production')
        try:
            async def unread_body():
                raise AssertionError('multipart body was read before identity rejection')
            request=Request({'type':'http','method':'POST','path':'/api/procurement/quick-intake',
                'headers':[],'query_string':b''},receive=unread_body)
            async def forbidden_next(_):
                raise AssertionError('unauthenticated request reached FastAPI')
            response=await application.das_identity_boundary(request,forbidden_next)
            assert response.status_code==403
            review=Request({'type':'http','method':'POST','path':'/api/procurement/catalog/preview-id/review-pdf',
                'headers':[],'query_string':b''},receive=unread_body)
            response=await application.das_identity_boundary(review,forbidden_next)
            assert response.status_code==403
        finally:application.settings=settings

        entered=asyncio.Event()
        release=asyncio.Event()
        slots=0
        async def held_app(scope,receive,send):
            nonlocal slots
            slots+=1
            if slots==2:entered.set()
            await release.wait()
        gate=UploadBodyLimit(held_app)
        scope={'type':'http','method':'POST','path':'/api/procurement/quick-intake','headers':[]}
        async def receive():return {'type':'http.request','body':b'','more_body':False}
        sent=[]
        async def send(message):sent.append(message)
        first=asyncio.create_task(gate(scope,receive,send))
        second=asyncio.create_task(gate(scope,receive,send))
        await asyncio.wait_for(entered.wait(),timeout=1)
        try:
            await gate(scope,receive,send)
            assert sent[0]['status']==429
            assert slots==2
        finally:
            release.set()
            await asyncio.gather(first,second)
    asyncio.run(run())


def test_aggregate_request_error_explains_batch_limit():
    async def run():
        sent=[]
        async def send(message):sent.append(message)
        async def app(*args):pytest.fail('Oversized envelope must not be parsed')
        async def receive():pytest.fail('Oversized body must not be read')
        scope={'type':'http','method':'POST','path':'/api/imports/batch','headers':[(b'content-length',str(MAX_BODY+1).encode())]}
        await UploadBodyLimit(app)(scope,receive,send)
        assert sent[0]['status']==413 and PACK_TOO_LARGE.encode() in sent[1]['body']
    asyncio.run(run())


def test_price_review_json_is_bounded_before_parse_and_replayed_intact():
    async def run():
        path='/api/procurement/catalog/preview-id/review-pdf'
        scope={'type':'http','method':'POST','path':path,'headers':[]}
        assert price_review_request(scope) and not upload_request(scope)
        async def app(scope,receive,send):
            body=b''
            while True:
                message=await receive();body+=message.get('body',b'')
                if not message.get('more_body'):break
            assert body==b'{"rows":[]}';await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'ok'})
        emitted=[]
        async def send(message):emitted.append(message)
        chunks=iter([{'type':'http.request','body':b'{"rows":','more_body':True},
                     {'type':'http.request','body':b'[]}','more_body':False}])
        async def receive():return next(chunks)
        await UploadBodyLimit(app)(scope,receive,send)
        assert emitted[0]['status']==200

        # Zero-byte ASGI messages do not accumulate per-message dictionary
        # overhead while a slow authenticated client remains connected.
        fragments=iter([{'type':'http.request','body':b'','more_body':True} for _ in range(10000)]
            +[{'type':'http.request','body':b'{"rows":[]}','more_body':False}])
        async def fragmented_receive():return next(fragments)
        emitted.clear()
        await UploadBodyLimit(app)(scope,fragmented_receive,send)
        assert emitted[0]['status']==200

        blocked=[]
        async def blocked_send(message):blocked.append(message)
        async def must_not_read():pytest.fail('Oversized review must be rejected before parse')
        await UploadBodyLimit(app)({**scope,'headers':[(b'content-length',str(MAX_PRICE_REVIEW_BODY+1).encode())]},
            must_not_read,blocked_send)
        assert blocked[0]['status']==413 and b'2' in blocked[1]['body']

        chunks=iter([{'type':'http.request','body':b'x'*CHUNK,'more_body':True} for _ in range(3)])
        async def chunked_receive():return next(chunks)
        blocked=[]
        await UploadBodyLimit(app)(scope,chunked_receive,blocked_send)
        assert blocked[0]['status']==413
    asyncio.run(run())


def test_stalled_price_review_releases_upload_slot(monkeypatch):
    import procurement.upload_io as upload_io
    original_timeout=asyncio.timeout
    monkeypatch.setattr(upload_io.asyncio,'timeout',lambda _:original_timeout(0.01))
    async def run():
        scope={'type':'http','method':'POST','path':'/api/procurement/catalog/preview-id/review-pdf','headers':[]}
        gate=UploadBodyLimit(lambda *_:pytest.fail('Incomplete body must not reach parser'))
        async def stalled():await asyncio.Event().wait()
        sent=[]
        async def send(message):sent.append(message)
        await gate(scope,stalled,send)
        assert sent[0]['status']==408
        assert gate.slots._value==2
    asyncio.run(run())


def test_upload_and_download_auth_csrf_unchanged_over_old_nginx_limit(http_boundary):
    client,authority,settings,db=http_boundary
    path='/api/documents'
    data=b'%PDF-1.4\n'+b' '* (3*1024*1024)
    params={'document_type':'project_section','project_id':1}
    assert client.post(path,params=params,files={'file':('large.pdf',data)}).status_code==401
    alice=login(client,authority);h=headers(alice)
    pr=client.post('/api/projects',headers=h,json={'name':'Test','region':'Воронеж','delivery_address':'Test'}).json()
    params['project_id']=pr['id']
    assert client.post(path,params=params,files={'file':('large.pdf',data)}).status_code==403
    response=client.post(path,params=params,headers=h,files={'file':('large.pdf',data)})
    assert response.status_code==201,response.text
    doc=response.json();url=f"/api/launch/documents/{doc['id']}/download"
    assert hashlib.sha256(client.get(url).content).hexdigest()==hashlib.sha256(data).hexdigest()
    assert client.head(url).status_code==200
    assert client.get(url,headers={'Range':'bytes=0-9'}).content==data[:10]
    bob=login(client,authority,BOB);authority.users[BOB]['read_only']=True
    assert client.post(path,params=params,headers=headers(bob),files={'file':('large.pdf',data)}).status_code==403
    client.cookies.clear()
    assert client.get(url,headers={'Range':'bytes=0-9','X-OpenWebUI-User-Id':'admin'}).status_code==401


def test_streamed_mime_exact_binary_unicode_filename_dot_transparency(tmp_path):
    path=tmp_path/'attachment';data=bytes(range(256))*17000;path.write_bytes(data)
    e=EmailMessage();e['From']='sender@example.test';e['To']='recipient@example.test';e['Subject']='ТЕСТ'
    e.set_content('.first\n..second\n001230040500 0 10\n')
    with CaptureSMTP() as capture:
        import smtplib
        with smtplib.SMTP('127.0.0.1',capture.server_address[1],timeout=10) as smtp:
            send_streamed(smtp,e,[('исходный.pdf',FilePayload(path),hashlib.sha256(data).hexdigest())])
        received=capture.messages[0]
        assert received.get_body(preferencelist=('plain',)).get_content().startswith('.first\r\n..second')
        file=list(received.iter_attachments())[0]
        assert file.get_filename()=='исходный.pdf' and file.get_payload(decode=True)==data


def test_streamed_mime_provider_size_limit_is_not_success(tmp_path):
    path=tmp_path/'file';path.write_bytes(b'%PDF-'+b'x'*1000)
    class SMTP:
        esmtp_features={'size':'10'}
        def ehlo_or_helo_if_needed(self):pass
        def has_extn(self,name):return True
        def mail(self,*args):pytest.fail('DATA must never begin')
    e=EmailMessage();e['From']='a@example.test';e['To']='b@example.test';e.set_content('body')
    with pytest.raises(ValueError,match='message-size limit'):
        send_streamed(SMTP(),e,[('f.pdf',FilePayload(path),payload_sha256(FilePayload(path)))])
